from argparse import Namespace
import base64
import json
from pathlib import Path

from ae.cli.__main__ import handle_registry


def test_registry_kubesecret_renders(tmp_path: Path, monkeypatch) -> None:
    # Write a temporary registries.yaml and point AE_REGISTRY_CONFIG to it
    cfg = tmp_path / "registries.yaml"
    cfg.write_text(
        """
ghcr.io:
  username: alice
  password: secret
registry-1.docker.io:
  username: bob
  password: hunter2
        """.strip()
    )
    monkeypatch.setenv("AE_REGISTRY_CONFIG", str(cfg))

    out = tmp_path / "sec.yaml"
    code = handle_registry(
        Namespace(
            registry_cmd="kubesecret",
            name="regcred",
            namespace="demo",
            host=[],
            output=str(out),
        )
    )
    assert code == 0 and out.exists()
    text = out.read_text()
    assert "kubernetes.io/dockerconfigjson" in text
    # crude decode to verify data payload structure
    import yaml

    data = yaml.safe_load(text)
    b = data["data"][".dockerconfigjson"]
    js = json.loads(base64.b64decode(b).decode("utf-8"))
    assert "auths" in js and "ghcr.io" in js["auths"] and "registry-1.docker.io" in js["auths"]
