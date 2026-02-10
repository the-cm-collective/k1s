from ae.storage.config import load_storage_registry


def test_load_storage_registry_from_provisioners(tmp_path) -> None:
    path = tmp_path / "prov.yaml"
    path.write_text(
        """
provisioners:
  - name: csi-fast
    provisioner: csi.example.com
    type: csi
    controllerEndpoint: unix:///tmp/csi.sock
    nodeEndpoint: unix:///tmp/csi.sock
    mountOptions:
      - noatime
""",
        encoding="utf-8",
    )
    classes, registry = load_storage_registry(path)

    assert any(sc.name == "csi-fast" for sc in classes)
    entry = registry.for_storage_class("csi-fast")
    assert entry is not None
    assert entry.type == "csi"
    assert entry.node_endpoint == "unix:///tmp/csi.sock"
