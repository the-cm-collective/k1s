import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

_CRI_STACK_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "cri_stack.py"
_SPEC = spec_from_file_location("cri_stack_script", _CRI_STACK_PATH)
assert _SPEC is not None and _SPEC.loader is not None
cri_stack = module_from_spec(_SPEC)
_SPEC.loader.exec_module(cri_stack)


def test_runp_args_default_runtime_handler_is_runc(monkeypatch) -> None:
    monkeypatch.delenv("AE_CRI_RUNTIME_HANDLER", raising=False)
    pod_cfg = Path("pod.json")
    assert cri_stack._runp_args(pod_cfg) == ["runp", "-r", "runc", str(pod_cfg)]


def test_runp_args_runtime_handler_override(monkeypatch) -> None:
    monkeypatch.setenv("AE_CRI_RUNTIME_HANDLER", "kata")
    pod_cfg = Path("pod.json")
    assert cri_stack._runp_args(pod_cfg) == ["runp", "-r", "kata", str(pod_cfg)]
    assert cri_stack._runp_args(pod_cfg, "runc") == ["runp", "-r", "runc", str(pod_cfg)]


def test_pod_payload_omits_hostname_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AE_CRI_SET_HOSTNAME", raising=False)
    payload = cri_stack._pod_payload(
        component="k1s-core-etcd",
        work=Path("state/profiles/k1s-core/cri/k1s-core-etcd"),
        labels={"ae.stack.profile": "k1s-core"},
    )
    assert "hostname" not in payload
    assert payload["linux"]["security_context"]["namespace_options"]["network"] == 2


def test_pod_payload_includes_hostname_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AE_CRI_SET_HOSTNAME", "1")
    payload = cri_stack._pod_payload(
        component="k1s-core-etcd",
        work=Path("state/profiles/k1s-core/cri/k1s-core-etcd"),
        labels={"ae.stack.profile": "k1s-core"},
    )
    assert payload["hostname"] == "k1s-core-etcd"


