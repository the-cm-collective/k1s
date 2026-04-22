from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bench" / "pin_runtime_class.py"


def _load_docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def test_pin_runtime_class_adds_runc_without_mutating_source(tmp_path: Path) -> None:
    source = tmp_path / "echo.yaml"
    source.write_text(
        """\
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: echo
spec:
  image: docker.io/mendhak/http-https-echo:37
""",
        encoding="utf-8",
    )
    output = tmp_path / "pinned.yaml"

    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), str(source), str(output)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert proc.stdout.strip() == str(output)
    original_docs = _load_docs(source)
    pinned_docs = _load_docs(output)

    assert original_docs[0]["spec"].get("runtimeClassName") is None
    assert pinned_docs[0]["spec"]["runtimeClassName"] == "runc"


def test_pin_runtime_class_replaces_existing_runtime_class_and_preserves_other_docs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "multi.yaml"
    source.write_text(
        """\
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: echo
spec:
  image: docker.io/mendhak/http-https-echo:37
  runtimeClassName: kata
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: untouched
data:
  mode: demo
""",
        encoding="utf-8",
    )
    output = tmp_path / "pinned.yaml"

    subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), str(source), str(output)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    pinned_docs = _load_docs(output)
    assert pinned_docs[0]["spec"]["runtimeClassName"] == "runc"
    assert pinned_docs[1]["kind"] == "ConfigMap"
    assert pinned_docs[1]["data"]["mode"] == "demo"


def test_pin_runtime_class_fails_without_deployment_or_app(tmp_path: Path) -> None:
    source = tmp_path / "configmap.yaml"
    source.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: only-config
data:
  mode: demo
""",
        encoding="utf-8",
    )
    output = tmp_path / "pinned.yaml"

    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), str(source), str(output)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert "does not contain an app/deployment spec" in proc.stderr
    assert not output.exists()
