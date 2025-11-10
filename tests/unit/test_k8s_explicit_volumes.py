from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_docs


def test_explicit_config_secret_volume_items_and_modes() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # add explicit mode on a file entry
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "secret_refs": [
                        man.spec.secret_refs[0].model_copy(update={"files": [{"key": "token", "file": "secret/token", "mode": 0o440}]})
                    ]
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo", emit_configs=True, emit_secrets=True))
    dep = next(d for d in docs if d.get("kind") == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    vols = pod.get("volumes", [])
    # One of the volumes must be a Secret with items and include the mode we set
    sec = next(v for v in vols if v.get("secret") and v["secret"].get("items"))
    item = sec["secret"]["items"][0]
    assert item["key"] == "token"
    assert item["path"] == "secret/token"
    assert int(item.get("mode", 0)) == 0o440

