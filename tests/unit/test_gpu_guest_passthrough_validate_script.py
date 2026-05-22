from __future__ import annotations

import json
import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "gpu_guest_passthrough_validate.py"

_SPEC = spec_from_file_location("gpu_guest_passthrough_validate_script", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
gpu_guest_passthrough_validate = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gpu_guest_passthrough_validate
_SPEC.loader.exec_module(gpu_guest_passthrough_validate)


def _completed(
    cmd: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, responses: dict[str, subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        for needle, result in self.responses.items():
            if needle in joined:
                return result
        raise AssertionError(f"unexpected command: {joined}")


def _runtime_ready_output(*, handlers: str = "nvidia,runc") -> str:
    return "\n".join(
        [
            "crictl: /usr/bin/crictl",
            "CRI condition RuntimeReady=True",
            "RuntimeReady message: ok",
            "CRI condition NetworkReady=True",
            "NetworkReady message: ok",
            "CRI required runtime handler=nvidia",
            f"CRI available runtime handlers={handlers}",
            "CRI preflight OK",
        ]
    )


def _compute_success_output() -> str:
    return "\n".join(
        [
            "[Vector addition of 50000 elements]",
            "Copy input data from the host memory to the CUDA device",
            "CUDA kernel launch with 196 blocks of 256 threads",
            "Copy output data from the CUDA device to the host memory",
            "Test PASSED",
            "Done",
        ]
    )


def _make_config(tmp_path: Path, **overrides: object) -> gpu_guest_passthrough_validate.ValidationConfig:
    values = {
        "run_id": "review-run",
        "runs_dir": tmp_path,
        "guest_ip": "192.0.2.10",
        "vm_name": None,
        "inventory": None,
        "ssh_user": "ae",
        "ssh_key": None,
        "guest_repo": "/mnt/host",
        "expected_gpu": "TITAN RTX",
        "min_vram_gib": 24,
        "expected_pci_bus_id": None,
        "runtime_handler": "nvidia",
        "compute_image": "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1",
        "compute_success_signal": "Test PASSED",
        "execution_model": "linux_guest_passthrough",
    }
    values.update(overrides)
    return gpu_guest_passthrough_validate.ValidationConfig(**values)


def test_gpu_guest_passthrough_plan_emits_stable_artifact_shape(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    payload = gpu_guest_passthrough_validate.build_plan(config)
    assert payload["phase"] == "egpu_passthrough_validate"
    assert payload["execution_model"] == "linux_guest_passthrough"
    assert payload["artifacts"]["summary"].endswith("review-run/checks/egpu_passthrough_validate.json")
    assert payload["artifacts"]["attach"].endswith("review-run/checks/egpu_attach.json")
    assert payload["artifacts"]["cri_runtime"].endswith("review-run/checks/egpu_cri_runtime.json")
    assert payload["artifacts"]["compute_smoke"].endswith(
        "review-run/checks/egpu_compute_smoke.json"
    )


def test_gpu_guest_passthrough_validate_rejects_wrong_gpu_model(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = FakeRunner(
        {
            "nvidia-smi --query-gpu=name,memory.total,pci.bus_id": _completed(
                [],
                stdout="RTX 8000, 49152 MiB, 0000:65:00.0\n",
            ),
            "AE_CRI_REQUIRE_RUNTIME_READY=1": _completed([], stdout=_runtime_ready_output()),
            "AE_CRI_VECTORADD_IMAGE=": _completed([], stdout=_compute_success_output()),
        }
    )
    payload = gpu_guest_passthrough_validate.run_validation(config, runner=runner)
    attach = json.loads(
        Path(payload["artifacts"]["attach"]).read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert attach["status"] == "failed"
    assert attach["assertions"]["model_match"] is False


def test_gpu_guest_passthrough_validate_accepts_vendor_prefixed_model_name(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = FakeRunner(
        {
            "nvidia-smi --query-gpu=name,memory.total,pci.bus_id": _completed(
                [],
                stdout="NVIDIA TITAN RTX, 24576 MiB, 0000:65:00.0\n",
            ),
            "AE_CRI_REQUIRE_RUNTIME_READY=1": _completed([], stdout=_runtime_ready_output()),
            "AE_CRI_VECTORADD_IMAGE=": _completed([], stdout=_compute_success_output()),
        }
    )
    payload = gpu_guest_passthrough_validate.run_validation(config, runner=runner)
    attach = json.loads(
        Path(payload["artifacts"]["attach"]).read_text(encoding="utf-8")
    )
    assert attach["assertions"]["model_match"] is True


def test_gpu_guest_passthrough_validate_rejects_undersized_vram(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = FakeRunner(
        {
            "nvidia-smi --query-gpu=name,memory.total,pci.bus_id": _completed(
                [],
                stdout="TITAN RTX, 16000 MiB, 0000:65:00.0\n",
            ),
            "AE_CRI_REQUIRE_RUNTIME_READY=1": _completed([], stdout=_runtime_ready_output()),
            "AE_CRI_VECTORADD_IMAGE=": _completed([], stdout=_compute_success_output()),
        }
    )
    payload = gpu_guest_passthrough_validate.run_validation(config, runner=runner)
    attach = json.loads(
        Path(payload["artifacts"]["attach"]).read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert attach["assertions"]["min_vram"] is False


def test_gpu_guest_passthrough_validate_fails_when_runtime_handler_is_missing(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runner = FakeRunner(
        {
            "nvidia-smi --query-gpu=name,memory.total,pci.bus_id": _completed(
                [],
                stdout="TITAN RTX, 24576 MiB, 0000:65:00.0\n",
            ),
            "AE_CRI_REQUIRE_RUNTIME_READY=1": _completed(
                [],
                returncode=1,
                stdout=_runtime_ready_output(handlers="runc"),
                stderr="required CRI runtime handler 'nvidia' is unavailable\n",
            ),
            "AE_CRI_VECTORADD_IMAGE=": _completed([], stdout=_compute_success_output()),
        }
    )
    payload = gpu_guest_passthrough_validate.run_validation(config, runner=runner)
    runtime = json.loads(
        Path(payload["artifacts"]["cri_runtime"]).read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert runtime["status"] == "failed"
    assert runtime["assertions"]["runtime_handler_available"] is False


def test_gpu_guest_passthrough_validate_fails_when_compute_smoke_lacks_success_signal(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    runner = FakeRunner(
        {
            "nvidia-smi --query-gpu=name,memory.total,pci.bus_id": _completed(
                [],
                stdout="TITAN RTX, 24576 MiB, 0000:65:00.0\n",
            ),
            "AE_CRI_REQUIRE_RUNTIME_READY=1": _completed([], stdout=_runtime_ready_output()),
            "AE_CRI_VECTORADD_IMAGE=": _completed(
                [],
                returncode=1,
                stdout="[Vector addition of 50000 elements]\nDone\n",
                stderr="GPU compute smoke missing success signal: Test PASSED\n",
            ),
        }
    )
    payload = gpu_guest_passthrough_validate.run_validation(config, runner=runner)
    compute = json.loads(
        Path(payload["artifacts"]["compute_smoke"]).read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert compute["status"] == "failed"
    assert compute["assertions"]["success_signal_present"] is False


def test_gpu_guest_passthrough_validate_resolves_vm_name_from_inventory_and_writes_stable_summary(
    tmp_path: Path,
) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps([{"name": "a-node-1", "ip": "192.0.2.25"}]),
        encoding="utf-8",
    )
    config = _make_config(
        tmp_path,
        guest_ip=None,
        vm_name="a-node-1",
        inventory=inventory,
        expected_pci_bus_id="0000:65:00.0",
    )
    runner = FakeRunner(
        {
            "nvidia-smi --query-gpu=name,memory.total,pci.bus_id": _completed(
                [],
                stdout="TITAN RTX, 24576 MiB, 0000:65:00.0\n",
            ),
            "AE_CRI_REQUIRE_RUNTIME_READY=1": _completed([], stdout=_runtime_ready_output()),
            "AE_CRI_VECTORADD_IMAGE=": _completed([], stdout=_compute_success_output()),
        }
    )
    payload = gpu_guest_passthrough_validate.run_validation(config, runner=runner)
    summary = json.loads(
        Path(payload["artifacts"]["summary"]).read_text(encoding="utf-8")
    )
    assert payload["status"] == "passed"
    assert summary["status"] == "passed"
    assert summary["guest"]["guest_ip"] == "192.0.2.25"
    assert summary["expected"]["gpu_family"] == "TITAN RTX"
    assert summary["checks"] == {
        "egpu_attach": "passed",
        "egpu_cri_runtime": "passed",
        "egpu_compute_smoke": "passed",
    }
    assert summary["detected"]["gpu"]["pci_bus_id"] == "0000:65:00.0"


def test_gpu_guest_passthrough_validate_prefers_host_a_inventory_and_guest_repo(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(gpu_guest_passthrough_validate, "ROOT", tmp_path)
    inventory = tmp_path / "state" / "libvirt-host-a" / "k1s-core-a-gpu" / "inventory.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(
        json.dumps(
            [
                {
                    "name": "k1s-core-a-gpu",
                    "ip": "192.0.2.44",
                    "guest_repo": "/home/ae/k1s",
                    "guest_user": "ae",
                }
            ]
        ),
        encoding="utf-8",
    )
    config = _make_config(
        tmp_path,
        guest_ip=None,
        vm_name="k1s-core-a-gpu",
        inventory=None,
        guest_repo=None,
    )
    runner = FakeRunner(
        {
            "nvidia-smi --query-gpu=name,memory.total,pci.bus_id": _completed(
                [],
                stdout="TITAN RTX, 24576 MiB, 0000:65:00.0\n",
            ),
            "/home/ae/k1s/scripts/cri_preflight.sh": _completed([], stdout=_runtime_ready_output()),
            "/home/ae/k1s/scripts/cri_cuda_vectoradd_smoke.sh": _completed(
                [], stdout=_compute_success_output()
            ),
        }
    )

    payload = gpu_guest_passthrough_validate.run_validation(config, runner=runner)
    runtime = json.loads(Path(payload["artifacts"]["cri_runtime"]).read_text(encoding="utf-8"))
    compute = json.loads(Path(payload["artifacts"]["compute_smoke"]).read_text(encoding="utf-8"))
    summary = json.loads(Path(payload["artifacts"]["summary"]).read_text(encoding="utf-8"))

    assert payload["status"] == "passed"
    assert summary["guest"]["guest_ip"] == "192.0.2.44"
    assert summary["guest"]["inventory"] == str(inventory)
    assert summary["guest"]["guest_repo"] == "/home/ae/k1s"
    assert "/home/ae/k1s/scripts/cri_preflight.sh" in runtime["command"]
    assert "/home/ae/k1s/scripts/cri_cuda_vectoradd_smoke.sh" in compute["command"]
