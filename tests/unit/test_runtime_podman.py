import base64
import json

import pytest

from ae.apishim.store import ObjectStore
from ae.controller.spec import (
    AppManifest,
    AppSpec,
    DNSConfig,
    DNSConfigOption,
    HostAlias,
    Metadata,
    ResourceQuantities,
    ResourcesSpec,
    ServiceSpec,
    VolumeSpec,
)
from ae.runtime.podman_runtime import PodmanRuntime


class DummyResult:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.code = code
        self.out = out
        self.err = err


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _manifest_single(
    image: str = "localhost/demo-blue:latest",
    image_pull_policy: str | None = None,
) -> AppManifest:
    return AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="blue"),
        spec=AppSpec(
            image=image,
            replicas=1,
            env=[{"name": "APP_NAME", "value": "blue"}],
            service=ServiceSpec(port=8080, target_port=8080),
            image_pull_policy=image_pull_policy,
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
        kind="App",
        metadata=Metadata(name="initapp"),
        spec=AppSpec(
            image="localhost/demo:latest",
            replicas=1,
                init_containers=[
                    {
                        "name": "prep",
                        "image": "alpine:3.20",
                        "command": ["sh", "-c"],
                        "args": ["true"],
                    }
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


def test_podman_init_containers_share_process_namespace(monkeypatch):
    rt = PodmanRuntime()
    captured: list[list[str]] = []

    class P:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def fake_run(argv, **_kwargs):  # noqa: ANN001
        captured.append(list(argv))
        return P()

    monkeypatch.setattr(rt, "_ensure_image", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "_ensure_pod_sandbox", lambda *_a, **_k: "ae-demo-rev1-0-pod")
    monkeypatch.setattr("subprocess.run", fake_run)

    m = AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            share_process_namespace=True,
            init_containers=[
                {"name": "prep", "image": "alpine:3.20", "command": ["true"]}
            ],
        ),
    )

    res = rt.run_init_containers(m, replica_id="demo-rev1-0", revision=1)
    assert res and res[0][1] == 0
    assert any(
        "--pid" in c and "container:ae-demo-rev1-0-pod" in c for c in captured
    ), f"--pid container:ae-demo-rev1-0-pod missing in: {captured}"


def test_podman_env_valuefrom_resolution(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

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

    m = AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo", namespace="demo"),
        spec=AppSpec(
            image="busybox",
            replicas=1,
            resources=ResourcesSpec(
                limits=ResourceQuantities(cpu=0.5),
                requests=ResourceQuantities(memory="128Mi"),
            ),
            env=[
                {"name": "APP_NAME", "value": "demo"},
                {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                {
                    "name": "POD_NAMESPACE",
                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                },
                {
                    "name": "CPU_UNITS",
                    "valueFrom": {
                        "resourceFieldRef": {"resource": "limits.cpu", "divisor": "100m"}
                    },
                },
                {
                    "name": "MEM_UNITS",
                    "valueFrom": {
                        "resourceFieldRef": {"resource": "requests.memory", "divisor": "1Mi"}
                    },
                },
            ],
        ),
    )
    rt._create_container(m, "demo-rev1-0", 1)
    run_calls = [c for c in calls if c[:2] == [rt._bin, "run"]]
    assert run_calls, f"expected podman run call, got: {calls}"
    cmd = run_calls[0]
    envs: dict[str, str] = {}
    for idx, arg in enumerate(cmd):
        if arg == "-e" and idx + 1 < len(cmd):
            key, value = cmd[idx + 1].split("=", 1)
            envs[key] = value
    assert envs["APP_NAME"] == "demo"
    assert envs["POD_NAME"] == "demo"
    assert envs["POD_NAMESPACE"] == "demo"
    assert envs["CPU_UNITS"] == "5"
    assert envs["MEM_UNITS"] == "128"


def test_podman_env_valuefrom_configmap_and_secret(monkeypatch, tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "demo",
        "app-config",
        {"name": "app-config", "namespace": "demo"},
        {"MODE": "auto", "LOG_LEVEL": "debug"},
        status={},
    )
    store.upsert(
        "",
        "v1",
        "secrets",
        "demo",
        "app-secret",
        {"name": "app-secret", "namespace": "demo"},
        {"PASSWORD": _b64("s3cr3t")},
        status={},
    )
    monkeypatch.setenv("AE_APISHIM_DB", str(tmp_path / "apishim.db"))

    rt = PodmanRuntime()
    calls: list[list[str]] = []

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

    m = AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo", namespace="demo"),
        spec=AppSpec(
            image="busybox",
            replicas=1,
            env=[
                {
                    "name": "",
                    "valueFrom": {"configMapKeyRef": {"name": "app-config", "key": ""}},
                },
                {
                    "name": "",
                    "valueFrom": {"secretKeyRef": {"name": "app-secret", "key": ""}},
                },
                {
                    "name": "LOG_LEVEL",
                    "valueFrom": {
                        "configMapKeyRef": {"name": "app-config", "key": "LOG_LEVEL"}
                    },
                },
                {
                    "name": "PASSWORD",
                    "valueFrom": {
                        "secretKeyRef": {"name": "app-secret", "key": "PASSWORD"}
                    },
                },
                {"name": "LOG_LEVEL", "value": "info"},
            ],
        ),
    )
    rt._create_container(m, "demo-rev1-0", 1)
    run_calls = [c for c in calls if c[:2] == [rt._bin, "run"]]
    assert run_calls, f"expected podman run call, got: {calls}"
    cmd = run_calls[0]
    envs: dict[str, str] = {}
    for idx, arg in enumerate(cmd):
        if arg == "-e" and idx + 1 < len(cmd):
            key, value = cmd[idx + 1].split("=", 1)
            envs[key] = value
    assert envs["MODE"] == "auto"
    assert envs["PASSWORD"] == "s3cr3t"
    assert envs["LOG_LEVEL"] == "info"


def test_podman_envfrom_prefix(monkeypatch, tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "demo",
        "app-config",
        {"name": "app-config", "namespace": "demo"},
        {"MODE": "auto"},
        status={},
    )
    monkeypatch.setenv("AE_APISHIM_DB", str(tmp_path / "apishim.db"))

    rt = PodmanRuntime()
    calls: list[list[str]] = []

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

    m = AppManifest(
        api_version="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo", namespace="demo"),
        spec=AppSpec(
            image="busybox",
            replicas=1,
            env=[
                {
                    "name": "",
                    "valueFrom": {
                        "configMapKeyRef": {
                            "name": "app-config",
                            "key": "",
                            "prefix": "CFG_",
                        }
                    },
                },
            ],
        ),
    )
    rt._create_container(m, "demo-rev1-0", 1)
    run_calls = [c for c in calls if c[:2] == [rt._bin, "run"]]
    assert run_calls, f"expected podman run call, got: {calls}"
    cmd = run_calls[0]
    envs: dict[str, str] = {}
    for idx, arg in enumerate(cmd):
        if arg == "-e" and idx + 1 < len(cmd):
            key, value = cmd[idx + 1].split("=", 1)
            envs[key] = value
    assert envs["CFG_MODE"] == "auto"


