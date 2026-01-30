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


def test_convert_hostpath_mounts() -> None:
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
                                {"name": "host", "mountPath": "/data", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "host", "hostPath": {"path": "/var/lib/demo"}},
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    vols = list(getattr(man.spec, "volumes", []) or [])
    assert len(vols) == 1
    vol = vols[0]
    assert vol.host_path == "/var/lib/demo"
    assert vol.mount_path == "/data"
    assert vol.read_only is True


def test_convert_pvc_volume_devices() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": "nginx:latest",
                            "volumeDevices": [{"name": "block", "devicePath": "/dev/xvdb"}],
                        }
                    ],
                    "volumes": [
                        {"name": "block", "persistentVolumeClaim": {"claimName": "blk-pvc"}}
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    mounts = list(getattr(man.spec, "pvc_mounts", []) or [])
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount.claim_name == "blk-pvc"
    assert mount.device_path == "/dev/xvdb"
    assert mount.mount_path == "/dev/xvdb"


def test_convert_pvc_mount_subpath() -> None:
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
                                {"name": "data", "mountPath": "/data", "subPath": "cache"}
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
    assert mount.sub_path == "cache"
