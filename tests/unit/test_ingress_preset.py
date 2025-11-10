from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_docs
from ae.k8s.presets import apply_ingress_preset


def test_ingress_preset_nginx_web_adds_annotations() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    opts = ExportOptions(namespace="demo", ingress_class_name="nginx")
    opts = apply_ingress_preset(opts, "nginx-web")
    docs = export_k8s_docs(manifest=man, options=opts)
    ing = next(d for d in docs if d["kind"] == "Ingress")
    ann = ing["metadata"].get("annotations", {})
    assert ann.get("nginx.ingress.kubernetes.io/proxy-read-timeout") == "60"


def test_ingress_preset_traefik_web_adds_entrypoints() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    opts = ExportOptions(namespace="demo", ingress_class_name="traefik")
    opts = apply_ingress_preset(opts, "traefik-web")
    docs = export_k8s_docs(manifest=man, options=opts)
    ing = next(d for d in docs if d["kind"] == "Ingress")
    ann = ing["metadata"].get("annotations", {})
    assert ann.get("traefik.ingress.kubernetes.io/router.entrypoints")