def test_podman_image_pull_policy_always_pulls(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr(rt, "_image_present", lambda *_a, **_k: True)

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]

    manifest = _manifest_single(image="demo:latest", image_pull_policy="Always")
    rt._ensure_image("demo:latest", manifest=manifest)

    assert [rt._bin, "pull", "demo:latest"] in calls


def test_podman_image_pull_policy_ifnotpresent_skips_pull(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr(rt, "_image_present", lambda *_a, **_k: True)

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]

    manifest = _manifest_single(image="demo:1.0", image_pull_policy="IfNotPresent")
    rt._ensure_image("demo:1.0", manifest=manifest)

    assert calls == []


def test_podman_image_pull_policy_never_missing(monkeypatch):
    rt = PodmanRuntime()
    monkeypatch.setattr(rt, "_image_present", lambda *_a, **_k: False)

    manifest = _manifest_single(image="demo:2.0", image_pull_policy="Never")
    with pytest.raises(RuntimeError):
        rt._ensure_image("demo:2.0", manifest=manifest)


def test_podman_image_pull_secrets_login(monkeypatch, tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "secrets",
        "default",
        "pull-secret",
        {"name": "pull-secret", "namespace": "default"},
        {
            ".dockerconfigjson": json.dumps(
                {"auths": {"ghcr.io": {"username": "user", "password": "pass"}}}
            )
        },
        status={},
    )
    monkeypatch.setenv("AE_APISHIM_DB", str(tmp_path / "apishim.db"))

    rt = PodmanRuntime()
    logins: list[tuple[str, str, str]] = []

    monkeypatch.setattr(rt, "_podman_login", lambda r, u, p: logins.append((r, u, p)))
    monkeypatch.setattr(rt, "_image_present", lambda *_a, **_k: False)
    monkeypatch.setattr(rt, "_run_ok", lambda *_a, **_k: DummyResult(0))  # type: ignore[arg-type]

    manifest = _manifest_single(image="ghcr.io/acme/demo:1", image_pull_policy="Always")
    manifest = manifest.model_copy(
        update={
            "spec": manifest.spec.model_copy(update={"image_pull_secrets": ["pull-secret"]})
        }
    )
    rt._ensure_image("ghcr.io/acme/demo:1", manifest=manifest)

    assert logins == [("ghcr.io", "user", "pass")]


