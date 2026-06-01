from __future__ import annotations

from ae.accelerators import (
    detect_nvidia_accelerator_capabilities,
    has_accelerator_inventory,
    has_identity_role_separation,
    link_metric_inventory,
    merge_projected_gpu_labels,
    network_interface_inventory,
    normalize_capabilities,
    preferred_gpu_count,
    preferred_gpu_models,
    rdma_device_inventory,
    storage_device_inventory,
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


def test_normalize_capabilities_accepts_f1_camelcase_facts() -> None:
    caps = normalize_capabilities(
        {
            "storageDevices": [
                {
                    "id": "nvme0",
                    "mediaType": "nvme",
                    "sizeBytes": "1024",
                    "mountPath": "/models",
                    "roles": "model-cache",
                }
            ],
            "networkInterfaces": [
                {
                    "name": "enp1s0f0",
                    "speedMbps": "25000",
                    "siteId": "site-a",
                    "roles": ["fabric"],
                    "linkMetrics": [
                        {
                            "fromSite": "site-a",
                            "toSite": "site-b",
                            "rttP95Ms": "4.5",
                            "jitterP95Ms": "0.2",
                            "lossPct": "0",
                        }
                    ],
                }
            ],
            "rdmaDevices": [
                {
                    "name": "irdma0",
                    "netDevice": "enp1s0f0",
                    "rdmaProtocols": ["roce"],
                    "pcieBusId": "0000:01:00.0",
                    "pcieLinkWidth": "4",
                    "pcieLinkSpeedGTs": "16",
                    "state": "active",
                }
            ],
            "identityRoles": {
                "management": "spiffe://node-a/management",
                "execution": {"id": "spiffe://node-a/execution"},
                "fabric": {"principal": "spiffe://node-a/fabric"},
            },
        }
    )

    assert "storageDevices" not in caps
    assert caps["storage_devices"][0]["medium"] == "nvme"
    assert caps["storage_devices"][0]["size_bytes"] == 1024
    assert storage_device_inventory(caps)[0]["roles"] == ["model-cache"]
    assert "networkInterfaces" not in caps
    assert network_interface_inventory(caps)[0]["speed_mbps"] == 25000
    assert link_metric_inventory(caps)[0]["from_site"] == "site-a"
    assert link_metric_inventory(caps)[0]["rtt_p95_ms"] == 4.5
    assert rdma_device_inventory(caps)[0]["pcie"]["bus_id"] == "0000:01:00.0"
    assert rdma_device_inventory(caps)[0]["pcie"]["link_width"] == 4
    assert has_identity_role_separation(caps) is True


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
        _ = (stderr, text, timeout)
        assert "--query-gpu=index,uuid,name,architecture,memory.total" in cmd[1]
        return sample

    monkeypatch.setattr("ae.accelerators.subprocess.check_output", fake_check_output)
    caps = detect_nvidia_accelerator_capabilities(smi_bin="nvidia-smi")
    assert caps["accelerators"][0]["id"] == "GPU-AAAA"
    assert caps["accelerators"][0]["family"] == "TITAN RTX"
    assert caps["accelerators"][0]["memory_bytes_per_device"] == 24576 * 1024 * 1024
    assert caps["accelerators"][1]["id"] == "GPU-BBBB"
    assert caps["accelerators"][1]["family"] == "RTX 8000"
