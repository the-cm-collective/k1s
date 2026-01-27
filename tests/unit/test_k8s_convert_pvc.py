from ae.k8s.convert import manifest_from_k8s_workload


def test_convert_pvc_mounts() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": "nginx:latest",
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": "data-pvc"}}
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    mounts = list(getattr(man.spec, "pvc_mounts", []) or [])
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount.claim_name == "data-pvc"
    assert mount.mount_path == "/data"
    assert mount.read_only is True


def test_convert_pvc_volume_read_only() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": "nginx:latest",
                            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "data",
                            "persistentVolumeClaim": {
                                "claimName": "data-pvc",
                                "readOnly": True,
                            },
                        }
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    mounts = list(getattr(man.spec, "pvc_mounts", []) or [])
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount.claim_name == "data-pvc"
    assert mount.mount_path == "/data"
    assert mount.read_only is True
