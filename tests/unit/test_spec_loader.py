"""Unit tests for manifest loading."""

from pathlib import Path

import pytest

from ae.controller.spec import AppManifest, ManifestError, load_manifest


def write_yaml(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_load_manifest_success(tmp_path: Path) -> None:
    manifest_path = write_yaml(
        tmp_path / "app.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: example
spec:
  image: alpine:3.20
  replicas: 2
  ports:
    - name: http
      containerPort: 8080
  health:
    readiness:
      httpGet:
        path: /
        port: 8080
      initialDelaySeconds: 5
        """.strip(),
    )

    manifest = load_manifest(manifest_path)

    assert isinstance(manifest, AppManifest)
    assert manifest.metadata.name == "example"
    assert manifest.spec.replicas == 2
    assert manifest.spec.health and manifest.spec.health.readiness
    assert manifest.spec.health.readiness.timeout_seconds == 1


def test_load_manifest_runtime_class_name(tmp_path: Path) -> None:
    manifest_path = write_yaml(
        tmp_path / "runtimeclass.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: gpu-app
spec:
  image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda12.5.0
  runtimeClassName: nvidia
        """.strip(),
    )
    manifest = load_manifest(manifest_path)
    assert manifest.spec.runtime_class_name == "nvidia"


def test_load_manifest_validation_error(tmp_path: Path) -> None:
    manifest_path = write_yaml(
        tmp_path / "bad.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata: {}
spec: {}
        """.strip(),
    )

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_load_manifest_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    with pytest.raises(ManifestError):
        load_manifest(path)
