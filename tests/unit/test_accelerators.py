from __future__ import annotations

from ae.accelerators import (
    detect_nvidia_accelerator_capabilities,
    has_accelerator_inventory,
    merge_projected_gpu_labels,
    normalize_capabilities,
    preferred_gpu_count,
    preferred_gpu_models,
)


def test_normalize_capabilities_preserves_accelerator_shape() -> None:
    caps = normalize_capabilities(
        {
            "accelerators": [
                {
                    "vendor": "nvidia",
                    "family": "RTX 8000",
                    "device_count": "2",
                    "runtime_handlers": ["nvidia"],
                }
            ]
        }
    )
    assert has_accelerator_inventory(caps) is True
    accelerator = caps["accelerators"][0]
    assert accelerator["id"] == "accelerator-0"
    assert accelerator["kind"] == "discrete_gpu"
    assert accelerator["vendor"] == "nvidia"
    assert accelerator["family"] == "RTX 8000"
    assert accelerator["device_count"] == 2


def test_merge_projected_gpu_labels_prefers_typed_inventory() -> None:
    capabilities = {
        "accelerators": [
            {
                "id": "gpu-0",
                "kind": "discrete_gpu",
                "vendor": "nvidia",
                "family": "TITAN RTX",
                "device_count": 1,
                "memory_model": "dedicated",
                "memory_bytes_per_device": 25769803776,
                "runtime_handlers": ["nvidia"],
                "partitioning_mode": "none",
                "backing_device_id": None,
                "execution_role": "execution",
            }
        ]
    }
    labels = merge_projected_gpu_labels({"site": "core-a", "gpu.count": "99"}, capabilities)
    assert labels["site"] == "core-a"
    assert labels["gpu.present"] == "true"
    assert labels["gpu.count"] == "1"
    assert labels["gpu.models"] == "TITAN RTX"
    assert preferred_gpu_count(labels, capabilities) == 1
    assert preferred_gpu_models(labels, capabilities) == "TITAN RTX"


def test_detect_nvidia_accelerator_capabilities(monkeypatch) -> None:
    sample = "\n".join(
        [
            "0, GPU-AAAA, TITAN RTX, Turing, 24576",
            "1, GPU-BBBB, RTX 8000, Turing, 49152",
        ]
    )

    def fake_check_output(cmd, stderr=None, text=None, timeout=None):  # noqa: ANN001
        assert "--query-gpu=index,uuid,name,architecture,memory.total" in cmd[1]
        return sample

    monkeypatch.setattr("ae.accelerators.subprocess.check_output", fake_check_output)
    caps = detect_nvidia_accelerator_capabilities(smi_bin="nvidia-smi")
    assert caps["accelerators"][0]["id"] == "GPU-AAAA"
    assert caps["accelerators"][0]["family"] == "TITAN RTX"
    assert caps["accelerators"][0]["memory_bytes_per_device"] == 24576 * 1024 * 1024
    assert caps["accelerators"][1]["id"] == "GPU-BBBB"
    assert caps["accelerators"][1]["family"] == "RTX 8000"
