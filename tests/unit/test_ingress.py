"""Tests for the ingress helpers."""

from pathlib import Path

from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata
from ae.ingress.caddy import CaddyIngressManager
from ae.ingress.service import IngressService


def build_manifest() -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            ingress=IngressSpec(host="demo.local", path="/"),
        ),
    )


def test_caddy_manager_writes_site(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):  # noqa: ANN001 - mimic subprocess signature
        calls.append(args)

        class Result:
            stdout = b""
            stderr = b""

        return Result()

    monkeypatch.setattr("ae.ingress.caddy.subprocess.run", fake_run)

    manager = CaddyIngressManager(config_root=tmp_path, caddy_binary="caddy")
    manifest = build_manifest()
    site_path = manager.apply(manifest, upstream="127.0.0.1:32000")

    assert site_path.exists()
    content = site_path.read_text()
    assert "demo.local" in content
    assert "127.0.0.1:32000" in content

    manager.reload()
    assert calls == [["caddy", "reload", "--config", str(tmp_path)]]


def test_ingress_service_apply(tmp_path, monkeypatch):
    monkeypatch.setattr("ae.ingress.caddy.subprocess.run", lambda *_args, **_kwargs: None)
    manager = CaddyIngressManager(config_root=tmp_path / "sites", caddy_binary="caddy")
    service = IngressService(manager)
    manifest = build_manifest()

    result = service.apply(manifest, upstream="127.0.0.1:32000")

    assert result.host == "demo.local"
    assert Path(result.config_path).exists()


def test_caddy_multi_path(tmp_path, monkeypatch):
    monkeypatch.setattr("ae.ingress.caddy.subprocess.run", lambda *_args, **_kwargs: None)
    manager = CaddyIngressManager(config_root=tmp_path / "sites", caddy_binary="caddy")
    m = build_manifest()
    # Inject multi-paths
    ing = m.spec.ingress
    assert ing is not None
    m = m.model_copy(
        update={
            "spec": m.spec.model_copy(
                update={"ingress": ing.model_copy(update={"paths": ["/", "/api"]})}
            )
        }
    )
    site_path = manager.apply(m, upstream="127.0.0.1:32000")
    text = site_path.read_text()
    assert "handle_path /api" in text


def test_caddy_byo_tls(tmp_path, monkeypatch):
    monkeypatch.setattr("ae.ingress.caddy.subprocess.run", lambda *_args, **_kwargs: None)
    manager = CaddyIngressManager(config_root=tmp_path / "sites", caddy_binary="caddy")
    m = build_manifest()
    ing = m.spec.ingress
    assert ing is not None
    m = m.model_copy(
        update={
            "spec": m.spec.model_copy(
                update={
                    "ingress": ing.model_copy(
                        update={
                            "tls_cert_path": "/etc/certs/tls.crt",
                            "tls_key_path": "/etc/certs/tls.key",
                        }
                    )
                }
            )
        }
    )
    site_path = manager.apply(m, upstream="127.0.0.1:32000")
    content = site_path.read_text()
    assert "tls /etc/certs/tls.crt /etc/certs/tls.key" in content
