from ae.controller.spec import AppManifest, AppSpec, Metadata, ServiceSpec
from ae.runtime.podman_runtime import PodmanRuntime


class DummyResult:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.code = code
        self.out = out
        self.err = err


def _manifest_single(image: str = "localhost/demo-blue:latest") -> AppManifest:
    return AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="blue"),
        spec=AppSpec(
            image=image,
            replicas=1,
            env=[{"name": "APP_NAME", "value": "blue"}],
            service=ServiceSpec(port=8080, target_port=8080),
        ),
    )


def test_create_container_removes_existing(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr("ae.runtime.podman_runtime.choose_host_port", lambda *_, **__: (8080, True))

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        # Simulate: container exists -> exit code 0 only for `container exists <name>`
        if argv[:3] == [rt._bin, "container", "exists"]:
            return DummyResult(0)
        # Simulate image existence query returning empty list
        if argv[:3] == [rt._bin, "images", "--format"]:
            return DummyResult(0, "[]")
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]
    # Avoid invoking actual volume helpers
    monkeypatch.setattr(rt, "ensure_storage_volumes", lambda *_a, **_k: None)

    m = _manifest_single()
    # Call the private helper to focus the behavior
    rt._create_container(m, "blue-rev3-0", 3, service=(8080, 8080, None))

    # Expect a `container exists <name>` check, a stop+rm by name, then `run ... --name <name>`
    names = [" ".join(c) for c in calls]
    assert any(" container exists ae-blue-rev3-0" in s for s in names)
    # stop timeout is configurable; ensure a stop with -t is issued
    assert any(" stop -t " in s and " ae-blue-rev3-0" in s for s in names)
    assert any(" rm -f ae-blue-rev3-0" in s for s in names)
    assert any(" run -d --name ae-blue-rev3-0" in s for s in names)


def test_podman_serial_service_rollout_removes_old(monkeypatch):
    monkeypatch.setenv("AE_SERIAL_SERVICE_ROLLOUT", "1")
    rt = PodmanRuntime()

    calls = {"list": 0}

    def fake_list(_app):  # noqa: ANN001
        calls["list"] += 1
        labels = {
            rt.APP_LABEL: "blue",
            rt.REPLICA_LABEL: "blue-rev0-0" if calls["list"] == 1 else "blue-rev1-0",
            rt.REVISION_LABEL: "0" if calls["list"] == 1 else "1",
        }
        entry = {
            "Id": "old-id" if calls["list"] == 1 else "new-id",
            "Config": {"Labels": labels},
            "State": {"Status": "running"},
        }
        return [entry]

    monkeypatch.setattr(rt, "_list_app_containers", fake_list)
    monkeypatch.setattr(rt, "_image_exists", lambda *_, **__: True)
    monkeypatch.setattr(rt, "_run_ok", lambda *_, **__: DummyResult(0, "[]"))
    monkeypatch.setattr(rt, "_create_container", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "_ensure_sidecars", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "_find_by_label", lambda *_a, **_k: None)

    removed_ids: list[str] = []
    monkeypatch.setattr(rt, "_stop_and_remove", lambda cid: removed_ids.append(cid))

    manifest = _manifest_single()
    result = rt.ensure_app(manifest, revision=1)

    assert removed_ids == ["old-id"]
    assert result.removed >= 1


def test_oci_runtime_flag_in_create(monkeypatch):
    # Ensure env is read at init time
    monkeypatch.setenv("AE_OCI_RUNTIME", "crun")
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr("ae.runtime.podman_runtime.choose_host_port", lambda *_, **__: (8080, True))

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        # Behave as non-existing container, and no local images
        if argv[:3] == [rt._bin, "container", "exists"]:
            return DummyResult(1)
        if argv[:3] == [rt._bin, "images", "--format"]:
            return DummyResult(0, "[]")
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]
    monkeypatch.setattr(rt, "ensure_storage_volumes", lambda *_a, **_k: None)

    m = _manifest_single()
    rt._create_container(m, "blue-rev1-0", 1, service=(8080, 8080, None))

    # Find the `podman run -d ...` invocation and assert --runtime crun is present
    run_calls = [
        c for c in calls if len(c) >= 3 and c[0] == rt._bin and c[1] == "run" and "-d" in c
    ]
    assert run_calls, f"expected a podman run -d call, got: {calls}"
    assert any(
        "--runtime" in c and "crun" in c for c in run_calls
    ), f"--runtime crun missing in: {run_calls}"


def test_oci_runtime_flag_in_init_containers(monkeypatch):
    monkeypatch.setenv("AE_OCI_RUNTIME", "crun")
    rt = PodmanRuntime()

    # Build a manifest with a simple init container
    m = AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="initapp"),
        spec=AppSpec(
            image="localhost/demo:latest",
            replicas=1,
            init_containers=[
                {"name": "prep", "image": "alpine", "command": ["sh", "-c"], "args": ["true"]}
            ],
        ),
    )

    captured: list[list[str]] = []

    class P:  # minimal proc-like object
        def __init__(self):
            self.returncode = 0

    def fake_popen(argv, **_kwargs):  # noqa: ANN001
        # We only intercept subprocess.run used by init containers here
        captured.append(list(argv))
        return P()

    # Avoid volume creation and image lookup side effects
    monkeypatch.setattr(rt, "ensure_storage_volumes", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "_image_exists", lambda *_a, **_k: True)
    monkeypatch.setattr("subprocess.run", fake_popen)

    res = rt.run_init_containers(m)
    assert res and res[0][1] == 0
    # Ensure the run argv contains --runtime crun
    assert any(
        c[:2] == [rt._bin, "run"] and "--runtime" in c and "crun" in c for c in captured
    ), f"--runtime crun missing in: {captured}"


# ruff: noqa: E501
