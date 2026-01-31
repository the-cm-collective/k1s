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