def test_podman_host_aliases_and_dns_config(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr(rt, "_image_exists", lambda *_a, **_k: True)

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        if argv[:3] == [rt._bin, "container", "exists"]:
            return DummyResult(1)
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]
    monkeypatch.setattr(rt, "ensure_storage_volumes", lambda *_a, **_k: None)

    base = _manifest_single(image="demo:3.0")
    manifest = base.model_copy(
        update={
            "spec": base.spec.model_copy(
                update={
                    "host_aliases": [
                        HostAlias(ip="10.0.0.10", hostnames=["db.local", "cache.local"])
                    ],
                    "dns_config": DNSConfig(
                        nameservers=["1.1.1.1"],
                        searches=["svc.cluster.local"],
                        options=[DNSConfigOption(name="ndots", value="5")],
                    ),
                }
            )
        }
    )

    rt._create_container(manifest, "blue-rev1-0", 1, service=(None, None, None))

    run_calls = [
        c for c in calls if len(c) >= 3 and c[0] == rt._bin and c[1] == "run" and "-d" in c
    ]
    assert run_calls, f"expected podman run call, got: {calls}"
    cmd = run_calls[0]
    assert "--add-host" in cmd
    assert "db.local:10.0.0.10" in cmd
    assert "cache.local:10.0.0.10" in cmd
    assert "--dns" in cmd and "1.1.1.1" in cmd
    assert "--dns-search" in cmd and "svc.cluster.local" in cmd
    assert "--dns-opt" in cmd and "ndots:5" in cmd


