from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_yaml
from ae.k8s.presets import apply_preset
from ae.k8s.validate import validate_documents


def test_preset_scale_ready_and_validate() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # bump replicas to make PDB relevant
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"replicas": 2})})
    opts = ExportOptions(namespace="demo")
    opts = apply_preset(opts, "scale-ready")
    yaml_out = export_k8s_yaml(manifest=man, options=opts)
    ok, errs = validate_documents(yaml_out)
    assert ok, f"unexpected validation errors: {errs}"


def test_preset_web_hardened_defaults() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    opts = ExportOptions(namespace="demo")
    opts = apply_preset(opts, "web-hardened")
    text = export_k8s_yaml(manifest=man, options=opts)
    assert "kind: HorizontalPodAutoscaler" in text
    assert "kind: ServiceAccount" in text
