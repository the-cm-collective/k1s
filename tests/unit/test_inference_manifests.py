from pathlib import Path

import pytest

from ae.controller.spec import (
    InferenceCellManifest,
    InferenceCellSetManifest,
    ManifestError,
    load_any_manifest,
    load_manifest,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_load_any_manifest_inference_cell(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "cell.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: InferenceCell
metadata:
  name: demo-cell
spec:
  model:
    modelId: llama
    localPath: /models/llama
  parallelism:
    tp: 1
    pp: 2
  executor:
    type: ray
    fallbackMode: mp_on_failure
    dtype: half
    runtimeClassName: nvidia
  members:
    - siteId: site-a
      nodeId: node-a
      gpuCount: 1
    - siteId: site-b
      nodeId: node-b
      gpuCount: 1
  linkMetrics:
    - fromSite: site-a
      toSite: site-b
      rttP95Ms: 5
      jitterP95Ms: 0.3
      lossPct: 0.0
        """,
    )
    doc = load_any_manifest(p)
    assert isinstance(doc, InferenceCellManifest)
    assert doc.metadata.name == "demo-cell"
    assert doc.spec.executor.type == "ray"
    assert doc.spec.executor.fallback_mode == "mp_on_failure"
    assert doc.spec.executor.dtype == "half"
    assert doc.spec.executor.runtime_class_name == "nvidia"
    assert doc.spec.fabric.mode == "lan_direct"


def test_load_any_manifest_inference_cell_normalizes_blank_dtype(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "cell-blank-dtype.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: InferenceCell
metadata:
  name: demo-cell
spec:
  model:
    modelId: llama
    localPath: /models/llama
  executor:
    dtype: "   "
  members:
    - siteId: site-a
      nodeId: node-a
      gpuCount: 1
  linkMetrics: []
        """,
    )
    doc = load_any_manifest(p)
    assert isinstance(doc, InferenceCellManifest)
    assert doc.spec.executor.dtype is None


def test_load_any_manifest_inference_cellset(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "cellset.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: InferenceCellSet
metadata:
  name: demo-set
spec:
  replicas: 0
  template:
    model:
      modelId: llama
      localPath: /models/llama
    parallelism:
      tp: 1
      pp: 1
    members:
      - siteId: site-a
        nodeId: node-a
        gpuCount: 1
        """,
    )
    doc = load_any_manifest(p)
    assert isinstance(doc, InferenceCellSetManifest)
    assert doc.spec.replicas == 0


def test_load_manifest_rejects_non_deployment(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "bad.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: InferenceCell
metadata:
  name: bad
spec:
  model:
    modelId: m
    localPath: /m
  members: []
        """,
    )
    with pytest.raises(ManifestError):
        load_manifest(p)


@pytest.mark.parametrize(
    ("rel_path", "expected_name", "expected_dtype"),
    [
        ("specs/examples/inference/cell-a-single.yaml", "cell-a-single", "half"),
        ("specs/examples/inference/cell-b-single.yaml", "cell-b-single", None),
        ("specs/examples/inference/cell-ab-pp2-ray.yaml", "cell-ab-pp2-ray", "half"),
        ("specs/examples/inference/cell-ab-pp2-mp.yaml", "cell-ab-pp2-mp", "half"),
    ],
)
def test_checked_in_inference_examples_load(
    rel_path: str, expected_name: str, expected_dtype: str | None
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    doc = load_any_manifest(repo_root / rel_path)
    assert isinstance(doc, InferenceCellManifest)
    assert doc.metadata.name == expected_name
    assert doc.spec.executor.runtime_class_name == "nvidia"
    assert doc.spec.executor.dtype == expected_dtype
    assert doc.spec.executor.ray_image == "rayproject/ray:latest"
    assert doc.spec.executor.mp_image == "vllm/vllm-openai:latest"
    assert doc.spec.executor.launcher_image == "vllm/vllm-openai:latest"
