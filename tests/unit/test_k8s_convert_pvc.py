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


def test_convert_service_account_name() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "serviceAccountName": "puller",
                    "containers": [{"image": "nginx:latest"}],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert man.spec.service_account_name == "puller"


def test_convert_service_account_default() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"image": "nginx:latest"}]}},
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert man.spec.service_account_name == "default"


def test_convert_image_pull_secrets() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "imagePullSecrets": [{"name": "regcred"}, {"name": "mirror"}],
                    "containers": [{"image": "nginx:latest"}],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert man.spec.image_pull_secrets == ["regcred", "mirror"]


def test_convert_registry_auth_ref_single_secret() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "imagePullSecrets": [{"name": "regcred"}],
                    "containers": [{"image": "nginx:latest"}],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert man.spec.registry_auth_ref == "regcred"


def test_convert_image_pull_secrets_from_service_account(monkeypatch) -> None:
    def fake_pull_secrets(namespace, service_account, db_path=None, dsn=None):  # noqa: ANN001
        if namespace == "demo" and service_account == "puller":
            return ["regcred"]
        return []

    monkeypatch.setattr(
        "ae.k8s.convert._service_account_pull_secrets", fake_pull_secrets, raising=True
    )
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "serviceAccountName": "puller",
                    "containers": [{"image": "nginx:latest"}],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert man.spec.image_pull_secrets == ["regcred"]


def test_convert_empty_pull_secrets_no_sa_fallback(monkeypatch) -> None:
    def fake_pull_secrets(namespace, service_account, db_path=None, dsn=None):  # noqa: ANN001
        return ["regcred"]

    monkeypatch.setattr(
        "ae.k8s.convert._service_account_pull_secrets", fake_pull_secrets, raising=True
    )
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "serviceAccountName": "puller",
                    "imagePullSecrets": [],
                    "containers": [{"image": "nginx:latest"}],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert man.spec.image_pull_secrets == []


def test_convert_image_pull_policy() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {"image": "nginx:latest", "imagePullPolicy": "IfNotPresent"}
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert man.spec.image_pull_policy == "IfNotPresent"


def test_convert_init_containers() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [{"name": "main", "image": "nginx:latest"}],
                    "initContainers": [
                        {
                            "name": "init",
                            "image": "alpine:3.20",
                            "command": ["/bin/sh", "-c"],
                            "args": ["echo init"],
                            "env": [{"name": "MODE", "value": "test"}],
                            "imagePullPolicy": "IfNotPresent",
                        }
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert len(man.spec.init_containers) == 1
    init = man.spec.init_containers[0]
    assert init.name == "init"
    assert init.image == "alpine:3.20"
    assert init.command == ["/bin/sh", "-c"]
    assert init.args == ["echo init"]
    assert init.env == [{"name": "MODE", "value": "test"}]
    assert init.image_pull_policy == "IfNotPresent"


def test_convert_sidecar_image_pull_policy() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {"name": "main", "image": "nginx:latest", "imagePullPolicy": "IfNotPresent"},
                        {"name": "sidecar", "image": "busybox:latest", "imagePullPolicy": "Always"},
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    assert man.spec.image_pull_policy == "IfNotPresent"
    assert len(man.spec.containers) == 1
    sidecar = man.spec.containers[0]
    assert sidecar.name == "sidecar"
    assert sidecar.image == "busybox:latest"
    assert sidecar.image_pull_policy == "Always"


def test_convert_sidecar_envfrom_valuefrom() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {"name": "main", "image": "nginx:latest"},
                        {
                            "name": "sidecar",
                            "image": "busybox:latest",
                            "env": [
                                {
                                    "name": "POD_NAME",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                }
                            ],
                            "envFrom": [{"configMapRef": {"name": "app-config"}}],
                        },
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    sidecar = man.spec.containers[0]
    assert any(
        e.get("name") == "POD_NAME" and isinstance(e.get("valueFrom"), dict)
        for e in sidecar.env
    )
    assert any(
        e.get("name") == ""
        and isinstance(e.get("valueFrom"), dict)
        and "configMapKeyRef" in e["valueFrom"]
        for e in sidecar.env
    )


def test_convert_envfrom_prefix() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "main",
                            "image": "nginx:latest",
                            "envFrom": [
                                {"configMapRef": {"name": "app-config"}, "prefix": "APP_"},
                                {"secretRef": {"name": "app-secret"}, "prefix": "SEC_"},
                            ],
                        }
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    env = man.spec.env
    cm_marker = next(
        (
            e
            for e in env
            if e.get("name") == ""
            and isinstance(e.get("valueFrom"), dict)
            and "configMapKeyRef" in e["valueFrom"]
        ),
        None,
    )
    sec_marker = next(
        (
            e
            for e in env
            if e.get("name") == ""
            and isinstance(e.get("valueFrom"), dict)
            and "secretKeyRef" in e["valueFrom"]
        ),
        None,
    )
    assert cm_marker is not None
    assert cm_marker["valueFrom"]["configMapKeyRef"]["prefix"] == "APP_"
    assert sec_marker is not None
    assert sec_marker["valueFrom"]["secretKeyRef"]["prefix"] == "SEC_"


def test_convert_sidecar_volume_mounts_and_devices() -> None:
    dep = {
        "metadata": {"name": "app", "namespace": "demo"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {"name": "main", "image": "nginx:latest"},
                        {
                            "name": "sidecar",
                            "image": "busybox:latest",
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {"name": "host", "mountPath": "/cache", "readOnly": True},
                            ],
                            "volumeDevices": [
                                {"name": "block", "devicePath": "/dev/xvdb"},
                                {"name": "rawdev", "devicePath": "/dev/xvdc"},
                            ],
                        },
                    ],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": "data-pvc"}},
                        {"name": "block", "persistentVolumeClaim": {"claimName": "blk-pvc"}},
                        {"name": "host", "hostPath": {"path": "/var/lib/demo"}},
                        {"name": "rawdev", "hostPath": {"path": "/dev/loop0"}},
                    ],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(dep)
    sidecar = man.spec.containers[0]
    assert any(pm.claim_name == "data-pvc" and pm.mount_path == "/data" for pm in sidecar.pvc_mounts)
    assert any(
        pm.claim_name == "blk-pvc" and pm.device_path == "/dev/xvdb" for pm in sidecar.pvc_mounts
    )
    assert any(
        vm.host_path == "/var/lib/demo" and vm.mount_path == "/cache" and vm.read_only
        for vm in sidecar.volume_mounts
    )
    assert any(
        vd.host_path == "/dev/loop0" and vd.device_path == "/dev/xvdc" for vd in sidecar.volume_devices
    )


def test_convert_job_service_account_name() -> None:
    job = {
        "metadata": {"name": "job", "namespace": "demo"},
        "spec": {
            "parallelism": 1,
            "template": {
                "spec": {
                    "serviceAccountName": "job-sa",
                    "containers": [{"image": "busybox:latest"}],
                }
            },
        },
    }
    man = manifest_from_k8s_workload(job)
    assert man.spec.service_account_name == "job-sa"


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