def test_start_rathole_server_uses_image_entrypoint_args(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    cfg = tmp_path / "server.toml"
    cfg.write_text("[server]\n", encoding="utf-8")
    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    cri_stack._start_rathole_server("k1s-core", cfg, runtime_handler="runc")

    assert captured["runtime_handler"] == "runc"
    assert captured.get("command") is None
    assert captured["args"] == ["--server", "/etc/rathole/server.toml"]


def test_start_rathole_client_uses_image_entrypoint_args(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    cfg = tmp_path / "client.toml"
    cfg.write_text("[client]\n", encoding="utf-8")
    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    cri_stack._start_rathole_client(
        "k1s-edge",
        "sea-edge-02",
        "edge-1",
        cfg,
        runtime_handler="runc",
    )

    assert captured["runtime_handler"] == "runc"
    assert captured.get("command") is None
    assert captured["args"] == ["--client", "/etc/rathole/client.toml"]


def test_start_etcd_uses_ae_cri_data_root(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    data_root = (tmp_path / "cri-data").resolve()
    monkeypatch.setenv("AE_CRI_DATA_ROOT", str(data_root))
    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)

    cri_stack._start_etcd("k1s-core", runtime_handler="runc", recreate=False)

    mounts = captured["mounts"]
    assert isinstance(mounts, list)
    assert {
        "host_path": str(data_root / "etcd"),
        "container_path": "/etcd-data",
        "readonly": False,
    } in mounts


def test_cri_data_root_defaults_to_profile_scoped_path_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("AE_CRI_DATA_ROOT", raising=False)
    assert cri_stack._cri_data_root("k1s-core") == (
        cri_stack.ROOT / "state" / "profiles" / "k1s-core" / "cri-data"
    )


def test_start_etcd_allows_cluster_overrides(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    data_root = (tmp_path / "cri-data").resolve()
    monkeypatch.setenv("AE_CRI_DATA_ROOT", str(data_root))
    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)

    cri_stack._start_etcd(
        "k1s-ha-core",
        name="core-a",
        component="k1s-ha-core-etcd",
        advertise_client_urls="http://192.168.155.10:2379",
        initial_advertise_peer_urls="http://192.168.155.10:2380",
        initial_cluster=(
            "core-a=http://192.168.155.10:2380,"
            "core-b=http://192.168.155.11:2380,"
            "core-c=http://192.168.155.12:2380"
        ),
        data_dir_name="ha-etcd",
        runtime_handler="runc",
        recreate=True,
    )

    assert captured["component"] == "k1s-ha-core-etcd"
    assert captured["recreate"] is True
    assert "--name=core-a" in captured["command"]
    assert "--advertise-client-urls=http://192.168.155.10:2379" in captured["command"]
    assert "--initial-advertise-peer-urls=http://192.168.155.10:2380" in captured["command"]
    assert (
        "--initial-cluster=core-a=http://192.168.155.10:2380,"
        "core-b=http://192.168.155.11:2380,"
        "core-c=http://192.168.155.12:2380"
    ) in captured["command"]
    mounts = captured["mounts"]
    assert isinstance(mounts, list)
    assert {
        "host_path": str(data_root / "ha-etcd"),
        "container_path": "/etcd-data",
        "readonly": False,
    } in mounts


def test_start_postgres_reset_data_removes_only_profile_dir(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    removed: list[str] = []

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    def fake_find_pod(profile: str, component: str):
        assert profile == "k1s-core"
        assert component == "k1s-core-postgres"
        return {"id": "pod-1"}

    def fake_remove_pod(pod_id: str) -> None:
        removed.append(pod_id)

    data_root = (tmp_path / "cri-data").resolve()
    pg_data = data_root / "postgres"
    pg_data.mkdir(parents=True, exist_ok=True)
    (pg_data / "PG_VERSION").write_text("16", encoding="utf-8")
    monkeypatch.setenv("AE_CRI_DATA_ROOT", str(data_root))
    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    monkeypatch.setattr(cri_stack, "_find_pod", fake_find_pod)
    monkeypatch.setattr(cri_stack, "_remove_pod", fake_remove_pod)

    cri_stack._start_postgres("k1s-core", runtime_handler="runc", recreate=True, reset_data=True)

    assert removed == ["pod-1"]
    assert not (pg_data / "PG_VERSION").exists()
    mounts = captured["mounts"]
    assert isinstance(mounts, list)
    assert {
        "host_path": str(pg_data),
        "container_path": "/var/lib/postgresql/data",
        "readonly": False,
    } in mounts


def test_start_postgres_honors_configured_bind_ip_and_port(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    data_root = (tmp_path / "cri-data").resolve()
    monkeypatch.setenv("AE_CRI_DATA_ROOT", str(data_root))
    monkeypatch.setenv("POSTGRES_PORT", "35432")
    monkeypatch.setenv("POSTGRES_BIND_IP", "10.55.0.2")
    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)

    cri_stack._start_postgres("k1s-core", runtime_handler="runc", recreate=True)

    assert captured["component"] == "k1s-core-postgres"
    assert captured["runtime_handler"] == "runc"
    assert captured["recreate"] is True
    assert captured["args"] == [
        "-c",
        "port=35432",
        "-c",
        "listen_addresses=10.55.0.2,127.0.0.1",
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["POSTGRES_USER"] == "shim"
    assert env["POSTGRES_PASSWORD"] == "shim"
    assert env["POSTGRES_DB"] == "shim"


def test_start_envoy_mounts_state_paths_for_tls_resolution(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    monkeypatch.setattr(cri_stack, "_ensure_envoy_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cri_stack, "_stable_hash", lambda parts: "|".join(str(item) for item in parts))
    monkeypatch.setattr(cri_stack, "_envoy_base_id", lambda *_args, **_kwargs: 4242)
    monkeypatch.delenv("AE_TLS_DIR", raising=False)
    cfg = tmp_path / "envoy.yaml"
    cfg.write_text("static_resources: {}", encoding="utf-8")

    cri_stack._start_envoy("k1s-core", cfg, runtime_handler="runc")

    mounts = captured["mounts"]
    assert isinstance(mounts, list)
    state_dir = str((cri_stack.ROOT / "state").resolve())
    assert {
        "host_path": str(cfg),
        "container_path": "/etc/envoy/envoy.yaml",
        "readonly": True,
    } in mounts
    assert captured["stable_seconds"] == 2
    assert captured["run_as_user"] == 0
    assert "run_as_user=0" in captured["rollout_key"]
    assert "base_id=4242" in captured["rollout_key"]
    assert captured["command"] == [
        "envoy",
        "-c",
        "/etc/envoy/envoy.yaml",
        "--log-level",
        "info",
        "--base-id",
        "4242",
    ]
    assert {"host_path": state_dir, "container_path": "/state", "readonly": True} in mounts
    assert {"host_path": state_dir, "container_path": state_dir, "readonly": True} in mounts


def test_start_envoy_mounts_external_absolute_tls_dir(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    monkeypatch.setattr(cri_stack, "_ensure_envoy_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cri_stack, "_envoy_base_id", lambda *_args, **_kwargs: 4242)
    external_tls = (tmp_path / "tls-outside").resolve()
    external_tls.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AE_TLS_DIR", str(external_tls))
    cfg = tmp_path / "envoy.yaml"
    cfg.write_text("static_resources: {}", encoding="utf-8")

    cri_stack._start_envoy("k1s-core", cfg, runtime_handler="runc")

    mounts = captured["mounts"]
    assert isinstance(mounts, list)
    assert {
        "host_path": str(external_tls),
        "container_path": str(external_tls),
        "readonly": True,
    } in mounts


def test_start_envoy_supports_controlplane_component_and_secret_mounts(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    monkeypatch.setattr(cri_stack, "_ensure_envoy_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cri_stack, "_envoy_base_id", lambda *_args, **_kwargs: 8484)
    cfg = tmp_path / "envoy.yaml"
    cfg.write_text("static_resources: {}", encoding="utf-8")
    secret = tmp_path / "controlplane-secrets.yaml"
    secret.write_text("static_resources: {}", encoding="utf-8")

    cri_stack._start_envoy(
        "k1s-core",
        cfg,
        component="k1s-core-envoy-controlplane",
        extra_mounts=(secret,),
        runtime_handler="runc",
    )

    mounts = captured["mounts"]
    assert isinstance(mounts, list)
    assert captured["component"] == "k1s-core-envoy-controlplane"
    assert captured["run_as_user"] == 0
    assert captured["command"][-2:] == ["--base-id", "8484"]
    assert {
        "host_path": str(secret.resolve()),
        "container_path": str(secret.resolve()),
        "readonly": True,
    } in mounts


def test_envoy_base_id_is_stable_and_component_specific() -> None:
    assert cri_stack._envoy_base_id("k1s-core", "k1s-core-envoy") == cri_stack._envoy_base_id(
        "k1s-core", "k1s-core-envoy"
    )
    assert cri_stack._envoy_base_id("k1s-core", "k1s-core-envoy") != cri_stack._envoy_base_id(
        "k1s-core", "k1s-core-envoy-controlplane"
    )
    assert cri_stack._envoy_base_id("k1s-core", "k1s-core-envoy") > 0


def test_start_component_unlocked_writes_run_as_user_security_context(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(cri_stack, "ROOT", tmp_path)
    monkeypatch.setattr(cri_stack, "_find_pod", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cri_stack, "_ensure_image", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cri_stack, "_component_running_container", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cri_stack, "_record_event", lambda *_args, **_kwargs: None)

    def fake_crictl(args, check=True):
        if args[0] == "runp":
            return SimpleNamespace(stdout="pod-1\n", returncode=0)
        if args[0] == "create":
            return SimpleNamespace(stdout="ctr-1\n", returncode=0)
        if args[0] == "start":
            return SimpleNamespace(stdout="", returncode=0)
        raise AssertionError(args)

    monkeypatch.setattr(cri_stack, "_crictl", fake_crictl)

    cri_stack._start_component_unlocked(
        profile="k1s-core",
        component="k1s-core-envoy",
        image="localhost:5001/envoyproxy/envoy:v1.29-latest",
        command=["envoy", "-c", "/etc/envoy/envoy.yaml", "--log-level", "info"],
        resolve_image=False,
        run_as_user=0,
    )

    container_cfg = (
        tmp_path / "state" / "profiles" / "k1s-core" / "cri" / "k1s-core-envoy" / "container.json"
    )
    payload = json.loads(container_cfg.read_text(encoding="utf-8"))
    assert payload["linux"]["security_context"]["run_as_user"]["value"] == 0


def test_envoy_expected_endpoints_reads_listener_and_admin_ports(tmp_path) -> None:
    cfg = tmp_path / "envoy.yaml"
    cfg.write_text(
        """
static_resources:
  listeners:
    - name: edge_listener_http
      address:
        socket_address:
          address: 0.0.0.0
          port_value: 10080
    - name: edge_listener_tls
      address:
        socket_address:
          address: 127.0.0.1
          port_value: 10443
admin:
  address:
    socket_address:
      address: 127.0.0.1
      port_value: 9901
""",
        encoding="utf-8",
    )

    assert cri_stack._envoy_expected_endpoints(cfg) == [
        ("edge_listener_http", "127.0.0.1", 10080),
        ("edge_listener_tls", "127.0.0.1", 10443),
        ("admin", "127.0.0.1", 9901),
    ]


def test_ensure_envoy_ready_reports_missing_ports(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "envoy.yaml"
    cfg.write_text(
        """
static_resources:
  listeners:
    - name: edge_listener_tls
      address:
        socket_address:
          address: 0.0.0.0
          port_value: 10443
admin:
  address:
    socket_address:
      address: 127.0.0.1
      port_value: 9901
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cri_stack, "_tcp_port_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        cri_stack,
        "_component_status_summary",
        lambda profile, component: f"component={component} pod=pod-1 state=CONTAINER_EXITED",
    )
    monotonic = iter([0.0, 11.0])
    monkeypatch.setattr(cri_stack.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(cri_stack.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="envoy listeners not ready for k1s-core-envoy"):
        cri_stack._ensure_envoy_ready("k1s-core", "k1s-core-envoy", cfg)


def test_start_envoy_recreates_once_when_listener_check_fails(monkeypatch, tmp_path) -> None:
    recreates: list[bool] = []
    ready_calls = {"count": 0}

    def fake_start_component(**kwargs):
        recreates.append(bool(kwargs["recreate"]))

    def fake_ensure_ready(*_args, **_kwargs):
        ready_calls["count"] += 1
        if ready_calls["count"] == 1:
            raise RuntimeError("missing listener")

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    monkeypatch.setattr(cri_stack, "_ensure_envoy_ready", fake_ensure_ready)
    cfg = tmp_path / "envoy.yaml"
    cfg.write_text("static_resources: {}", encoding="utf-8")

    cri_stack._start_envoy("k1s-core", cfg, runtime_handler="runc")

    assert recreates == [False, True]


def test_component_running_container_ignores_non_component_containers(monkeypatch) -> None:
    def fake_list_containers():
        return [
            {
                "podSandboxId": "pod-1",
                "state": "CONTAINER_RUNNING",
                "labels": {},
            },
            {
                "podSandboxId": "pod-1",
                "state": "CONTAINER_RUNNING",
                "labels": {"ae.stack.component": "k1s-core-etcd"},
            },
        ]

    monkeypatch.setattr(cri_stack, "_list_containers", fake_list_containers)
    assert cri_stack._component_running_container("pod-1", "k1s-core-etcd")
    assert not cri_stack._component_running_container("pod-1", "k1s-core-postgres")


def test_resolve_image_ref_uses_configured_registry_and_namespace(monkeypatch) -> None:
    monkeypatch.setenv("AE_CRI_REGISTRY", "127.0.0.1:32000")
    monkeypatch.setenv("AE_CRI_REGISTRY_NAMESPACE", "k1s")
    assert (
        cri_stack._resolve_image_ref("docker.io/library/nats:2.10")
        == "127.0.0.1:32000/k1s/library/nats:2.10"
    )
    assert (
        cri_stack._resolve_image_ref("localhost/k1s-apishim:dev")
        == "127.0.0.1:32000/k1s/k1s-apishim:dev"
    )


def test_resolve_image_ref_registry_mode_off_skips_rewrite(monkeypatch) -> None:
    monkeypatch.setenv("AE_CRI_REGISTRY_MODE", "off")
    monkeypatch.setenv("AE_CRI_REGISTRY", "127.0.0.1:32000")
    assert (
        cri_stack._resolve_image_ref("docker.io/library/nats:2.10") == "docker.io/library/nats:2.10"
    )


def test_resolve_image_policy_defaults_prompt_in_tty(monkeypatch) -> None:
    monkeypatch.delenv("AE_CRI_IMAGE_POLICY", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(cri_stack, "_is_tty", lambda: True)
    assert cri_stack._resolve_image_policy() == "prompt"


def test_resolve_image_policy_defaults_fail_noninteractive(monkeypatch) -> None:
    monkeypatch.delenv("AE_CRI_IMAGE_POLICY", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(cri_stack, "_is_tty", lambda: False)
    assert cri_stack._resolve_image_policy() == "fail"


def test_build_command_available_for_apishim_registry_push_flow() -> None:
    cmd = cri_stack._build_command(
        "k1s-core-apishim",
        source_image="localhost/k1s-apishim:dev",
        target_image="localhost:32000/k1s-apishim:dev",
    )
    assert cmd is not None
    assert cmd[0] == "bash"
    assert cmd[1].endswith("scripts/build_cri_apishim_image.sh")
    assert "--image" in cmd
    assert "--push" in cmd
    assert "--pull-cri" in cmd


def test_build_command_available_for_non_apishim_mirror_flow() -> None:
    cmd = cri_stack._build_command(
        "k1s-core-etcd",
        source_image="quay.io/coreos/etcd:v3.5.13",
        target_image="localhost:32000/coreos/etcd:v3.5.13",
    )
    assert cmd is not None
    assert cmd[0] == "bash"
    assert cmd[1].endswith("scripts/dev/cri_image_mirror.sh")
    assert "--source" in cmd
    assert "--target" in cmd
    assert "--pull-cri" in cmd


def test_build_command_non_apishim_requires_distinct_source_and_target() -> None:
    cmd = cri_stack._build_command(
        "k1s-core-etcd",
        source_image="quay.io/coreos/etcd:v3.5.13",
        target_image="quay.io/coreos/etcd:v3.5.13",
    )
    assert cmd is None


def test_start_apishim_uses_expected_component_and_entrypoint(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    fresh_calls: list[tuple[str, str]] = []

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    def fake_ensure_apishim_image_fresh(profile: str, image: str) -> str:
        fresh_calls.append((profile, image))
        return "rollout-1234"

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    monkeypatch.setattr(cri_stack, "_ensure_apishim_image_fresh", fake_ensure_apishim_image_fresh)

    env_file = tmp_path / "apishim.env"
    env_file.write_text("AE_APISHIM_ALLOW_ANON=0\n", encoding="utf-8")
    cert_file = tmp_path / "apishim.crt"
    cert_file.write_text("crt", encoding="utf-8")
    key_file = tmp_path / "apishim.key"
    key_file.write_text("key", encoding="utf-8")

    cri_stack._start_apishim(
        "k1s-core",
        8445,
        "127.0.0.1",
        env_file,
        cert_file,
        key_file,
        runtime_handler="runc",
        recreate=True,
    )

    assert captured["component"] == "k1s-core-apishim"
    assert captured["runtime_handler"] == "runc"
    assert captured["recreate"] is True
    assert captured["rollout_key"] == "rollout-1234"
    assert captured["stable_seconds"] == 2
    assert captured["command"] == [
        "python",
        "-m",
        "ae.apishim",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8445",
        "--tls",
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["AE_APISHIM_ALLOW_ANON"] == "0"
    assert env["AE_APISHIM_ENABLE"] == "1"
    assert env["AE_APISHIM_SERVER"] == "https://127.0.0.1:8445"
    assert fresh_calls == [("k1s-core", "localhost/k1s-apishim:dev")]


def test_ensure_apishim_image_fresh_rebuilds_and_records_stamp(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cri_stack, "ROOT", tmp_path)
    monkeypatch.setattr(cri_stack, "_resolve_image_ref", lambda image: image)
    monkeypatch.setattr(cri_stack, "_apishim_rollout_key", lambda _image: "rollout-5678")
    monkeypatch.setattr(cri_stack, "_image_exists", lambda _image: False)
    monkeypatch.setattr(
        cri_stack,
        "_build_command",
        lambda component, *, source_image, target_image: [
            "bash",
            "build.sh",
            component,
            source_image,
            target_image,
        ],
    )
    run_cmds: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        run_cmds.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(cri_stack, "_run", fake_run)

    rollout_key = cri_stack._ensure_apishim_image_fresh(
        "k1s-core",
        "localhost:5001/k1s-apishim:dev",
    )

    assert rollout_key == "rollout-5678"
    assert run_cmds == [
        [
            "bash",
            "build.sh",
            "k1s-core-apishim",
            "localhost:5001/k1s-apishim:dev",
            "localhost:5001/k1s-apishim:dev",
        ]
    ]
    stamp_path = (
        tmp_path
        / "state"
        / "profiles"
        / "k1s-core"
        / "cri"
        / "k1s-core-apishim"
        / "image-build.json"
    )
    payload = json.loads(stamp_path.read_text(encoding="utf-8"))
    assert payload["image"] == "localhost:5001/k1s-apishim:dev"
    assert payload["rollout_key"] == "rollout-5678"


def test_start_registry_uses_upstream_image_without_registry_rewrite(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    cri_stack._start_registry(
        "k1s-core",
        "127.0.0.1",
        5001,
        runtime_handler="runc",
        recreate=False,
    )

    assert captured["component"] == "k1s-core-registry"
    assert captured["image"] == "docker.io/library/registry:2"
    assert captured["resolve_image"] is False
    assert captured["runtime_handler"] == "runc"
    env = captured["env"]
    assert env["REGISTRY_HTTP_ADDR"] == "127.0.0.1:5001"


def test_up_postgres_parser_invokes_reset_data(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_check_ready() -> None:
        return None

    def fake_start_postgres(
        profile: str,
        *,
        runtime_handler: str | None = None,
        recreate: bool = False,
        reset_data: bool = False,
    ) -> None:
        captured["profile"] = profile
        captured["runtime_handler"] = runtime_handler
        captured["recreate"] = recreate
        captured["reset_data"] = reset_data

    monkeypatch.setattr(cri_stack, "_check_ready", fake_check_ready)
    monkeypatch.setattr(cri_stack, "_start_postgres", fake_start_postgres)

    rc = cri_stack.main(["up-postgres", "--profile", "k1s-core", "--recreate", "--reset-data"])

    assert rc == 0
    assert captured == {
        "profile": "k1s-core",
        "runtime_handler": "runc",
        "recreate": True,
        "reset_data": True,
    }


def test_ensure_image_fail_policy_errors_without_pull(monkeypatch) -> None:
    monkeypatch.setenv("AE_CRI_IMAGE_POLICY", "fail")
    monkeypatch.setattr(cri_stack, "_image_exists", lambda _image: False)
    with pytest.raises(RuntimeError, match="required image is missing"):
        cri_stack._ensure_image("localhost/k1s-apishim:dev", "k1s-core-apishim")


def test_ensure_image_build_choice_runs_local_build_push(monkeypatch) -> None:
    app_image = "localhost/k1s-apishim:dev"
    pull_calls: list[str] = []
    state = {"app_built": False}

    monkeypatch.setenv("AE_CRI_IMAGE_POLICY", "prompt")
    monkeypatch.setattr(cri_stack, "_noninteractive", lambda: False)

    def fake_image_exists(image: str) -> bool:
        if image == app_image:
            return state["app_built"]
        return False

    monkeypatch.setattr(cri_stack, "_image_exists", fake_image_exists)

    def fake_pull(image: str) -> tuple[bool, str]:
        pull_calls.append(image)
        if image == app_image:
            return False, "missing"
        return False, "unknown"

    monkeypatch.setattr(cri_stack, "_pull_image", fake_pull)

    def fake_build_cmd(_component: str, *, source_image: str, target_image: str) -> list[str]:
        return ["bash", "build.sh", source_image, target_image]

    monkeypatch.setattr(
        cri_stack,
        "_build_command",
        fake_build_cmd,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "b")

    def fake_run(_cmd, **_kwargs):
        state["app_built"] = True

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(cri_stack, "_run", fake_run)

    cri_stack._ensure_image(app_image, "k1s-core-apishim")

    assert pull_calls == [app_image]


def test_ensure_image_non_apishim_build_choice_runs_mirror(monkeypatch) -> None:
    source_image = "quay.io/coreos/etcd:v3.5.13"
    target_image = "localhost:32000/coreos/etcd:v3.5.13"
    pull_calls: list[str] = []
    run_cmds: list[list[str]] = []
    state = {"mirrored": False}

    monkeypatch.setenv("AE_CRI_IMAGE_POLICY", "prompt")
    monkeypatch.setattr(cri_stack, "_noninteractive", lambda: False)

    def fake_image_exists(image: str) -> bool:
        if image == target_image:
            return state["mirrored"]
        return False

    monkeypatch.setattr(cri_stack, "_image_exists", fake_image_exists)

    def fake_pull(image: str) -> tuple[bool, str]:
        pull_calls.append(image)
        return False, "missing"

    monkeypatch.setattr(cri_stack, "_pull_image", fake_pull)

    def fake_build_cmd(_component: str, *, source_image: str, target_image: str) -> list[str]:
        return ["bash", "mirror.sh", source_image, target_image]

    monkeypatch.setattr(cri_stack, "_build_command", fake_build_cmd)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "b")

    def fake_run(cmd, **_kwargs):
        run_cmds.append(list(cmd))
        state["mirrored"] = True

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(cri_stack, "_run", fake_run)

    cri_stack._ensure_image(target_image, "k1s-core-etcd", source_image=source_image)

    assert pull_calls == [target_image]
    assert run_cmds == [["bash", "mirror.sh", source_image, target_image]]
