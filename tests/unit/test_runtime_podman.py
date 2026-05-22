from ae.controller.spec import (
    AppManifest,
    AppSpec,
    Metadata,
    ResourceQuantities,
    ResourcesSpec,
    ServiceSpec,
)
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


def _manifest_with_command(
    *,
    command: list[str] | None = None,
    args: list[str] | None = None,
) -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "bucket-init"},
            "spec": {
                "image": "docker.io/minio/mc:latest",
                "replicas": 1,
                **({"command": command} if command is not None else {}),
                **({"args": args} if args is not None else {}),
            },
        }
    )


def _manifest_with_sidecar_command(
    *,
    command: list[str] | None = None,
    args: list[str] | None = None,
) -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "bucket-init"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "containers": [
                    {
                        "name": "init",
                        "image": "docker.io/minio/mc:latest",
                        **({"command": command} if command is not None else {}),
                        **({"args": args} if args is not None else {}),
                    }
                ],
            },
        }
    )


def _container_with_ports(
    *,
    host_port: str = "32001",
    pod_ip: str = "10.88.0.42",
    network_name: str = "podman",
) -> dict:
    return {
        "Id": "container-1",
        "Name": "/ae-blue-rev1-0",
        "Config": {
            "Labels": {
                PodmanRuntime.APP_LABEL: "blue",
                PodmanRuntime.REPLICA_LABEL: "blue-rev1-0",
                PodmanRuntime.REVISION_LABEL: "1",
            }
        },
        "State": {
            "Status": "running",
            "StartedAt": "2025-10-23T00:00:00+00:00",
        },
        "NetworkSettings": {
            "IPAddress": pod_ip,
            "Ports": {
                "8080/tcp": [
                    {
                        "HostIp": "127.0.0.1",
                        "HostPort": host_port,
                    }
                ]
            },
            "Networks": {
                network_name: {
                    "IPAddress": pod_ip,
                }
            },
        },
    }


class _SocketMarker:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


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


def test_podman_runtime_maps_kubernetes_command_args_to_entrypoint(monkeypatch):
    cases = [
        (
            ["/bin/sh", "-c"],
            ["mc alias set local http://minio:9000"],
            ["/bin/sh"],
            ["-c", "mc alias set local http://minio:9000"],
        ),
        (["python", "-m", "rawform.bootstrap"], None, ["python"], ["-m", "rawform.bootstrap"]),
        (None, ["server", "/data"], None, ["server", "/data"]),
        (None, None, None, []),
    ]

    for command, args, expected_entrypoint, expected_args in cases:
        rt = PodmanRuntime()
        calls: list[list[str]] = []

        def fake_run(argv, allow_fail=False):  # noqa: ANN001
            _ = allow_fail
            calls.append(list(argv))
            if argv[:3] == [rt._bin, "container", "exists"]:
                return DummyResult(1)
            return DummyResult(0)

        monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]
        monkeypatch.setattr(rt, "ensure_storage_volumes", lambda *_a, **_k: None)

        manifest = _manifest_with_command(command=command, args=args)
        rt._create_container(manifest, "bucket-init-rev1-0", 1)

        run_call = next(c for c in calls if c[:2] == [rt._bin, "run"])
        image_index = run_call.index("docker.io/minio/mc:latest")
        if expected_entrypoint:
            assert run_call[image_index - 2 : image_index] == [
                "--entrypoint",
                expected_entrypoint[0],
            ]
        else:
            assert "--entrypoint" not in run_call[:image_index]
        assert run_call[image_index + 1 :] == expected_args


def test_podman_runtime_maps_sidecar_command_args_to_entrypoint(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]

    manifest = _manifest_with_sidecar_command(
        command=["/bin/sh", "-c"],
        args=["mc alias set local http://minio:9000"],
    )
    rt._ensure_sidecars(manifest, "bucket-init-rev1-0", 1)

    run_call = next(c for c in calls if c[:2] == [rt._bin, "run"])
    image_index = run_call.index("docker.io/minio/mc:latest")
    assert run_call[image_index - 2 : image_index] == ["--entrypoint", "/bin/sh"]
    assert run_call[image_index + 1 :] == [
        "-c",
        "mc alias set local http://minio:9000",
    ]


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


