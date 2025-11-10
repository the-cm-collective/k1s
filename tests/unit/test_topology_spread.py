from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_docs


def test_inject_topology_spread_when_replicas_gt1() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"replicas": 2})})
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo", inject_topology_spread=True))
    dep = next(d for d in docs if d.get("kind") == "Deployment")
    tsc = dep["spec"]["template"]["spec"].get("topologySpreadConstraints", [])
    assert tsc and tsc[0].get("topologyKey") == "kubernetes.io/hostname"
    assert tsc[0].get("labelSelector", {}).get("matchLabels", {}).get("app") == man.metadata.name
