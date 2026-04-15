from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "helm_shim_demo.sh"


def test_helm_shim_demo_values_include_starter_defaults() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "httpRoute:" in text
    assert "enabled: false" in text
    assert "serviceAccount:" in text
    assert "automount: true" in text
    assert "imagePullSecrets: []" in text
    assert "podAnnotations: {}" in text
    assert "podLabels: {}" in text
    assert "livenessProbe:" in text
    assert "readinessProbe:" in text
    assert "volumes: []" in text
    assert "volumeMounts: []" in text
    assert "falling back to helm template apply" in text
    assert "cannot be imported into the current release" in text
    assert "run_template_only()" in text