def test_create_container_normalizes_k8s_memory_quantities(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr("ae.runtime.podman_runtime.choose_host_port", lambda *_, **__: (8080, True))

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        if argv[:3] == [rt._bin, "container", "exists"]:
            return DummyResult(1)
        if argv[:3] == [rt._bin, "images", "--format"]:
            return DummyResult(0, "[]")
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]
    monkeypatch.setattr(rt, "ensure_storage_volumes", lambda *_a, **_k: None)

    manifest = AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="memtest"),
        spec=AppSpec(
            image="docker.io/library/demo-shell:latest",
            replicas=1,
            service=ServiceSpec(port=8080, target_port=8080),
            resources=ResourcesSpec(
                limits=ResourceQuantities(memory="256Mi"),
                requests=ResourceQuantities(memory="128Mi"),
            ),
        ),
    )

    rt._create_container(manifest, "memtest-rev1-0", 1, service=(8080, 8080, None))

    run_calls = [
        c for c in calls if len(c) >= 3 and c[0] == rt._bin and c[1] == "run" and "-d" in c
    ]
    assert run_calls, f"expected a podman run -d call, got: {calls}"
    run_argv = run_calls[0]
    assert "--memory" in run_argv
    assert run_argv[run_argv.index("--memory") + 1] == str(256 * 1024 * 1024)
    assert "--memory-reservation" in run_argv
    assert run_argv[run_argv.index("--memory-reservation") + 1] == str(128 * 1024 * 1024)


def test_endpoint_from_container_uses_published_host_port_by_default() -> None:
    rt = PodmanRuntime()

    endpoint = rt._endpoint_from_container(_manifest_single(), _container_with_ports())

    assert endpoint == "127.0.0.1:32001"


def test_endpoint_from_container_prefers_direct_ip_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AE_PODMAN_ENDPOINT_PREFER_DIRECT", "1")
    rt = PodmanRuntime()

    endpoint = rt._endpoint_from_container(_manifest_single(), _container_with_ports())

    assert endpoint == "10.88.0.42:8080"


def test_endpoint_from_container_falls_back_to_direct_ip_without_published_ports(monkeypatch) -> None:
    rt = PodmanRuntime()
    container = _container_with_ports()
    container["NetworkSettings"]["Ports"] = {}
    calls: list[list[str]] = []

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        return DummyResult(1)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]

    endpoint = rt._endpoint_from_container(_manifest_single(), container)

    assert calls == [[rt._bin, "port", "container-1"]]
    assert endpoint == "10.88.0.42:8080"


def test_port_forward_socket_falls_back_to_container_ip(monkeypatch) -> None:
    rt = PodmanRuntime()
    container = _container_with_ports(pod_ip="10.88.0.42")
    container["State"]["Pid"] = 4242
    calls: list[tuple[int, str, int, int]] = []
    marker = _SocketMarker()

    monkeypatch.setattr(rt, "_inspect_container_record", lambda _cid: container)

    def fake_connect(pid: int, host: str, port: int, *, timeout: int):  # noqa: ANN001
        calls.append((pid, host, port, timeout))
        if host == "127.0.0.1":
            raise OSError("loopback blocked")
        return marker

    monkeypatch.setattr(rt, "_connect_in_network_namespace", fake_connect)

    sock = rt.port_forward_socket(
        pod_id="container-1",
        pod_name=None,
        namespace=None,
        port=8080,
    )

    assert sock is marker
    assert calls == [
        (4242, "127.0.0.1", 8080, 2),
        (4242, "10.88.0.42", 8080, 2),
    ]


def test_connect_in_network_namespace_restores_namespace(monkeypatch) -> None:
    rt = PodmanRuntime()
    calls: list[tuple[str, object]] = []
    marker = _SocketMarker()
    opened = {
        "/proc/self/ns/net": 10,
        "/proc/4242/ns/net": 11,
    }

    monkeypatch.setattr("ae.runtime.podman_runtime.os.open", lambda path, flags: opened[path])
    monkeypatch.setattr(
        "ae.runtime.podman_runtime.os.close",
        lambda fd: calls.append(("close", fd)),
    )
    monkeypatch.setattr(
        rt,
        "_setns",
        lambda fd, nstype=0: calls.append(("setns", (fd, nstype))),
    )
    monkeypatch.setattr(
        "socket.create_connection",
        lambda addr, timeout=0: calls.append(("connect", (addr, timeout))) or marker,
    )

    sock = rt._connect_in_network_namespace(4242, "127.0.0.1", 8080, timeout=3)

    assert sock is marker
    assert calls == [
        ("setns", (11, 0)),
        ("connect", (("127.0.0.1", 8080), 3)),
        ("setns", (10, 0)),
        ("close", 11),
        ("close", 10),
    ]


# ruff: noqa: E501
