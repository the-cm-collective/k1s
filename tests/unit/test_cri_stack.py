from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

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


def test_start_rathole_server_uses_image_entrypoint_args(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    cri_stack._start_rathole_server("k1s-core", Path("server.toml"), runtime_handler="runc")

    assert captured["runtime_handler"] == "runc"
    assert captured.get("command") is None
    assert captured["args"] == ["--server", "/etc/rathole/server.toml"]


def test_start_rathole_client_uses_image_entrypoint_args(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    cri_stack._start_rathole_client(
        "k1s-edge",
        "sea-edge-02",
        "edge-1",
        Path("client.toml"),
        runtime_handler="runc",
    )

    assert captured["runtime_handler"] == "runc"
    assert captured.get("command") is None
    assert captured["args"] == ["--client", "/etc/rathole/client.toml"]


def test_start_envoy_mounts_state_paths_for_tls_resolution(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
    monkeypatch.delenv("AE_TLS_DIR", raising=False)
    cfg = tmp_path / "envoy.yaml"
    cfg.write_text("static_resources: {}", encoding="utf-8")

    cri_stack._start_envoy("k1s-core", cfg, runtime_handler="runc")

    mounts = captured["mounts"]
    assert isinstance(mounts, list)
    state_dir = str((cri_stack.ROOT / "state").resolve())
    assert {"host_path": str(cfg), "container_path": "/etc/envoy/envoy.yaml", "readonly": True} in mounts
    assert {"host_path": state_dir, "container_path": "/state", "readonly": True} in mounts
    assert {"host_path": state_dir, "container_path": state_dir, "readonly": True} in mounts


def test_start_envoy_mounts_external_absolute_tls_dir(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)
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
        cri_stack._resolve_image_ref("docker.io/library/nats:2.10")
        == "docker.io/library/nats:2.10"
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

    def fake_start_component(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cri_stack, "_start_component", fake_start_component)

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
