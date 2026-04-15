from __future__ import annotations

import subprocess

from tests.integration import _profile_smoke
from tests.integration._profile_smoke import resolve_http_smoke_image


def test_resolve_http_smoke_image_defaults_to_upstream() -> None:
    assert resolve_http_smoke_image() == "docker.io/library/python:3.11-alpine"


def test_resolve_http_smoke_image_uses_managed_registry_for_strict_cri(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AE_CRI_REGISTRY", raising=False)
    monkeypatch.delenv("AE_REGISTRY_HOST", raising=False)
    monkeypatch.delenv("AE_CRI_REGISTRY_NAMESPACE", raising=False)
    monkeypatch.delenv("AE_CRI_REGISTRY_MODE", raising=False)
    monkeypatch.delenv("AE_CRI_MANAGED_REGISTRY_PORT", raising=False)

    assert resolve_http_smoke_image(strict_cri=True) == "localhost:5001/library/python:3.11-alpine"


def test_resolve_http_smoke_image_honors_registry_namespace(monkeypatch) -> None:
    monkeypatch.setenv("AE_CRI_REGISTRY", "127.0.0.1:32000")
    monkeypatch.setenv("AE_CRI_REGISTRY_NAMESPACE", "k1s")
    monkeypatch.delenv("AE_CRI_REGISTRY_MODE", raising=False)

    assert (
        resolve_http_smoke_image(strict_cri=True)
        == "127.0.0.1:32000/k1s/library/python:3.11-alpine"
    )


def test_resolve_http_smoke_image_respects_registry_mode_off(monkeypatch) -> None:
    monkeypatch.setenv("AE_CRI_REGISTRY_MODE", "off")
    monkeypatch.setenv("AE_CRI_REGISTRY", "127.0.0.1:32000")

    assert resolve_http_smoke_image(strict_cri=True) == "docker.io/library/python:3.11-alpine"


def test_apply_manifest_uses_attached_token_arg_for_hyphen_prefixed_tokens(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(_profile_smoke.subprocess, "run", fake_run)

    manifest = tmp_path / "smoke.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")

    _profile_smoke.apply_manifest(
        manifest,
        server_base="http://127.0.0.1:9108",
        bearer_token="-leading-hyphen-token",
        env={"PATH": "ignored"},
    )

    argv = captured["argv"]
    assert "--token=-leading-hyphen-token" in argv
    assert "--token" not in argv
