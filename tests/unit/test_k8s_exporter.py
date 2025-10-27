from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_docs, export_k8s_yaml
from ae.k8s.check import k8s_portability_issues


def test_export_k8s_minimal_echo(tmp_path: Path) -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    opts = ExportOptions(namespace="demo", ingress_class_name="traefik", service_port=80)
    docs = export_k8s_docs(man, options=opts)

    # Expect Deployment, Service, Ingress
    kinds = [d["kind"] for d in docs]
    assert kinds == ["Deployment", "Service", "Ingress"]

    dep = docs[0]
    assert dep["apiVersion"] == "apps/v1"
    assert dep["metadata"]["name"] == man.metadata.name
    assert dep["metadata"]["namespace"] == "demo"
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == man.spec.image
    assert any(p.get("containerPort") == 8080 for p in c.get("ports", []))
    # env includes valueFrom references for config/secret mappings
    env = {e["name"]: e for e in c.get("env", [])}
    assert env.get("APP_MODE")["valueFrom"]["configMapKeyRef"]["name"] == "app-config"
    assert env.get("APP_MODE")["valueFrom"]["configMapKeyRef"]["key"] == "mode"
    assert env.get("API_TOKEN")["valueFrom"]["secretKeyRef"]["name"] == "demo-secret"
    assert env.get("API_TOKEN")["valueFrom"]["secretKeyRef"]["key"] == "token"

    svc = docs[1]
    assert svc["kind"] == "Service"
    port = svc["spec"]["ports"][0]
    assert port["port"] == 80 and port["targetPort"] == 8080

    ing = docs[2]
    assert ing["spec"]["ingressClassName"] == "traefik"
    rule = ing["spec"]["rules"][0]
    assert rule["host"] == man.spec.ingress.host

    # YAML render sanity
    out = export_k8s_yaml(man, options=opts)
    assert "kind: Deployment" in out and "kind: Service" in out and "kind: Ingress" in out


def test_k8s_check_echo_manifest() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    issues = k8s_portability_issues(man)
    # Should be only warnings (e.g., multi-arch unknown, maybe security)
    assert all(i.level in {"warn"} for i in issues)


def test_export_with_storage_and_serviceaccount() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Fake storage entry to test PVC emission
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "storage": [
                        {"name": "data", "mountPath": "/data", "retention": "Retain", "size": "2Gi"}
                    ]
                }
            )
        }
    )
    # Bump replicas so PDB is eligible
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"replicas": 2})})
    opts = ExportOptions(namespace="demo", emit_storage=True, service_account_name="web-sa", emit_pdb=True, pdb_min_available=2, hpa_min=1, hpa_max=3, hpa_cpu_target=70, hpa_mem_target=75, default_security=True)
    docs = export_k8s_docs(man, options=opts)
    kinds = [d["kind"] for d in docs]
    assert "PersistentVolumeClaim" in kinds
    assert "PodDisruptionBudget" in kinds
    assert "HorizontalPodAutoscaler" in kinds
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert pod.get("serviceAccountName") == "web-sa"
    vols = pod.get("volumes", [])
    vms = pod["containers"][0].get("volumeMounts", [])
    assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "echo-data" for v in vols)
    assert any(vm.get("mountPath") == "/data" for vm in vms)
    # default security applied when none provided
    sc = pod["containers"][0].get("securityContext", {})
    assert sc.get("readOnlyRootFilesystem") is True
    # PDB has the requested minAvailable
    pdb = next(d for d in docs if d["kind"] == "PodDisruptionBudget")
    assert int(pdb["spec"]["minAvailable"]) == 2
    # HPA has both CPU and memory metrics
    hpa = next(d for d in docs if d["kind"] == "HorizontalPodAutoscaler")
    names = [m["resource"]["name"] for m in hpa["spec"]["metrics"]]
    assert set(names) >= {"cpu", "memory"}
def test_pdb_max_unavailable() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"replicas": 2})})
    opts = ExportOptions(namespace="demo", emit_pdb=True, pdb_max_unavailable=1)
    docs = export_k8s_docs(man, options=opts)
    pdb = next(d for d in docs if d["kind"] == "PodDisruptionBudget")
    assert pdb["spec"].get("maxUnavailable") == 1
    assert "minAvailable" not in pdb["spec"]


def test_hpa_memory_average_value() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # need HPA bounds
    opts = ExportOptions(namespace="demo", hpa_min=1, hpa_max=3, hpa_mem_type="value", hpa_mem_value="200Mi")
    docs = export_k8s_docs(man, options=opts)
    hpa = next(d for d in docs if d["kind"] == "HorizontalPodAutoscaler")
    mem = next(m for m in hpa["spec"]["metrics"] if m["resource"]["name"] == "memory")
    assert mem["resource"]["target"]["type"] == "AverageValue"
    assert mem["resource"]["target"]["averageValue"] == "200Mi"
