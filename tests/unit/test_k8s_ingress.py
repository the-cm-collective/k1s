from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_docs


def test_ingress_multi_path_and_tls_secret() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # inject multi-paths and tlsSecretName
    ing = man.spec.ingress
    assert ing is not None
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "ingress": ing.model_copy(
                        update={"paths": ["/", "/api"], "tls_secret_name": "echo-tls"}
                    )
                }
            )
        }
    )
    opts = ExportOptions(namespace="demo", service_port=80, ingress_class_name="traefik")
    docs = export_k8s_docs(manifest=man, options=opts)
    ing_doc = next(d for d in docs if d["kind"] == "Ingress")
    paths = ing_doc["spec"]["rules"][0]["http"]["paths"]
    assert any(p.get("path") == "/api" for p in paths)
    tls = ing_doc["spec"]["tls"][0]
    assert tls.get("secretName") == "echo-tls"


def test_traefik_rejects_exact_pathtype() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    opts = ExportOptions(namespace="demo", ingress_class_name="traefik", ingress_path_type="Exact")
    import pytest

    with pytest.raises(ValueError):
        export_k8s_docs(manifest=man, options=opts)


def test_ingress_annotations_passthrough_requires_flag() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    ing = man.spec.ingress
    assert ing is not None
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "ingress": ing.model_copy(
                        update={"annotations": {"nginx.ingress.kubernetes.io/rewrite-target": "/"}}
                    )
                }
            )
        }
    )
    opts = ExportOptions(namespace="demo", ingress_class_name="nginx")
    ing_doc = next(d for d in export_k8s_docs(manifest=man, options=opts) if d["kind"] == "Ingress")
    assert "annotations" not in ing_doc["metadata"]

    opts = ExportOptions(
        namespace="demo",
        ingress_class_name="nginx",
        allow_ingress_annotations=True,
        ingress_annotations={"nginx.ingress.kubernetes.io/proxy-read-timeout": "60"},
    )
    ing_doc = next(d for d in export_k8s_docs(manifest=man, options=opts) if d["kind"] == "Ingress")
    ann = ing_doc["metadata"].get("annotations", {})
    assert ann.get("nginx.ingress.kubernetes.io/rewrite-target") == "/"
    # Explicit flag values override spec values
    assert ann.get("nginx.ingress.kubernetes.io/proxy-read-timeout") == "60"
