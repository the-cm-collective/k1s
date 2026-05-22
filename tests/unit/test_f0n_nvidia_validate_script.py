from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "f0n_nvidia_validate.py"
_SPEC = spec_from_file_location("f0n_nvidia_validate_script", SCRIPT)
f0n_nvidia_validate = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = f0n_nvidia_validate
_SPEC.loader.exec_module(f0n_nvidia_validate)


def _seed_image_groups_from_refs(groups: dict[str, list[str]]) -> dict[str, list[f0n_nvidia_validate.SeedImageSpec]]:
    return {
        section: [
            f0n_nvidia_validate.SeedImageSpec(ref=f0n_nvidia_validate._normalize_image_ref(ref))
            for ref in refs
        ]
        for section, refs in groups.items()
    }


def _fake_image_id(ref: str) -> str:
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _write_fake_seed_metadata(*, bundle: Path, manifest: Path, metadata_only: bool) -> dict[str, str]:
    groups = f0n_nvidia_validate._seed_manifest_image_groups(manifest)
    images = f0n_nvidia_validate._required_seed_images(groups)
    expected = {image.ref: _fake_image_id(image.ref) for image in images}
    info_path = bundle.parent / "cri-seed-info.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(
        json.dumps(
            {
                "bundle": str(bundle),
                "metadata_only": metadata_only,
                "image_refs": [image.ref for image in images],
                "images": [
                    {
                        "ref": image.ref,
                        "expected_image_id": expected[image.ref],
                    }
                    for image in images
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if not metadata_only:
        bundle.write_bytes(b"fake-seed-bundle")
    return expected


def _write_fake_seed_metadata_with_build_override(
    *,
    bundle: Path,
    manifest: Path,
    metadata_only: bool,
    build_suffix: str,
) -> dict[str, str]:
    groups = f0n_nvidia_validate._seed_manifest_image_groups(manifest)
    images = f0n_nvidia_validate._required_seed_images(groups)
    expected = {
        image.ref: _fake_image_id(image.ref if metadata_only else f"{image.ref}:{build_suffix}")
        for image in images
    }
    info_path = bundle.parent / "cri-seed-info.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(
        json.dumps(
            {
                "bundle": str(bundle),
                "metadata_only": metadata_only,
                "image_refs": [image.ref for image in images],
                "images": [
                    {
                        "ref": image.ref,
                        "expected_image_id": expected[image.ref],
                    }
                    for image in images
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if not metadata_only:
        bundle.write_bytes(b"fake-seed-bundle")
    return expected


def test_f0n_validate_plan_emits_stable_artifact_shape(tmp_path) -> None:
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "plan",
            "--run-id",
            "review-run",
            "--runs-dir",
            str(tmp_path),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["run_id"] == "review-run"
    assert payload["inventory"]["nodes"].endswith("review-run/ae/nodes.json")
    assert payload["inventory"]["plan"].endswith("review-run/plan.json")
    assert payload["inventory"]["summary"].endswith("review-run/summary.json")
    assert payload["execution_hosts"][0]["execution_model"] == "linux_guest_passthrough"
    assert payload["execution_hosts"][1]["execution_model"] == "host_native"
    assert payload["checks"]["egpu_attach"].endswith("review-run/checks/egpu_attach.json")
    assert payload["checks"]["egpu_cri_runtime"].endswith(
        "review-run/checks/egpu_cri_runtime.json"
    )
    assert payload["checks"]["egpu_compute_smoke"].endswith(
        "review-run/checks/egpu_compute_smoke.json"
    )
    assert [phase["name"] for phase in payload["phases"]] == [
        "egpu_passthrough_validate",
        "cell_validation",
    ]
    assert [cell["name"] for cell in payload["cells"]] == [
        "cell-a-single",
        "cell-b-single",
        "cell-ab-pp2-ray",
        "cell-ab-pp2-mp",
    ]
    for cell in payload["cells"]:
        artifacts = cell["artifacts"]
        assert artifacts["rendered_manifest"].endswith("manifest-rendered.yaml")
        assert artifacts["status_initial"].endswith("status-initial.json")
        assert artifacts["join_debug_initial"].endswith("join-debug-initial.json")
        assert artifacts["events_initial"].endswith("events-initial.txt")
        assert artifacts["api_probe_initial"].endswith("api-probe-initial.json")
        assert artifacts["status_reapplied"].endswith("status-reapplied.json")
        assert artifacts["join_debug_reapplied"].endswith("join-debug-reapplied.json")
        assert artifacts["api_probe_reapplied"].endswith("api-probe-reapplied.json")
        assert artifacts["teardown"].endswith("teardown.txt")
    assert payload["artifacts"]["model_bootstrap"].endswith("review-run/ae/model-bootstrap.json")
    assert payload["artifacts"]["vllm_image_probe"].endswith("review-run/ae/vllm-image-probe.json")
    assert payload["artifacts"]["vllm_image_probe_transcript"].endswith(
        "review-run/ae/vllm-image-probe.transcript.txt"
    )
    assert payload["artifacts"]["vllm_startup_probe"].endswith(
        "review-run/ae/vllm-startup-probe.json"
    )
    assert payload["artifacts"]["vllm_startup_probe_transcript"].endswith(
        "review-run/ae/vllm-startup-probe.transcript.txt"
    )


def test_f0n_validate_plan_can_limit_to_host_a_lane(tmp_path) -> None:
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "plan",
            "--run-id",
            "host-a-only",
            "--runs-dir",
            str(tmp_path),
            "--cell-lane",
            "cell-a-single",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert [cell["name"] for cell in payload["cells"]] == ["cell-a-single"]
    assert payload["phases"][1]["cell_count"] == 1


def test_ae_env_loads_controller_state_profile_when_requested(tmp_path, monkeypatch) -> None:
    controller_env = tmp_path / "controller.env"
    controller_env.write_text(
        "\n".join(
            [
                "AE_STATE_BACKEND=etcd",
                "AE_ETCD_ENDPOINTS=http://127.0.0.1:2379",
                "AE_ETCD_PREFIX=k1s/profiles/k1s-core",
                "AE_SITE_ID=core",
            ]
        ),
        encoding="utf-8",
    )
    for key in (
        "AE_STATE_BACKEND",
        "AE_ETCD_ENDPOINTS",
        "AE_ETCD_PREFIX",
        "AE_SITE_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    env = f0n_nvidia_validate._ae_env(controller_env)

    assert env["AE_STATE_BACKEND"] == "etcd"
    assert env["AE_ETCD_ENDPOINTS"] == "http://127.0.0.1:2379"
    assert env["AE_ETCD_PREFIX"] == "k1s/profiles/k1s-core"
    assert env["AE_SITE_ID"] == "core"
    assert env["AE_INFERENCE_EXPERIMENTAL"] == "1"
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT / "src")


def test_validation_seed_groups_include_bootstrap_compute_and_rendered_images(tmp_path: Path) -> None:
    plan = f0n_nvidia_validate.build_plan(
        run_id="validation-seed",
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )
    f0n_nvidia_validate._prepare_rendered_manifests(
        plan=plan,
        test_model=f0n_nvidia_validate.TestModelSpec(
            model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
            revision=None,
            local_path="/models/smollm2-1.7b-instruct",
        ),
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
    )
    args = argparse.Namespace(
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
        guest_ip="",
        vm_name="",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        run_id="validation-seed",
        runs_dir=tmp_path,
    )

    groups = f0n_nvidia_validate._validation_seed_image_groups(plan=plan, args=args)
    bootstrap_refs = [image.ref for image in groups["bootstrap"]]
    core_refs = [image.ref for image in groups["core"]]
    edge_refs = [image.ref for image in groups["edge"]]

    assert "docker.io/library/busybox:1.36" in bootstrap_refs
    assert "registry.k8s.io/pause:3.9" in bootstrap_refs
    assert f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE in core_refs
    assert "docker.io/rayproject/ray:latest" in core_refs
    assert f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE in core_refs
    assert edge_refs == []


def test_normalize_image_ref_adds_docker_host_defaults() -> None:
    assert f0n_nvidia_validate._normalize_image_ref("busybox:1.36") == "docker.io/library/busybox:1.36"
    assert f0n_nvidia_validate._normalize_image_ref("rayproject/ray:latest") == "docker.io/rayproject/ray:latest"
    assert f0n_nvidia_validate._normalize_image_ref("docker.io/vllm/vllm-openai:v0.6.2") == (
        "docker.io/vllm/vllm-openai:v0.6.2"
    )
    assert (
        f0n_nvidia_validate._normalize_image_ref("docker.io/library/k1s-vllm-openai:host-a-cu121-v2")
        == "docker.io/library/k1s-vllm-openai:host-a-cu121-v2"
    )


def test_normalize_image_id_canonicalizes_sha256_forms() -> None:
    digest = "A1" * 32
    assert f0n_nvidia_validate._normalize_image_id(digest) == f"sha256:{digest.casefold()}"
    assert f0n_nvidia_validate._normalize_image_id(f"sha256:{digest}") == (
        f"sha256:{digest.casefold()}"
    )
    assert f0n_nvidia_validate._normalize_image_id("not-a-digest") is None


def test_classify_guest_seed_images_treats_prefixed_and_unprefixed_digest_as_fresh() -> None:
    ref = "docker.io/library/k1s-vllm-openai:host-a-cu121-v2"
    digest = "ab" * 32
    required = [f0n_nvidia_validate.SeedImageSpec(ref=ref, expected_image_id=digest)]
    guest_states = {
        ref: {
            "present": True,
            "image_id": f"sha256:{digest}",
            "detail": "",
        }
    }

    missing, stale, fresh = f0n_nvidia_validate._classify_guest_seed_images(required, guest_states)

    assert missing == []
    assert stale == []
    assert fresh == [ref]
    assert f0n_nvidia_validate._image_id_matches_expected(
        guest_image_id=f"sha256:{digest}",
        expected_image_id=digest,
    )


def test_cleanup_guest_seed_staging_removes_obsolete_vllm_tags(monkeypatch) -> None:
    remote_commands: list[str] = []

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        remote_commands.append(command)
        return subprocess.CompletedProcess(["ssh"], 0, "cleanup-ok", "")

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    config = argparse.Namespace(
        run_id="cleanup",
        runs_dir=ROOT / "runs",
        guest_ip="192.0.2.10",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )

    result = f0n_nvidia_validate._cleanup_guest_seed_staging(
        config=f0n_nvidia_validate.egpu_validate.make_config(config),
        guest_ip="192.0.2.10",
        before_refs={
            "docker.io/vllm/vllm-openai:latest",
            f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        },
        required_images=[
            "docker.io/rayproject/ray:latest",
            f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        ],
    )

    assert result["status"] == "ok"
    assert result["removed_images"] == ["docker.io/vllm/vllm-openai:latest"]
    assert len(remote_commands) == 1
    assert "rm -f /tmp/*-cri-seed-images.oci.tar" in remote_commands[0]
    assert "crictl ps -a --image docker.io/vllm/vllm-openai:latest -q" in remote_commands[0]
    assert "ctr -n k8s.io images rm --sync docker.io/vllm/vllm-openai:latest" in remote_commands[0]


def test_cleanup_guest_seed_staging_fails_when_free_space_stays_below_requirement(monkeypatch) -> None:
    remote_commands: list[str] = []

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        remote_commands.append(command)
        return subprocess.CompletedProcess(
            ["ssh"],
            0,
            "__seed_cleanup__ avail_before=1024 avail_after=2048 purged_namespace=1\ncleanup-ok",
            "",
        )

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    config = argparse.Namespace(
        run_id="cleanup-low-space",
        runs_dir=ROOT / "runs",
        guest_ip="192.0.2.10",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )

    result = f0n_nvidia_validate._cleanup_guest_seed_staging(
        config=f0n_nvidia_validate.egpu_validate.make_config(config),
        guest_ip="192.0.2.10",
        before_refs={f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE},
        required_images=[f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE],
        min_free_bytes=4096,
    )

    assert result["status"] == "failed"
    assert result["purged_namespace"] is True
    assert result["avail_before_bytes"] == 1024
    assert result["avail_after_bytes"] == 2048
    assert result["required_free_bytes"] == 4096
    assert len(remote_commands) == 1
    assert "crictl ps -a -q" in remote_commands[0]
    assert "ctr -n k8s.io images ls -q" in remote_commands[0]


def test_classify_join_debug_prefers_guest_local_reachability() -> None:
    classification = f0n_nvidia_validate._classify_join_debug(
        controller_probe={"ok": False},
        guest_snapshot={
            "loopback_health": {"ok": True},
            "guest_ip_health": {"ok": True},
            "ss_18080": {"detail": "LISTEN 0 4096 0.0.0.0:18080"},
        },
        launcher_logs="",
        head_logs="",
    )

    assert classification == "listener_reachable_guest_local_only"


def test_classify_join_debug_prefers_in_pod_listener_when_host_unreachable() -> None:
    classification = f0n_nvidia_validate._classify_join_debug(
        controller_probe={"ok": False},
        guest_snapshot={
            "loopback_health": {"ok": False},
            "guest_ip_health": {"ok": False},
            "ss_18080": {"detail": ""},
            "workload_probes": {
                "ray-launcher": {
                    "loopback_health": {"ok": True},
                }
            },
        },
        launcher_logs="",
        head_logs="",
    )

    assert classification == "listener_in_pod_only"


def test_classify_join_debug_detects_launcher_failure_from_logs() -> None:
    classification = f0n_nvidia_validate._classify_join_debug(
        controller_probe={"ok": False},
        guest_snapshot={
            "loopback_health": {"ok": False},
            "guest_ip_health": {"ok": False},
            "ss_18080": {"detail": ""},
        },
        launcher_logs="Traceback (most recent call last): RuntimeError: startup failed",
        head_logs="",
    )

    assert classification == "launcher_failed"


def test_node_containers_payload_filters_to_join_workloads(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "containers": [
                    {
                        "name": "cell-a-single-ray-launcher-rev1-0",
                        "labels": {
                            "ae.pod_name": "cell-a-single-ray-launcher-rev1-0",
                            "ae.app": "cell-a-single-ray-launcher",
                        },
                        "uid": "launcher-uid",
                        "host_ports": [18080],
                        "port_map": {"18080": 18080},
                        "host_ip": "192.0.2.10",
                        "restart_count": 0,
                        "started_at": "2026-05-05T22:09:44Z",
                        "running": True,
                        "pod_ip": "10.42.0.10",
                    },
                    {
                        "name": "cell-a-single-ray-head-rev1-0",
                        "labels": {
                            "ae.pod_name": "cell-a-single-ray-head-rev1-0",
                            "ae.app": "cell-a-single-ray-head",
                        },
                        "uid": "head-uid",
                        "host_ports": [],
                        "port_map": {},
                        "host_ip": "192.0.2.10",
                        "restart_count": 0,
                        "started_at": "2026-05-05T22:09:43Z",
                        "running": True,
                        "pod_ip": "10.42.0.11",
                    },
                    {
                        "name": "unrelated",
                        "labels": {"ae.app": "busybox-demo"},
                        "uid": "other-uid",
                        "host_ports": [],
                        "port_map": {},
                        "host_ip": "192.0.2.10",
                        "restart_count": 0,
                        "started_at": "2026-05-05T22:09:42Z",
                        "running": True,
                        "pod_ip": "10.42.0.12",
                    },
                ]
            }

    monkeypatch.setattr(f0n_nvidia_validate.requests, "get", lambda url, timeout: FakeResponse())

    workloads = [
        {
            "role": "ray-head",
            "app_name": "cell-a-single-ray-head",
            "pod_states": [{"pod_name": "cell-a-single-ray-head-rev1-0"}],
        },
        {
            "role": "ray-launcher",
            "app_name": "cell-a-single-ray-launcher",
            "pod_states": [{"pod_name": "cell-a-single-ray-launcher-rev1-0"}],
        },
    ]

    payload = f0n_nvidia_validate._node_containers_payload(
        guest_ip="192.0.2.10",
        workloads=workloads,
    )

    assert payload["ok"] is True
    assert payload["all_count"] == 3
    assert [item["role"] for item in payload["containers"]] == ["ray-launcher", "ray-head"]
    assert payload["containers"][0]["pod_ip"] == "10.42.0.10"
    assert payload["containers"][1]["pod_ip"] == "10.42.0.11"


def test_guest_join_debug_snapshot_records_workload_probes_and_network_state(
    tmp_path: Path, monkeypatch
) -> None:
    guest_commands: list[str] = []

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        guest_commands.append(command)
        payload = {
            "loopback_health": {"ok": False},
            "guest_ip_health": {"ok": False},
            "ss_18080": {"detail": ""},
            "crictl_ps": {"detail": "ps"},
            "ip_route": {"detail": "default via 192.0.2.1 dev lan0"},
            "ip_addr_ae0": {"detail": "ae0"},
            "ip_addr_cni0": {"detail": "cni0"},
            "hostport_nat_18080": {"detail": "-A CNI-HOSTPORT-DNAT -p tcp --dport 18080"},
            "route_to_launcher_pod": {"detail": "10.42.0.10 dev cni0"},
            "workload_probes": {
                "ray-launcher": {
                    "loopback_health": {"ok": True},
                    "ss": {"detail": "LISTEN 0 4096 0.0.0.0:18080"},
                    "env": {"detail": "API_PORT=18080"},
                },
                "ray-head": {
                    "env": {"detail": "MASTER_ADDR=10.250.0.10\nFABRIC_IP=10.250.0.10"},
                    "ss": {"detail": "LISTEN 0 4096 10.250.0.10:6379"},
                },
            },
        }
        return subprocess.CompletedProcess(["ssh"], 0, json.dumps(payload), "")

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    config = f0n_nvidia_validate.egpu_validate.ValidationConfig(
        run_id="join-debug",
        runs_dir=tmp_path,
        guest_ip="192.0.2.10",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo="/home/ae/k1s",
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id=None,
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        compute_success_signal=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_SUCCESS_SIGNAL,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )

    workloads = [
        {
            "role": "ray-head",
            "app_name": "cell-a-single-ray-head",
            "pod_states": [{"pod_name": "cell-a-single-ray-head-rev1-0"}],
        },
        {
            "role": "ray-launcher",
            "app_name": "cell-a-single-ray-launcher",
            "pod_states": [{"pod_name": "cell-a-single-ray-launcher-rev1-0"}],
        },
    ]

    payload = f0n_nvidia_validate._guest_join_debug_snapshot(
        config=config,
        guest_ip="192.0.2.10",
        api_endpoint="192.0.2.10:18080",
        workloads=workloads,
        master_addr="10.250.0.10",
        master_port=6379,
        node_containers={
            "containers": [
                {"role": "ray-launcher", "pod_ip": "10.42.0.10"},
                {"role": "ray-head", "pod_ip": "10.42.0.11"},
            ]
        },
    )

    assert payload["status"] == "ok"
    assert payload["workload_probes"]["ray-launcher"]["loopback_health"]["ok"] is True
    assert payload["route_to_launcher_pod"]["detail"] == "10.42.0.10 dev cni0"
    assert "LAUNCHER_POD_IP=10.42.0.10" in guest_commands[0]
    assert "MASTER_ADDR=10.250.0.10" in guest_commands[0]
    assert "WORKLOAD_SPECS=" in guest_commands[0]


def test_write_join_debug_artifact_uses_cri_fallback_and_records_guest_debug(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_command_capture(*, cmd, env, truncate=12000):  # type: ignore[no-untyped-def]
        name = cmd[-3]
        return {
            "command": " ".join(cmd),
            "returncode": 1,
            "detail": f"No status recorded for default/{name}",
        }

    monkeypatch.setattr(f0n_nvidia_validate, "_command_capture_payload", fake_command_capture)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_guest_cri_log_capture_payload",
        lambda **kwargs: {
            "command": "sudo crictl logs --tail 200 abc123",
            "returncode": 0,
            "detail": f"cri logs for {kwargs['workload']['role']}",
            "container_id": "abc123",
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_controller_health_probe_payload",
        lambda api_endpoint: {"api_endpoint": api_endpoint, "health_url": "http://x/health", "ok": False},
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_node_containers_payload",
        lambda **kwargs: {
            "ok": True,
            "containers": [
                {
                    "role": "ray-launcher",
                    "app_name": "cell-a-single-ray-launcher",
                    "pod_ip": "10.42.0.233",
                    "host_ports": [18080],
                },
                {
                    "role": "ray-head",
                    "app_name": "cell-a-single-ray-head",
                    "pod_ip": "10.42.0.232",
                    "host_ports": [],
                },
            ],
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_guest_join_debug_snapshot",
        lambda **kwargs: {
            "loopback_health": {"ok": False},
            "guest_ip_health": {"ok": False},
            "ss_18080": {"detail": ""},
            "workload_probes": {
                "ray-launcher": {
                    "loopback_health": {"ok": True},
                }
            },
        },
    )

    config = f0n_nvidia_validate.egpu_validate.ValidationConfig(
        run_id="join-debug",
        runs_dir=tmp_path,
        guest_ip="192.0.2.10",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo="/home/ae/k1s",
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id=None,
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        compute_success_signal=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_SUCCESS_SIGNAL,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )

    status_payload = {
        "phase": "JOINING",
        "last_error": "JOIN_API_NOT_READY",
        "allocations": {
            "api_endpoint": "192.0.2.10:18080",
            "master_addr": "10.250.0.10",
            "master_port": 6379,
            "execution": {
                "workloads": [
                    {
                        "role": "ray-head",
                        "app_name": "cell-a-single-ray-head",
                        "pod_states": [{"pod_name": "cell-a-single-ray-head-rev1-0"}],
                    },
                    {
                        "role": "ray-launcher",
                        "app_name": "cell-a-single-ray-launcher",
                        "pod_states": [{"pod_name": "cell-a-single-ray-launcher-rev1-0"}],
                    },
                ]
            },
        },
    }

    path = tmp_path / "join-debug.json"
    f0n_nvidia_validate._write_join_debug_artifact(
        ae=["ae"],
        env={},
        cell_name="cell-a-single",
        status_payload=status_payload,
        path=path,
        join_debug_context={"config": config, "guest_ip": "192.0.2.10"},
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["node_containers"]["containers"][0]["pod_ip"] == "10.42.0.233"
    assert payload["workload_logs"]["ray-launcher"]["cri_fallback"]["detail"] == (
        "cri logs for ray-launcher"
    )
    assert payload["workload_logs"]["ray-head"]["cri_fallback"]["detail"] == "cri logs for ray-head"
    assert payload["classification"] == "listener_in_pod_only"


def test_wait_for_cell_ready_writes_join_debug_on_timeout(tmp_path, monkeypatch) -> None:
    join_debug_calls: list[dict] = []

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        if "status" in cmd:
            payload = {"phase": "JOINING", "allocations": {}, "last_error": "JOIN_API_NOT_READY"}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monotonic_values = iter([0.0, 0.5, 2.0])

    def fake_monotonic() -> float:
        return next(monotonic_values)

    def fake_join_debug(**kwargs):  # type: ignore[no-untyped-def]
        join_debug_calls.append(kwargs)
        kwargs["path"].write_text(json.dumps({"status": "timeout"}), encoding="utf-8")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
    monkeypatch.setattr(f0n_nvidia_validate.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(f0n_nvidia_validate.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(f0n_nvidia_validate, "_write_join_debug_artifact", fake_join_debug)

    join_debug_path = tmp_path / "join-debug.json"
    with pytest.raises(SystemExit, match="did not reach READY"):
        f0n_nvidia_validate._wait_for_cell_ready(
            ae=["ae"],
            manifest="spec.yaml",
            name="cell-a-single",
            apply_path=tmp_path / "apply.txt",
            status_path=tmp_path / "status.json",
            env={},
            timeout_s=1,
            poll_interval_s=0.1,
            join_debug_path=join_debug_path,
            join_debug_context={"config": None, "guest_ip": ""},
        )

    assert len(join_debug_calls) == 1
    assert join_debug_path.is_file()


def test_wait_for_cell_ready_writes_join_debug_on_failed_phase(tmp_path, monkeypatch) -> None:
    join_debug_calls: list[dict] = []

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        if "status" in cmd:
            payload = {"phase": "FAILED", "allocations": {}, "last_error": "JOIN_FAILED"}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    def fake_join_debug(**kwargs):  # type: ignore[no-untyped-def]
        join_debug_calls.append(kwargs)
        kwargs["path"].write_text(json.dumps({"status": "failed"}), encoding="utf-8")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
    monkeypatch.setattr(f0n_nvidia_validate.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(f0n_nvidia_validate, "_write_join_debug_artifact", fake_join_debug)

    join_debug_path = tmp_path / "join-debug-failed.json"
    with pytest.raises(SystemExit, match="entered FAILED phase"):
        f0n_nvidia_validate._wait_for_cell_ready(
            ae=["ae"],
            manifest="spec.yaml",
            name="cell-a-single",
            apply_path=tmp_path / "apply.txt",
            status_path=tmp_path / "status.json",
            env={},
            timeout_s=30,
            poll_interval_s=0.1,
            join_debug_path=join_debug_path,
            join_debug_context={"config": None, "guest_ip": ""},
        )

    assert len(join_debug_calls) == 1
    assert join_debug_path.is_file()


def test_cell_lanes_require_host_a_seed_only_for_host_a_lanes(tmp_path) -> None:
    host_a_plan = f0n_nvidia_validate.build_plan(
        run_id="host-a-seed",
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )
    host_b_plan = f0n_nvidia_validate.build_plan(
        run_id="host-b-only",
        runs_dir=tmp_path,
        cell_lane_names=["cell-b-single"],
    )

    assert f0n_nvidia_validate._cell_lanes_require_host_a_seed(host_a_plan) is True
    assert f0n_nvidia_validate._cell_lanes_require_host_a_seed(host_b_plan) is False


def test_validation_seed_cache_copies_bundle_to_host_a_guest(tmp_path, monkeypatch) -> None:
    run_id = "host-a-validation-seed"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "validation-seed-cache.json"
    raw_image_groups = {
        "bootstrap": [
            "docker.io/library/busybox:1.36",
            "registry.k8s.io/pause:3.9",
        ],
        "core": [
            f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
            "docker.io/rayproject/ray:latest",
            f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        ],
        "edge": [],
    }
    image_groups = _seed_image_groups_from_refs(raw_image_groups)
    required_images = f0n_nvidia_validate._required_seed_images(image_groups)
    required_refs = [image.ref for image in required_images]
    run_commands: list[list[str]] = []
    guest_commands: list[str] = []
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"
    seed_run_id = f"{run_id}-validation-seed"
    bundle = tmp_path / "state" / "lab-vm" / seed_run_id / "seeds" / "cri-seed-images.oci.tar"
    expected_image_ids: dict[str, str] = {}

    guest_image_state_calls = {"count": 0}

    def fake_guest_image_states(*, config, guest_ip, image_refs):  # type: ignore[no-untyped-def]
        guest_image_state_calls["count"] += 1
        if guest_image_state_calls["count"] == 1:
            return {
                ref: {"present": False, "image_id": None, "detail": "not found"}
                for ref in image_refs
            }
        return {
            ref: {"present": True, "image_id": expected_image_ids[ref], "detail": ""}
            for ref in image_refs
        }

    monkeypatch.setattr(f0n_nvidia_validate, "_guest_image_states", fake_guest_image_states)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_validation_seed_image_groups",
        lambda *, plan, args: {
            section: list(refs) for section, refs in image_groups.items()
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_seed_bundle_paths",
        lambda run_id, label="core-seed": (seed_run_id, bundle),
    )

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        run_commands.append(cmd)
        if cmd[0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]:
            manifest = Path(cmd[cmd.index("--manifest") + 1])
            metadata_only = "--metadata-only" in cmd
            expected_image_ids.update(
                _write_fake_seed_metadata(bundle=bundle, manifest=manifest, metadata_only=metadata_only)
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_cleanup_guest_seed_staging",
        lambda **kwargs: {
            "status": "ok",
            "detail": "",
            "removed_images": [],
            "purged_namespace": False,
            "avail_before_bytes": None,
            "avail_after_bytes": None,
            "required_free_bytes": kwargs.get("min_free_bytes"),
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": inventory_path,
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        guest_commands.append(command)
        return subprocess.CompletedProcess(["ssh"], 0, "", "")

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )

    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )
    f0n_nvidia_validate._ensure_guest_validation_seed_cache(
        args=args,
        run_root=run_root,
        plan=plan,
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["missing_before"] == required_refs
    assert payload["stale_before"] == []
    assert payload["fresh_before"] == []
    assert payload["missing_after"] == []
    assert payload["stale_after"] == []
    assert payload["fresh_after"] == required_refs
    assert payload["cleanup"]["status"] == "ok"
    assert payload["guest_bundle"] == f"/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar"
    assert payload["guest_bundle"].startswith("/tmp/")
    assert payload["manifest"].endswith("host-a-validation-seed/ae/validation-seed-manifest.json")
    assert isinstance(payload["import_duration_ms"], int)
    assert payload["selected_vllm_image"] == f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
    assert payload["selected_vllm_image_present_after_import"] is True
    assert payload["selected_vllm_image_matches_expected_after_import"] is True
    assert len(run_commands) == 3
    assert run_commands[0][0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]
    assert "--metadata-only" in run_commands[0]
    assert run_commands[1][0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]
    assert "--output" in run_commands[1]
    assert run_commands[2][0] == "scp"
    assert run_commands[2][-1] == (
        f"ae@192.0.2.10:/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar"
    )
    assert len(guest_commands) == 1
    assert "/mnt/host" not in guest_commands[0]
    assert f"/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar" in guest_commands[0]
    assert "ctr -n k8s.io images import --no-unpack" in guest_commands[0]
    manifest_payload = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    assert [entry["ref"] for entry in manifest_payload["images"]["bootstrap"]] == raw_image_groups["bootstrap"]
    assert [entry["ref"] for entry in manifest_payload["images"]["core"]] == raw_image_groups["core"]
    assert all(entry["expected_image_id"] for entry in manifest_payload["images"]["bootstrap"])
    assert all(entry["expected_image_id"] for entry in manifest_payload["images"]["core"])


def test_validation_seed_cache_only_bundles_missing_images(tmp_path, monkeypatch) -> None:
    run_id = "host-a-validation-missing-vllm"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "validation-seed-cache.json"
    raw_image_groups = {
        "bootstrap": [
            "docker.io/library/busybox:1.36",
            "registry.k8s.io/pause:3.9",
        ],
        "core": [
            f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
            "docker.io/rayproject/ray:latest",
            f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        ],
        "edge": [],
    }
    image_groups = _seed_image_groups_from_refs(raw_image_groups)
    required_images = f0n_nvidia_validate._required_seed_images(image_groups)
    required_refs = [image.ref for image in required_images]
    run_commands: list[list[str]] = []
    guest_commands: list[str] = []
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"
    seed_run_id = f"{run_id}-validation-seed"
    bundle = tmp_path / "state" / "lab-vm" / seed_run_id / "seeds" / "cri-seed-images.oci.tar"
    expected_image_ids: dict[str, str] = {}

    guest_image_state_calls = {"count": 0}

    def fake_guest_image_states(*, config, guest_ip, image_refs):  # type: ignore[no-untyped-def]
        guest_image_state_calls["count"] += 1
        missing_ref = f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
        if guest_image_state_calls["count"] == 1:
            states: dict[str, dict[str, object]] = {}
            for ref in image_refs:
                if ref == missing_ref:
                    states[ref] = {"present": False, "image_id": None, "detail": "not found"}
                else:
                    states[ref] = {"present": True, "image_id": expected_image_ids[ref], "detail": ""}
            return states
        return {
            ref: {"present": True, "image_id": expected_image_ids[ref], "detail": ""}
            for ref in image_refs
        }

    monkeypatch.setattr(f0n_nvidia_validate, "_guest_image_states", fake_guest_image_states)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_validation_seed_image_groups",
        lambda *, plan, args: {
            section: list(refs) for section, refs in image_groups.items()
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_seed_bundle_paths",
        lambda run_id, label="core-seed": (seed_run_id, bundle),
    )

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        run_commands.append(cmd)
        if cmd[0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]:
            manifest = Path(cmd[cmd.index("--manifest") + 1])
            metadata_only = "--metadata-only" in cmd
            expected_image_ids.update(
                _write_fake_seed_metadata(bundle=bundle, manifest=manifest, metadata_only=metadata_only)
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_cleanup_guest_seed_staging",
        lambda **kwargs: {
            "status": "ok",
            "detail": "",
            "removed_images": [],
            "purged_namespace": False,
            "avail_before_bytes": None,
            "avail_after_bytes": None,
            "required_free_bytes": kwargs.get("min_free_bytes"),
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": inventory_path,
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        guest_commands.append(command)
        return subprocess.CompletedProcess(["ssh"], 0, "", "")

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )

    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )
    f0n_nvidia_validate._ensure_guest_validation_seed_cache(
        args=args,
        run_root=run_root,
        plan=plan,
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["missing_before"] == [f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE]
    assert payload["stale_before"] == []
    assert sorted(payload["fresh_before"]) == sorted(
        [ref for ref in required_refs if ref != f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE]
    )
    assert payload["missing_after"] == []
    assert payload["stale_after"] == []
    assert payload["guest_bundle"] == f"/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar"
    assert payload["manifest"].endswith(
        "host-a-validation-missing-vllm/ae/validation-seed-manifest.json"
    )
    assert isinstance(payload["import_duration_ms"], int)
    assert payload["selected_vllm_image"] == f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
    assert payload["selected_vllm_image_present_after_import"] is True
    assert payload["selected_vllm_image_matches_expected_after_import"] is True
    seed_manifest = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    assert seed_manifest["images"]["bootstrap"] == []
    assert [entry["ref"] for entry in seed_manifest["images"]["core"]] == [
        f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
    ]
    assert seed_manifest["images"]["core"][0]["expected_image_id"] == _fake_image_id(
        f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
    )
    assert len(run_commands) == 3
    assert run_commands[0][0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]
    assert run_commands[0][3] == "host-a-validation-missing-vllm-validation-seed"
    assert "--metadata-only" in run_commands[0]
    assert len(guest_commands) == 1
    assert f"/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar" in guest_commands[0]
    assert "ctr -n k8s.io images import --no-unpack" in guest_commands[0]


def test_validation_seed_cache_rebuilds_full_bundle_after_namespace_purge(
    tmp_path, monkeypatch
) -> None:
    run_id = "host-a-validation-purged-namespace"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "validation-seed-cache.json"
    raw_image_groups = {
        "bootstrap": [
            "docker.io/library/busybox:1.36",
            "registry.k8s.io/pause:3.9",
        ],
        "core": [
            f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
            "docker.io/rayproject/ray:latest",
            f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        ],
        "edge": [],
    }
    image_groups = _seed_image_groups_from_refs(raw_image_groups)
    required_images = f0n_nvidia_validate._required_seed_images(image_groups)
    required_refs = [image.ref for image in required_images]
    missing_ref = f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
    run_commands: list[list[str]] = []
    guest_commands: list[str] = []
    seed_bundle_refs_per_call: list[list[str]] = []
    seed_run_id = f"{run_id}-validation-seed"
    bundle = tmp_path / "state" / "lab-vm" / seed_run_id / "seeds" / "cri-seed-images.oci.tar"
    expected_image_ids: dict[str, str] = {}

    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_validation_seed_image_groups",
        lambda *, plan, args: {
            section: list(refs) for section, refs in image_groups.items()
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_seed_bundle_paths",
        lambda run_id, label="core-seed": (seed_run_id, bundle),
    )

    guest_image_state_calls = {"count": 0}

    def fake_guest_image_states(*, config, guest_ip, image_refs):  # type: ignore[no-untyped-def]
        guest_image_state_calls["count"] += 1
        if guest_image_state_calls["count"] == 1:
            states: dict[str, dict[str, object]] = {}
            for ref in image_refs:
                if ref == missing_ref:
                    states[ref] = {"present": False, "image_id": None, "detail": "not found"}
                else:
                    states[ref] = {"present": True, "image_id": expected_image_ids[ref], "detail": ""}
            return states
        return {
            ref: {"present": True, "image_id": expected_image_ids[ref], "detail": ""}
            for ref in image_refs
        }

    monkeypatch.setattr(f0n_nvidia_validate, "_guest_image_states", fake_guest_image_states)

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        run_commands.append(cmd)
        if cmd[0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]:
            manifest = Path(cmd[cmd.index("--manifest") + 1])
            seed_bundle_refs_per_call.append(
                [
                    image.ref
                    for image in f0n_nvidia_validate._required_seed_images(
                        f0n_nvidia_validate._seed_manifest_image_groups(manifest)
                    )
                ]
            )
            metadata_only = "--metadata-only" in cmd
            expected_image_ids.update(
                _write_fake_seed_metadata(bundle=bundle, manifest=manifest, metadata_only=metadata_only)
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_cleanup_guest_seed_staging",
        lambda **kwargs: {
            "status": "ok",
            "detail": "",
            "removed_images": [],
            "purged_namespace": True,
            "avail_before_bytes": 10,
            "avail_after_bytes": 10**12,
            "required_free_bytes": kwargs.get("min_free_bytes"),
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json",
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        lambda runner, *, config, guest_ip, command: guest_commands.append(command)
        or subprocess.CompletedProcess(["ssh"], 0, "", ""),
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )

    f0n_nvidia_validate._ensure_guest_validation_seed_cache(
        args=args,
        run_root=run_root,
        plan=plan,
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["missing_before"] == [missing_ref]
    assert payload["stale_before"] == []
    assert sorted(payload["fresh_before"]) == sorted([ref for ref in required_refs if ref != missing_ref])
    assert payload["missing_after"] == []
    assert payload["stale_after"] == []
    assert sorted(payload["fresh_after"]) == sorted(required_refs)
    assert payload["cleanup"]["purged_namespace"] is True
    assert payload["selected_vllm_image_matches_expected_after_import"] is True
    assert seed_bundle_refs_per_call == [
        required_refs,
        [missing_ref],
        required_refs,
    ]
    assert len(run_commands) == 4
    assert len(guest_commands) == 1
    seed_manifest = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    assert [entry["ref"] for entry in seed_manifest["images"]["bootstrap"]] == raw_image_groups["bootstrap"]
    assert [entry["ref"] for entry in seed_manifest["images"]["core"]] == raw_image_groups["core"]


def test_validation_seed_cache_uses_built_bundle_metadata_for_post_import_match(
    tmp_path, monkeypatch
) -> None:
    run_id = "host-a-validation-built-meta"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "validation-seed-cache.json"
    raw_image_groups = {
        "bootstrap": [],
        "core": ["docker.io/rayproject/ray:latest"],
        "edge": [],
    }
    image_groups = _seed_image_groups_from_refs(raw_image_groups)
    required_images = f0n_nvidia_validate._required_seed_images(image_groups)
    required_refs = [image.ref for image in required_images]
    seed_run_id = f"{run_id}-validation-seed"
    bundle = tmp_path / "state" / "lab-vm" / seed_run_id / "seeds" / "cri-seed-images.oci.tar"
    metadata_expected_image_ids: dict[str, str] = {}
    build_expected_image_ids: dict[str, str] = {}
    guest_image_state_calls = {"count": 0}

    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_validation_seed_image_groups",
        lambda *, plan, args: {
            section: list(refs) for section, refs in image_groups.items()
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_seed_bundle_paths",
        lambda run_id, label="core-seed": (seed_run_id, bundle),
    )

    def fake_guest_image_states(*, config, guest_ip, image_refs):  # type: ignore[no-untyped-def]
        guest_image_state_calls["count"] += 1
        if guest_image_state_calls["count"] == 1:
            return {
                ref: {"present": False, "image_id": None, "detail": "not found"}
                for ref in image_refs
            }
        return {
            ref: {"present": True, "image_id": build_expected_image_ids[ref], "detail": ""}
            for ref in image_refs
        }

    monkeypatch.setattr(f0n_nvidia_validate, "_guest_image_states", fake_guest_image_states)

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        if cmd[0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]:
            manifest = Path(cmd[cmd.index("--manifest") + 1])
            metadata_only = "--metadata-only" in cmd
            generated = _write_fake_seed_metadata_with_build_override(
                bundle=bundle,
                manifest=manifest,
                metadata_only=metadata_only,
                build_suffix="built",
            )
            if metadata_only:
                metadata_expected_image_ids.update(generated)
            else:
                build_expected_image_ids.update(generated)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_cleanup_guest_seed_staging",
        lambda **kwargs: {
            "status": "ok",
            "detail": "",
            "removed_images": [],
            "purged_namespace": False,
            "avail_before_bytes": None,
            "avail_after_bytes": None,
            "required_free_bytes": kwargs.get("min_free_bytes"),
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json",
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        lambda runner, *, config, guest_ip, command: subprocess.CompletedProcess(["ssh"], 0, "", ""),
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )

    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )
    f0n_nvidia_validate._ensure_guest_validation_seed_cache(
        args=args,
        run_root=run_root,
        plan=plan,
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert metadata_expected_image_ids != build_expected_image_ids
    assert payload["status"] == "ready"
    assert payload["missing_before"] == required_refs
    assert payload["stale_before"] == []
    assert payload["missing_after"] == []
    assert payload["stale_after"] == []
    assert payload["fresh_after"] == required_refs
    assert payload["expected_images"][0]["expected_image_id"] == build_expected_image_ids[required_refs[0]]


def test_validation_seed_cache_skips_import_when_guest_images_match_expected_ids(
    tmp_path, monkeypatch
) -> None:
    run_id = "host-a-validation-fresh"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "validation-seed-cache.json"
    raw_image_groups = {
        "bootstrap": [],
        "core": [
            "docker.io/rayproject/ray:latest",
            f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        ],
        "edge": [],
    }
    image_groups = _seed_image_groups_from_refs(raw_image_groups)
    required_images = f0n_nvidia_validate._required_seed_images(image_groups)
    required_refs = [image.ref for image in required_images]
    run_commands: list[list[str]] = []
    seed_run_id = f"{run_id}-validation-seed"
    bundle = tmp_path / "state" / "lab-vm" / seed_run_id / "seeds" / "cri-seed-images.oci.tar"
    expected_image_ids: dict[str, str] = {}

    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_validation_seed_image_groups",
        lambda *, plan, args: {
            section: list(refs) for section, refs in image_groups.items()
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_seed_bundle_paths",
        lambda run_id, label="core-seed": (seed_run_id, bundle),
    )

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        run_commands.append(cmd)
        if cmd[0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]:
            manifest = Path(cmd[cmd.index("--manifest") + 1])
            expected_image_ids.update(
                _write_fake_seed_metadata(bundle=bundle, manifest=manifest, metadata_only=True)
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_guest_image_states",
        lambda *, config, guest_ip, image_refs: {
            ref: {"present": True, "image_id": expected_image_ids[ref], "detail": ""}
            for ref in image_refs
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json",
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )

    f0n_nvidia_validate._ensure_guest_validation_seed_cache(
        args=args,
        run_root=run_root,
        plan=plan,
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["missing_before"] == []
    assert payload["stale_before"] == []
    assert payload["fresh_before"] == required_refs
    assert payload["selected_vllm_image_present_after_import"] is True
    assert payload["selected_vllm_image_matches_expected_after_import"] is True
    assert len(run_commands) == 1
    assert "--metadata-only" in run_commands[0]


def test_validation_seed_cache_refreshes_stale_guest_images_by_expected_id(
    tmp_path, monkeypatch
) -> None:
    run_id = "host-a-validation-stale-vllm"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "validation-seed-cache.json"
    raw_image_groups = {
        "bootstrap": [],
        "core": [
            "docker.io/rayproject/ray:latest",
            f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        ],
        "edge": [],
    }
    image_groups = _seed_image_groups_from_refs(raw_image_groups)
    run_commands: list[list[str]] = []
    guest_commands: list[str] = []
    cleanup_calls: list[dict[str, object]] = []
    seed_run_id = f"{run_id}-validation-seed"
    bundle = tmp_path / "state" / "lab-vm" / seed_run_id / "seeds" / "cri-seed-images.oci.tar"
    expected_image_ids: dict[str, str] = {}
    stale_ref = f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE

    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_validation_seed_image_groups",
        lambda *, plan, args: {
            section: list(refs) for section, refs in image_groups.items()
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_seed_bundle_paths",
        lambda run_id, label="core-seed": (seed_run_id, bundle),
    )

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        run_commands.append(cmd)
        if cmd[0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]:
            manifest = Path(cmd[cmd.index("--manifest") + 1])
            metadata_only = "--metadata-only" in cmd
            expected_image_ids.update(
                _write_fake_seed_metadata(bundle=bundle, manifest=manifest, metadata_only=metadata_only)
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)

    guest_image_state_calls = {"count": 0}

    def fake_guest_image_states(*, config, guest_ip, image_refs):  # type: ignore[no-untyped-def]
        guest_image_state_calls["count"] += 1
        if guest_image_state_calls["count"] == 1:
            states: dict[str, dict[str, object]] = {}
            for ref in image_refs:
                image_id = expected_image_ids[ref]
                if ref == stale_ref:
                    image_id = "sha256:" + ("cd" * 32)
                states[ref] = {"present": True, "image_id": image_id, "detail": ""}
            return states
        return {
            ref: {"present": True, "image_id": expected_image_ids[ref], "detail": ""}
            for ref in image_refs
        }

    monkeypatch.setattr(f0n_nvidia_validate, "_guest_image_states", fake_guest_image_states)

    def fake_cleanup_guest_seed_staging(**kwargs):  # type: ignore[no-untyped-def]
        cleanup_calls.append(kwargs)
        return {
            "status": "ok",
            "detail": "",
            "removed_images": list(kwargs.get("stale_refs") or []),
            "purged_namespace": False,
            "avail_before_bytes": None,
            "avail_after_bytes": None,
            "required_free_bytes": kwargs.get("min_free_bytes"),
        }

    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_cleanup_guest_seed_staging",
        fake_cleanup_guest_seed_staging,
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json",
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        guest_commands.append(command)
        return subprocess.CompletedProcess(["ssh"], 0, "", "")

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )

    f0n_nvidia_validate._ensure_guest_validation_seed_cache(
        args=args,
        run_root=run_root,
        plan=plan,
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["missing_before"] == []
    assert payload["stale_before"] == [stale_ref]
    assert payload["fresh_before"] == ["docker.io/rayproject/ray:latest"]
    assert payload["stale_after"] == []
    assert payload["selected_vllm_image_matches_expected_after_import"] is True
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0]["stale_refs"] == [stale_ref]
    seed_manifest = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    assert seed_manifest["images"]["bootstrap"] == []
    assert [entry["ref"] for entry in seed_manifest["images"]["core"]] == [stale_ref]
    assert len(run_commands) == 3
    assert len(guest_commands) == 1


def test_selected_test_model_defaults_to_smollm2_path() -> None:
    args = argparse.Namespace(
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
    )

    model = f0n_nvidia_validate._selected_test_model(args)

    assert model.model_id == "HuggingFaceTB/SmolLM2-1.7B-Instruct"
    assert model.revision is None
    assert model.local_path == "/models/smollm2-1.7b-instruct"


def test_prepare_rendered_manifests_overrides_model_fields(tmp_path: Path) -> None:
    plan = f0n_nvidia_validate.build_plan(
        run_id="rendered-manifest",
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )
    model = f0n_nvidia_validate.TestModelSpec(
        model_id="HuggingFaceTB/SmolLM2-360M-Instruct",
        revision="main",
        local_path="/models/smollm2-360m-instruct",
    )

    f0n_nvidia_validate._prepare_rendered_manifests(
        plan=plan,
        test_model=model,
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
    )

    rendered = Path(plan["cells"][0]["artifacts"]["rendered_manifest"])
    payload = f0n_nvidia_validate.yaml.safe_load(rendered.read_text(encoding="utf-8"))
    assert payload["spec"]["model"]["modelId"] == "HuggingFaceTB/SmolLM2-360M-Instruct"
    assert payload["spec"]["model"]["revision"] == "main"
    assert payload["spec"]["model"]["localPath"] == "/models/smollm2-360m-instruct"
    assert payload["spec"]["executor"]["launcherImage"] == f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
    assert payload["spec"]["executor"]["mpImage"] == f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
    assert payload["spec"]["executor"]["dtype"] == "half"


def test_ensure_guest_test_model_runs_remote_bootstrap_helper(tmp_path: Path, monkeypatch) -> None:
    run_id = "host-a-model"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "model-bootstrap.json"
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"
    remote_commands: list[str] = []

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": inventory_path,
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        remote_commands.append(command)
        return subprocess.CompletedProcess(
            ["ssh"],
            0,
            json.dumps(
                {
                    "status": "ready",
                    "result": "reused",
                    "model_id": "HuggingFaceTB/SmolLM2-1.7B-Instruct",
                    "local_path": "/models/smollm2-1.7b-instruct",
                }
            ),
            "",
        )

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id="HuggingFaceTB/SmolLM2-1.7B-Instruct",
        test_model_revision="",
        test_model_local_path="/models/smollm2-1.7b-instruct",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )

    f0n_nvidia_validate._ensure_guest_test_model(
        args=args,
        run_root=run_root,
        test_model=f0n_nvidia_validate._selected_test_model(args),
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["results"][0]["result"] == "reused"
    assert len(remote_commands) == 1
    assert "bootstrap_inference_model.py" in remote_commands[0]
    assert "HuggingFaceTB/SmolLM2-1.7B-Instruct" in remote_commands[0]
    assert "/models/smollm2-1.7b-instruct" in remote_commands[0]


def test_ensure_guest_vllm_image_probe_records_ready_result(tmp_path: Path, monkeypatch) -> None:
    run_id = "host-a-vllm-probe"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "vllm-image-probe.json"
    transcript_path = run_root / "ae" / "vllm-image-probe.transcript.txt"
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"
    remote_commands: list[str] = []

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": inventory_path,
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        remote_commands.append(command)
        return subprocess.CompletedProcess(
            ["ssh"],
            0,
            json.dumps(
                {
                    "status": "ready",
                    "torch_version": "2.4.0+cu121",
                    "cuda_version": "12.1",
                    "cuda_device_count": 1,
                    "cuda_is_available": True,
                    "cuda_tensor_ok": True,
                }
            ),
            "",
        )

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image="docker.io/vllm/vllm-openai:v0.6.2",
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )

    f0n_nvidia_validate._ensure_guest_vllm_image_probe(
        args=args,
        run_root=run_root,
        plan=plan,
        test_vllm_image="docker.io/vllm/vllm-openai:v0.6.2",
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["image"] == "docker.io/vllm/vllm-openai:v0.6.2"
    assert payload["phase"] == "python"
    assert payload["probe_timeout"] == f0n_nvidia_validate.DEFAULT_CRI_PROBE_TIMEOUT
    assert payload["result"]["cuda_is_available"] is True
    assert payload["result"]["cuda_tensor_ok"] is True
    assert payload["transcript"] == str(transcript_path)
    assert transcript_path.read_text(encoding="utf-8").strip().startswith("{")
    assert len(remote_commands) == 1
    assert "cri_torch_cuda_probe.sh" in remote_commands[0]
    assert "AE_CRI_PROBE_IMAGE=docker.io/vllm/vllm-openai:v0.6.2" in remote_commands[0]
    assert f"AE_CRI_PROBE_TIMEOUT={f0n_nvidia_validate.DEFAULT_CRI_PROBE_TIMEOUT}" in remote_commands[0]


def test_ensure_guest_vllm_image_probe_fails_with_artifact(tmp_path: Path, monkeypatch) -> None:
    run_id = "host-a-vllm-probe-fail"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "vllm-image-probe.json"
    transcript_path = run_root / "ae" / "vllm-image-probe.transcript.txt"
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": inventory_path,
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        lambda runner, *, config, guest_ip, command: subprocess.CompletedProcess(  # type: ignore[no-untyped-def]
            ["ssh"],
            1,
            json.dumps(
                {
                    "status": "failed",
                    "torch_version": "2.4.0+cu121",
                    "cuda_version": "12.1",
                    "cuda_device_count": 1,
                    "cuda_is_available": False,
                    "cuda_tensor_ok": False,
                    "cuda_error": "RuntimeError: forward compatibility was attempted on non supported HW",
                }
            ),
            "CUDA image probe container exited with code 1",
        ),
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image="docker.io/vllm/vllm-openai:v0.6.2",
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )

    try:
        f0n_nvidia_validate._ensure_guest_vllm_image_probe(
            args=args,
            run_root=run_root,
            plan=plan,
            test_vllm_image="docker.io/vllm/vllm-openai:v0.6.2",
        )
    except SystemExit as exc:
        assert "selected vLLM image failed guest CUDA probe" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected probe failure")

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["phase"] == "python"
    assert payload["result"]["cuda_is_available"] is False
    assert payload["result"]["cuda_tensor_ok"] is False
    assert payload["transcript"] == str(transcript_path)
    assert "CUDA image probe container exited with code 1" in transcript_path.read_text(
        encoding="utf-8"
    )


def test_ensure_guest_vllm_image_probe_records_pre_python_failure_debug(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "host-a-vllm-probe-create-timeout"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "vllm-image-probe.json"
    transcript_path = run_root / "ae" / "vllm-image-probe.transcript.txt"
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": inventory_path,
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        lambda runner, *, config, guest_ip, command: subprocess.CompletedProcess(  # type: ignore[no-untyped-def]
            ["ssh"],
            1,
            "\n".join(
                [
                    "__probe_stage__ phase=info status=ok elapsed_ms=5",
                    "__probe_stage__ phase=inspecti status=ok elapsed_ms=12",
                    "__probe_stage__ phase=create status=failed elapsed_ms=2004",
                    json.dumps(
                        {
                            "status": "failed",
                            "phase": "create",
                            "image": "docker.io/library/k1s-vllm-openai:host-a-cu121-v2",
                            "runtime_handler": "nvidia",
                            "timeout": "180s",
                            "duration_ms": 2250,
                            "error": "rpc error: code = DeadlineExceeded desc = context deadline exceeded",
                            "durations_ms": {"info": 5, "inspecti": 12, "create": 2004},
                            "debug": {
                                "containerd_journal": "containerd journal excerpt",
                                "image_inspect": "image inspect excerpt",
                            },
                        },
                        sort_keys=True,
                    ),
                ]
            ),
            "",
        ),
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )

    try:
        f0n_nvidia_validate._ensure_guest_vllm_image_probe(
            args=args,
            run_root=run_root,
            plan=plan,
            test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        )
    except SystemExit as exc:
        assert "selected vLLM image failed guest CUDA probe" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected probe failure")

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["phase"] == "create"
    assert payload["probe_timeout"] == "180s"
    assert payload["duration_ms"] == 2250
    assert "result" not in payload
    assert payload["detail"] == "rpc error: code = DeadlineExceeded desc = context deadline exceeded"
    assert payload["durations_ms"]["create"] == 2004
    assert payload["debug_excerpt"]["containerd_journal"] == "containerd journal excerpt"
    assert payload["debug_excerpt"]["image_inspect"] == "image inspect excerpt"
    assert payload["transcript"] == str(transcript_path)
    assert "__probe_stage__ phase=create status=failed elapsed_ms=2004" in transcript_path.read_text(
        encoding="utf-8"
    )


def test_ensure_guest_vllm_startup_probe_records_ready_result(tmp_path: Path, monkeypatch) -> None:
    run_id = "host-a-vllm-startup-probe"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "vllm-startup-probe.json"
    transcript_path = run_root / "ae" / "vllm-startup-probe.transcript.txt"
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"
    remote_commands: list[str] = []

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": inventory_path,
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )

    def fake_run_guest_command(runner, *, config, guest_ip, command):  # type: ignore[no-untyped-def]
        remote_commands.append(command)
        return subprocess.CompletedProcess(
            ["ssh"],
            0,
            json.dumps(
                {
                    "status": "ready",
                    "phase": "serve",
                    "duration_ms": 92341,
                    "timeout": "180s",
                    "result": {
                        "api_port": 8000,
                        "container_state": "CONTAINER_RUNNING",
                        "dtype": "half",
                        "model_path": "/models/smollm2-1.7b-instruct",
                        "ready_signal": "Application startup complete",
                    },
                }
            ),
            "",
        )

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        fake_run_guest_command,
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )
    f0n_nvidia_validate._prepare_rendered_manifests(
        plan=plan,
        test_model=f0n_nvidia_validate._selected_test_model(args),
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
    )

    f0n_nvidia_validate._ensure_guest_vllm_startup_probe(
        args=args,
        run_root=run_root,
        plan=plan,
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["phase"] == "serve"
    assert payload["cell_lane"] == "cell-a-single"
    assert payload["image"] == f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE
    assert payload["model_path"] == "/models/smollm2-1.7b-instruct"
    assert payload["dtype"] == "half"
    assert payload["result"]["ready_signal"] == "Application startup complete"
    assert payload["transcript"] == str(transcript_path)
    assert transcript_path.read_text(encoding="utf-8").strip().startswith("{")
    assert len(remote_commands) == 1
    assert "cri_vllm_startup_probe.sh" in remote_commands[0]
    assert "AE_CRI_PROBE_IMAGE=docker.io/library/k1s-vllm-openai:host-a-cu121-v2" in remote_commands[0]
    assert "AE_CRI_MODEL_PATH=/models/smollm2-1.7b-instruct" in remote_commands[0]
    assert "AE_CRI_VLLM_DTYPE=half" in remote_commands[0]


def test_ensure_guest_vllm_startup_probe_fails_with_artifact(tmp_path: Path, monkeypatch) -> None:
    run_id = "host-a-vllm-startup-probe-fail"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "vllm-startup-probe.json"
    transcript_path = run_root / "ae" / "vllm-startup-probe.transcript.txt"
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"

    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "resolve_guest_target",
        lambda config: {
            "guest_ip": "192.0.2.10",
            "guest_repo": "/home/ae/k1s",
            "inventory": inventory_path,
            "inventory_entry": {
                "name": "k1s-core-a-gpu",
                "guest_repo": "/home/ae/k1s",
                "guest_user": "ae",
            },
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_guest_command",
        lambda runner, *, config, guest_ip, command: subprocess.CompletedProcess(  # type: ignore[no-untyped-def]
            ["ssh"],
            1,
            json.dumps(
                {
                    "status": "failed",
                    "phase": "serve",
                    "duration_ms": 84123,
                    "timeout": "180s",
                    "error": (
                        "ValueError: Bfloat16 is only supported on GPUs with compute capability "
                        "of at least 8.0"
                    ),
                    "result": {
                        "api_port": 8000,
                        "container_exit_code": 1,
                        "container_state": "CONTAINER_EXITED",
                        "dtype": "half",
                        "model_path": "/models/smollm2-1.7b-instruct",
                    },
                    "debug": {
                        "container_logs": "ValueError: Bfloat16 is only supported...",
                    },
                }
            ),
            "",
        ),
    )

    args = argparse.Namespace(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane=["cell-a-single"],
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo=None,
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    plan = f0n_nvidia_validate.build_plan(
        run_id=run_id,
        runs_dir=tmp_path,
        cell_lane_names=["cell-a-single"],
    )
    f0n_nvidia_validate._prepare_rendered_manifests(
        plan=plan,
        test_model=f0n_nvidia_validate._selected_test_model(args),
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
    )

    try:
        f0n_nvidia_validate._ensure_guest_vllm_startup_probe(
            args=args,
            run_root=run_root,
            plan=plan,
            test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        )
    except SystemExit as exc:
        assert "selected vLLM image failed guest startup probe" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected startup probe failure")

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["phase"] == "serve"
    assert payload["model_path"] == "/models/smollm2-1.7b-instruct"
    assert payload["dtype"] == "half"
    assert payload["detail"].startswith("ValueError: Bfloat16 is only supported")
    assert payload["debug_excerpt"]["container_logs"] == "ValueError: Bfloat16 is only supported..."
    assert payload["transcript"] == str(transcript_path)
    assert transcript_path.read_text(encoding="utf-8").strip().startswith("{")


def test_probe_python_result_ignores_shell_level_failure_payload() -> None:
    payload = {
        "status": "failed",
        "phase": "create",
        "error": "rpc error: code = DeadlineExceeded desc = context deadline exceeded",
        "duration_ms": 2250,
        "durations_ms": {"info": 5, "inspecti": 12, "create": 2004},
    }

    assert f0n_nvidia_validate._probe_python_result(payload) is None


def test_run_collect_best_effort_deletes_cell_before_initial_apply(tmp_path, monkeypatch) -> None:
    args = argparse.Namespace(
        run_id="host-a-retest",
        runs_dir=tmp_path,
        force=False,
        ae_bin="",
        limit_events=20,
        skip_egpu_passthrough_validate=True,
        cell_lane=["cell-a-single"],
        cell_ready_timeout=30.0,
        cell_ready_poll_interval=1.0,
        controller_env=None,
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
    )
    delete_calls: list[list[str]] = []
    capture_calls: list[list[str]] = []
    wait_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(f0n_nvidia_validate, "_ae_env", lambda controller_env=None: {})
    monkeypatch.setattr(f0n_nvidia_validate, "_ae_prefix", lambda ae_bin: ["ae"])
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_ensure_guest_validation_seed_cache",
        lambda *, args, run_root, plan: None,
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_ensure_guest_test_model",
        lambda *, args, run_root, test_model: None,
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_ensure_guest_vllm_image_probe",
        lambda *, args, run_root, plan, test_vllm_image: None,
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_ensure_guest_vllm_startup_probe",
        lambda *, args, run_root, plan, test_vllm_image: None,
    )

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        delete_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "not found")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)

    def fake_run_capture(*, cmd, path, env):  # type: ignore[no-untyped-def]
        capture_calls.append(cmd)

    monkeypatch.setattr(f0n_nvidia_validate, "_run_capture", fake_run_capture)

    def fake_wait_for_cell_ready(**kwargs):  # type: ignore[no-untyped-def]
        wait_calls.append((kwargs["name"], kwargs["manifest"]))
        return {"phase": "READY", "allocations": {"api_endpoint": "http://127.0.0.1:18080/health"}}

    monkeypatch.setattr(f0n_nvidia_validate, "_wait_for_cell_ready", fake_wait_for_cell_ready)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_probe_cell_api",
        lambda *, status_payload, path: None,
    )

    rc = f0n_nvidia_validate.run_collect(args)

    assert rc == 0
    assert delete_calls == [["ae", "cell", "delete", "cell-a-single"]]
    assert wait_calls == [
        (
            "cell-a-single",
            str(
                tmp_path
                / "host-a-retest"
                / "cells"
                / "cell-a-single"
                / "manifest-rendered.yaml"
            ),
        ),
        (
            "cell-a-single",
            str(
                tmp_path
                / "host-a-retest"
                / "cells"
                / "cell-a-single"
                / "manifest-rendered.yaml"
            ),
        ),
    ]
    assert capture_calls[0] == ["ae", "nodes", "--json"]
    assert capture_calls[1] == ["ae", "cell", "events", "cell-a-single", "--limit", "20"]
    assert capture_calls[2] == ["ae", "cell", "delete", "cell-a-single"]


def test_run_collect_restores_validation_seed_before_egpu(tmp_path, monkeypatch) -> None:
    args = argparse.Namespace(
        run_id="host-a-validation-order",
        runs_dir=tmp_path,
        force=False,
        ae_bin="",
        limit_events=20,
        skip_egpu_passthrough_validate=False,
        cell_lane=["cell-a-single"],
        cell_ready_timeout=30.0,
        cell_ready_poll_interval=1.0,
        controller_env=None,
        test_model_id=f0n_nvidia_validate.DEFAULT_TEST_MODEL_ID,
        test_model_revision="",
        test_model_local_path="",
        test_vllm_image=f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE,
        guest_ip="192.0.2.10",
        vm_name="k1s-core-a-gpu",
        inventory=None,
        ssh_user="ae",
        ssh_key="/tmp/id_rsa",
        guest_repo="/home/ae/k1s",
        expected_gpu=f0n_nvidia_validate.egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=f0n_nvidia_validate.egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id="",
        runtime_handler=f0n_nvidia_validate.egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE,
        execution_model=f0n_nvidia_validate.egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    calls: list[str] = []

    monkeypatch.setattr(f0n_nvidia_validate, "_ae_env", lambda controller_env=None: {})
    monkeypatch.setattr(f0n_nvidia_validate, "_ae_prefix", lambda ae_bin: ["ae"])
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_ensure_guest_validation_seed_cache",
        lambda *, args, run_root, plan: calls.append("validation-seed"),
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_ensure_guest_test_model",
        lambda *, args, run_root, test_model: calls.append("model-bootstrap"),
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_ensure_guest_vllm_image_probe",
        lambda *, args, run_root, plan, test_vllm_image: calls.append("vllm-probe"),
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_ensure_guest_vllm_startup_probe",
        lambda *, args, run_root, plan, test_vllm_image: calls.append("vllm-startup-probe"),
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_run_capture",
        lambda *, cmd, path, env: calls.append(f"capture:{' '.join(cmd)}"),
    )
    monkeypatch.setattr(
        f0n_nvidia_validate.egpu_validate,
        "run_validation",
        lambda config: calls.append("egpu-validate") or {
            "status": "passed",
            "guest": {"guest_ip": "192.0.2.10"},
            "checks": {
                "egpu_attach": "passed",
                "egpu_cri_runtime": "passed",
                "egpu_compute_smoke": "passed",
            },
        },
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_best_effort_delete_cell",
        lambda *, ae, name, env: calls.append(f"delete:{name}"),
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_wait_for_cell_ready",
        lambda **kwargs: {"phase": "READY", "allocations": {"api_endpoint": "127.0.0.1:18080"}},
    )
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_probe_cell_api",
        lambda *, status_payload, path: calls.append("probe"),
    )

    rc = f0n_nvidia_validate.run_collect(args)

    assert rc == 0
    assert calls.index("validation-seed") < calls.index("egpu-validate")
    assert calls.index("egpu-validate") < calls.index("vllm-probe")
    assert calls.index("vllm-probe") < calls.index("model-bootstrap")
    assert calls.index("model-bootstrap") < calls.index("vllm-startup-probe")
