from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_docs


def test_export_multi_container_and_initcontainers() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    c1 = {"name": "web", "image": man.spec.image, "ports": man.spec.ports, "env": man.spec.env}
    c2 = {"name": "sidecar", "image": man.spec.image, "args": ["--sidecar"]}
    init = {"name": "init", "image": man.spec.image, "command": ["/bin/sh", "-c", "echo init"]}
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"containers": [c1, c2], "init_containers": [init]})})
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d.get("kind") == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    names = [c["name"] for c in pod.get("containers", [])]
    assert names == ["web", "sidecar"]
    inits = [c["name"] for c in pod.get("initContainers", [])]
    assert inits == ["init"]
