import json
from pathlib import Path
from types import SimpleNamespace

from ae.cli.__main__ import handle_plan


class DummyRuntime:
    def list_containers_info(self):
        return []


def write_manifest(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "app.yaml"
    p.write_text(content)
    return p


def test_plan_rollout_bounds_warnings(tmp_path):
    man = """
apiVersion: ae.dev/v1alpha1
kind: App
metadata: { name: test }
spec:
  image: alpine:3
  replicas: 2
  rollout:
    maxSurge: 3
    maxUnavailable: 2
"""
    f = write_manifest(tmp_path, man)
    args = SimpleNamespace(file=f, verbose=False, strict=True, json=True)
    # Stash stdout
    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = handle_plan(args, DummyRuntime())
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert rc != 0
    warns = "\n".join(out.get("warnings", []))
    assert "maxSurge" in warns
    assert "maxUnavailable" in warns


def test_plan_hook_schema_validation(tmp_path):
    man = """
apiVersion: ae.dev/v1alpha1
kind: App
metadata: { name: testhooks }
spec:
  image: alpine:3
  replicas: 1
  rollout:
    hooks:
      preSwitch: {}
      postSwitch:
        tcp: { port: 70000 }
"""
    f = write_manifest(tmp_path, man)
    args = SimpleNamespace(file=f, verbose=False, strict=True, json=True)
    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = handle_plan(args, DummyRuntime())
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    warns = "\n".join(out.get("warnings", []))
    assert rc != 0
    assert "must contain 'exec' or 'tcp'" in warns
    assert "tcp.port" in warns
