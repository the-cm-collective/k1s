from ae.k8s.convert import manifest_from_k8s_workload


def test_statefulset_volume_claim_templates_map_to_pvc_mounts() -> None:
    sts = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "default"},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "db"}},
            "template": {
                "metadata": {"labels": {"app": "db"}},
                "spec": {
                    "containers": [
                        {
                            "name": "db",
                            "image": "busybox",
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/var/lib/data"}
                            ],
                        }
                    ]
                },
            },
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data"},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": "1Gi"}},
                    },
                }
            ],
        },
    }

    manifest = manifest_from_k8s_workload(
        sts, volume_claim_templates=sts["spec"]["volumeClaimTemplates"]
    )

    assert manifest.spec.pvc_mounts
    pm = manifest.spec.pvc_mounts[0]
    assert pm.claim_name == "data"
    assert pm.claim_template is True
    assert pm.mount_path == "/var/lib/data"


def test_convert_preserves_gpu_resources_and_runtime_class() -> None:
    dep = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "gpu-smoke", "namespace": "k1s-dev-a"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "runtimeClassName": "nvidia",
                    "hostNetwork": True,
                    "nodeSelector": {"nvidia.com/gpu.present": "true"},
                    "containers": [
                        {
                            "name": "gpu-smoke",
                            "image": "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1",
                            "resources": {
                                "requests": {"memory": "256Mi", "nvidia.com/gpu": 1},
                                "limits": {"memory": "512Mi", "nvidia.com/gpu": 1},
                            },
                        }
                    ],
                }
            },
        },
    }

    manifest = manifest_from_k8s_workload(dep)

    assert manifest.spec.runtime_class_name == "nvidia"
    assert manifest.spec.host_network is True
    assert manifest.spec.node_selector == {"nvidia.com/gpu.present": "true"}
    assert manifest.spec.resources is not None
    assert manifest.spec.resources.requests is not None
    assert manifest.spec.resources.limits is not None
    assert manifest.spec.resources.requests.memory == "256Mi"
    assert manifest.spec.resources.requests.quantity_map()["nvidia.com/gpu"] == 1
    assert manifest.spec.resources.limits.quantity_map()["nvidia.com/gpu"] == 1