def test_podman_host_namespaces(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr(rt, "_image_exists", lambda *_a, **_k: True)

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

    base = _manifest_single(image="demo:latest")
    manifest = base.model_copy(
        update={
            "spec": base.spec.model_copy(
                update={"host_network": True, "host_pid": True, "host_ipc": True}
            )
        }
    )
    rt._create_container(manifest, "blue-rev1-0", 1, service=(8080, 8080, None))

    run_calls = [
        c for c in calls if len(c) >= 3 and c[0] == rt._bin and c[1] == "run" and "-d" in c
    ]
    assert run_calls, f"expected podman run call, got: {calls}"
    cmd = run_calls[0]
    assert "--network" in cmd and "host" in cmd
    assert "--pid" in cmd and "host" in cmd
    assert "--ipc" in cmd and "host" in cmd
    assert "-p" not in cmd


def test_podman_share_process_namespace_sidecar(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr(rt, "_ensure_image", lambda *_a, **_k: None)

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]

    base = _manifest_single(image="demo:latest")
    manifest = base.model_copy(
        update={
            "spec": base.spec.model_copy(
                update={
                    "share_process_namespace": True,
                    "containers": [AppSpec.ContainerSpec(name="sidecar", image="busybox")],
                }
            )
        }
    )

    rt._ensure_sidecars(manifest, "blue-rev1-0", 1)

    run_calls = [
        c for c in calls if len(c) >= 3 and c[0] == rt._bin and c[1] == "run" and "-d" in c
    ]
    assert run_calls, f"expected podman run call, got: {calls}"
    def _cmd_name(cmd):  # noqa: ANN001
        try:
            idx = cmd.index("--name")
            return cmd[idx + 1]
        except Exception:
            return ""

    sidecar_cmd = next(
        (c for c in run_calls if _cmd_name(c) == "ae-blue-rev1-0-sidecar"), run_calls[0]
    )
    assert "--pid" in sidecar_cmd and "container:ae-blue-rev1-0-pod" in sidecar_cmd


def test_podman_sidecar_includes_global_volumes(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    monkeypatch.setattr(rt, "_ensure_image", lambda *_a, **_k: None)

    def fake_run(argv, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        calls.append(list(argv))
        return DummyResult(0)

    monkeypatch.setattr(rt, "_run_ok", fake_run)  # type: ignore[arg-type]

    base = _manifest_single(image="demo:latest")
    manifest = base.model_copy(
        update={
            "spec": base.spec.model_copy(
                update={
                    "volumes": [VolumeSpec(host_path="/tmp/shared", mount_path="/data")],
                    "containers": [AppSpec.ContainerSpec(name="sidecar", image="busybox")],
                }
            )
        }
    )

    rt._ensure_sidecars(manifest, "blue-rev1-0", 1)

    run_calls = [
        c for c in calls if len(c) >= 3 and c[0] == rt._bin and c[1] == "run" and "-d" in c
    ]
    assert run_calls, f"expected podman run call, got: {calls}"
    def _cmd_name(cmd):  # noqa: ANN001
        try:
            idx = cmd.index("--name")
            return cmd[idx + 1]
        except Exception:
            return ""

    sidecar_cmd = next(
        (c for c in run_calls if _cmd_name(c) == "ae-blue-rev1-0-sidecar"), run_calls[0]
    )
    assert "-v" in sidecar_cmd
    assert "/tmp/shared:/data:rw" in sidecar_cmd


def test_podman_endpoint_uses_advertise_ip(monkeypatch):
    monkeypatch.setenv("AE_NODE_ADVERTISE_IP", "10.0.0.10")
    rt = PodmanRuntime()

    container = {
        "Config": {
            "Labels": {
                rt.REVISION_LABEL: "1",
                rt.POD_LABEL: "blue-rev1-0",
                rt.CONTAINER_LABEL: "main",
            }
        },
        "State": {"Status": "running"},
        "NetworkSettings": {
            "Ports": {"8080/tcp": [{"HostPort": "32001", "HostIp": "0.0.0.0"}]}
        },
    }

    monkeypatch.setattr(rt, "_list_app_containers", lambda _app: [container])
    monkeypatch.setattr(rt, "_create_container", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "_ensure_sidecars", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "_image_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(rt, "_maybe_inject_pvc_mounts", lambda m, **_: m)

    manifest = _manifest_single(image="demo:latest")
    result = rt.ensure_app(manifest, revision=1)

    assert result.pod_states
    assert result.pod_states[0].endpoint == "10.0.0.10:32001"


def test_podman_injects_pvc_mounts(monkeypatch):
    rt = PodmanRuntime()
    calls: list[list[str]] = []

    class StubVolumeManager:
        def inject_pvc_mounts(self, manifest, node_id=None):  # noqa: ANN001
            _ = node_id
            vols = list(getattr(manifest.spec, "volumes", []) or [])
            vols.append(VolumeSpec(host_path="/tmp/netfs", mount_path="/data"))
            updated = manifest.spec.model_copy(update={"volumes": vols})
            return manifest.model_copy(update={"spec": updated})

    monkeypatch.setattr(rt, "_get_volume_manager", lambda: StubVolumeManager())
    monkeypatch.setattr(rt, "_image_exists", lambda *_a, **_k: True)

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

    manifest = _manifest_single(image="demo:latest")
    rt.ensure_app(manifest, revision=1)

    run_calls = [
        c for c in calls if len(c) >= 3 and c[0] == rt._bin and c[1] == "run" and "-d" in c
    ]
    assert run_calls, f"expected podman run call, got: {calls}"
    cmd = run_calls[0]
    assert "-v" in cmd
    assert "/tmp/netfs:/data:rw" in cmd


# ruff: noqa: E501
