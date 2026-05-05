from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "f0n_nvidia_validate.py"
_SPEC = spec_from_file_location("f0n_nvidia_validate_script", SCRIPT)
f0n_nvidia_validate = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = f0n_nvidia_validate
_SPEC.loader.exec_module(f0n_nvidia_validate)


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
        assert artifacts["events_initial"].endswith("events-initial.txt")
        assert artifacts["api_probe_initial"].endswith("api-probe-initial.json")
        assert artifacts["status_reapplied"].endswith("status-reapplied.json")
        assert artifacts["api_probe_reapplied"].endswith("api-probe-reapplied.json")
        assert artifacts["teardown"].endswith("teardown.txt")
    assert payload["artifacts"]["model_bootstrap"].endswith("review-run/ae/model-bootstrap.json")


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

    assert "docker.io/library/busybox:1.36" in groups["bootstrap"]
    assert "registry.k8s.io/pause:3.9" in groups["bootstrap"]
    assert f0n_nvidia_validate.egpu_validate.DEFAULT_COMPUTE_IMAGE in groups["core"]
    assert "docker.io/rayproject/ray:latest" in groups["core"]
    assert f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE in groups["core"]
    assert groups["edge"] == []


def test_normalize_image_ref_adds_docker_host_defaults() -> None:
    assert f0n_nvidia_validate._normalize_image_ref("busybox:1.36") == "docker.io/library/busybox:1.36"
    assert f0n_nvidia_validate._normalize_image_ref("rayproject/ray:latest") == "docker.io/rayproject/ray:latest"
    assert f0n_nvidia_validate._normalize_image_ref("docker.io/vllm/vllm-openai:v0.6.2") == (
        "docker.io/vllm/vllm-openai:v0.6.2"
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
    image_groups = {
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
    required_images = f0n_nvidia_validate._required_seed_images(image_groups)
    run_commands: list[list[str]] = []
    guest_commands: list[str] = []
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"

    guest_image_ref_calls = {"count": 0}

    def fake_guest_image_refs(*, config, guest_ip):  # type: ignore[no-untyped-def]
        guest_image_ref_calls["count"] += 1
        if guest_image_ref_calls["count"] == 1:
            return set()
        return set(required_images)

    monkeypatch.setattr(f0n_nvidia_validate, "_guest_image_refs", fake_guest_image_refs)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_validation_seed_image_groups",
        lambda *, plan, args: dict(image_groups),
    )

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        run_commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
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
    assert payload["missing_before"] == required_images
    assert payload["missing_after"] == []
    assert payload["cleanup"]["status"] == "ok"
    assert payload["guest_bundle"] == f"/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar"
    assert payload["guest_bundle"].startswith("/tmp/")
    assert payload["manifest"].endswith("host-a-validation-seed/ae/validation-seed-manifest.json")
    assert len(run_commands) == 2
    assert run_commands[0][0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]
    assert run_commands[0][4] == "--manifest"
    assert run_commands[0][5].endswith("host-a-validation-seed/ae/validation-seed-manifest.json")
    assert run_commands[0][6] == "--profile"
    assert run_commands[0][7] == "all"
    assert run_commands[1][0] == "scp"
    assert run_commands[1][-1] == (
        f"ae@192.0.2.10:/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar"
    )
    assert len(guest_commands) == 2
    assert "rm -f /tmp/*-cri-seed-images.oci.tar" in guest_commands[0]
    assert "/mnt/host" not in guest_commands[1]
    assert f"/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar" in guest_commands[1]
    assert "ctr -n k8s.io images import --no-unpack" in guest_commands[1]
    manifest_payload = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    assert manifest_payload["images"]["bootstrap"] == image_groups["bootstrap"]
    assert manifest_payload["images"]["core"] == image_groups["core"]


def test_validation_seed_cache_only_bundles_missing_images(tmp_path, monkeypatch) -> None:
    run_id = "host-a-validation-missing-vllm"
    run_root = tmp_path / run_id
    artifact_path = run_root / "ae" / "validation-seed-cache.json"
    image_groups = {
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
    required_images = f0n_nvidia_validate._required_seed_images(image_groups)
    run_commands: list[list[str]] = []
    guest_commands: list[str] = []
    inventory_path = ROOT / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"

    guest_image_ref_calls = {"count": 0}

    def fake_guest_image_refs(*, config, guest_ip):  # type: ignore[no-untyped-def]
        guest_image_ref_calls["count"] += 1
        if guest_image_ref_calls["count"] == 1:
            return set(required_images[:-1])
        return set(required_images)

    monkeypatch.setattr(f0n_nvidia_validate, "_guest_image_refs", fake_guest_image_refs)
    monkeypatch.setattr(
        f0n_nvidia_validate,
        "_validation_seed_image_groups",
        lambda *, plan, args: dict(image_groups),
    )

    def fake_run_command(*, cmd, env):  # type: ignore[no-untyped-def]
        run_commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(f0n_nvidia_validate, "_run_command", fake_run_command)
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
    assert payload["missing_after"] == []
    assert payload["guest_bundle"] == f"/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar"
    assert payload["manifest"].endswith(
        "host-a-validation-missing-vllm/ae/validation-seed-manifest.json"
    )
    seed_manifest = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    assert seed_manifest["images"]["bootstrap"] == []
    assert seed_manifest["images"]["core"] == [f0n_nvidia_validate.DEFAULT_TEST_VLLM_IMAGE]
    assert len(run_commands) == 2
    assert run_commands[0][0:2] == ["bash", str(f0n_nvidia_validate.CRI_SEED_BUNDLE_SCRIPT)]
    assert run_commands[0][3] == "host-a-validation-missing-vllm-validation-seed"
    assert len(guest_commands) == 2
    assert "rm -f /tmp/*-cri-seed-images.oci.tar" in guest_commands[0]
    assert f"/tmp/{run_id}-validation-seed-cri-seed-images.oci.tar" in guest_commands[1]
    assert "ctr -n k8s.io images import --no-unpack" in guest_commands[1]


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
