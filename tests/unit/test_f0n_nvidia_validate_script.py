from __future__ import annotations

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
        assert artifacts["status_initial"].endswith("status-initial.json")
        assert artifacts["events_initial"].endswith("events-initial.txt")
        assert artifacts["api_probe_initial"].endswith("api-probe-initial.json")
        assert artifacts["status_reapplied"].endswith("status-reapplied.json")
        assert artifacts["api_probe_reapplied"].endswith("api-probe-reapplied.json")
        assert artifacts["teardown"].endswith("teardown.txt")


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
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT / "src")


def test_core_seed_images_keep_inference_images() -> None:
    images = f0n_nvidia_validate._core_seed_images()

    assert "docker.io/rayproject/ray:latest" in images
    assert "docker.io/vllm/vllm-openai:latest" in images


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
