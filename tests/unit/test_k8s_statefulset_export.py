from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_docs


def test_statefulset_export_with_storage_and_headless_service(tmp_path: Path) -> None:
    _ = tmp_path
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Add a storage item to trigger volumeClaimTemplates
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "storage": [
                        {"name": "data", "mountPath": "/data", "retention": "Retain", "size": "1Gi"}
                    ]
                }
            )
        }
    )
    opts = ExportOptions(
        workload_kind="StatefulSet",
        namespace="demo",
        emit_storage=True,
        default_pvc_size="1Gi",
    )
    docs = export_k8s_docs(man, options=opts)

    kinds = [d["kind"] for d in docs]
    # Expect headless Service first, then StatefulSet, Service, Ingress
    assert "StatefulSet" in kinds
    assert any(d["kind"] == "Service" and d["metadata"]["name"].endswith("-headless") for d in docs)

    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    assert sts["apiVersion"] == "apps/v1"
    assert sts["metadata"]["name"] == man.metadata.name
    assert sts["spec"]["serviceName"] == f"{man.metadata.name}-headless"
    # volumeClaimTemplates should be present with our storage
    vcts = sts["spec"].get("volumeClaimTemplates", [])
    assert vcts and vcts[0]["metadata"]["name"] == f"{man.metadata.name}-data"
    # No standalone PVC objects for StatefulSet
    assert not any(d["kind"] == "PersistentVolumeClaim" for d in docs)


def test_statefulset_storage_overrides_storageclass_and_accessmodes() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "replicas": 2,
                    "storage": [
                        {"name": "data", "mountPath": "/data", "retention": "Retain", "size": "2Gi"}
                    ],
                }
            )
        }
    )
    opts = ExportOptions(
        workload_kind="StatefulSet",
        namespace="demo",
        emit_storage=True,
        default_pvc_size="1Gi",
        storage_class_name="fast",
        pvc_access_modes=["ReadWriteMany"],
    )
    docs = export_k8s_docs(man, options=opts)
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    tmpl = sts["spec"].get("volumeClaimTemplates", [])[0]
    spec = tmpl.get("spec", {})
    assert spec.get("resources", {}).get("requests", {}).get("storage") == "2Gi"
    assert spec.get("storageClassName") == "fast"
    assert spec.get("accessModes") == ["ReadWriteMany"]
