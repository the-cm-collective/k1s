from ae.runtime.podman_runtime import PodmanRuntime
from ae.controller.spec import AppManifest, Metadata, AppSpec, ServiceSpec


class DummyResult:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.code = code
        self.out = out
        self.err = err


def _manifest_single(image: str = "localhost/demo-blue:latest") -> AppManifest:
    return AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="App",
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

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
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
    monkeypatch.setattr(rt, "ensure_storage_volumes", lambda *a, **k: None)

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
