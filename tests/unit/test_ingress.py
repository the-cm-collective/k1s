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
    site_path, changed = manager.apply(manifest, upstream="127.0.0.1:32000")

    assert changed is True
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
    site_path, changed = manager.apply(m, upstream="127.0.0.1:32000")
    assert changed is True
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
    site_path, changed = manager.apply(m, upstream="127.0.0.1:32000")
    assert changed is True
    content = site_path.read_text()
    assert "tls /etc/certs/tls.crt /etc/certs/tls.key" in content


def test_caddy_manager_quarantines_stale_generated_duplicate_host(tmp_path, monkeypatch):
    monkeypatch.setattr("ae.ingress.caddy.subprocess.run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ae.ingress.caddy.time.strftime", lambda *_args, **_kwargs: "20260708T220000Z")
    manager = CaddyIngressManager(config_root=tmp_path / "sites", caddy_binary="caddy")
    manager._config_root.mkdir(parents=True, exist_ok=True)  # noqa: SLF001
    stale = manager._config_root / "old-namespace--demo.caddy"  # noqa: SLF001
    stale.write_text(
        """https://demo.local {
    log {
        output stdout
        format console
    }
    # Ensure upstream HSTS does not stick during dev
    header -Strict-Transport-Security
    tls internal
    reverse_proxy 10.0.0.10:8080
}
""",
        encoding="utf-8",
    )
    manifest = build_manifest()

    site_path, changed = manager.apply(manifest, upstream="127.0.0.1:32000")

    assert changed is True
    assert site_path == manager._config_root / "demo.caddy"  # noqa: SLF001
    assert site_path.is_file()
    assert not stale.exists()
    quarantine = manager._config_root / ".ae-caddy-quarantine" / "20260708T220000Z-old-namespace--demo.caddy"  # noqa: SLF001
    assert quarantine.is_file()
    assert "10.0.0.10:8080" in quarantine.read_text(encoding="utf-8")
    assert "127.0.0.1:32000" in site_path.read_text(encoding="utf-8")


def test_caddy_manager_leaves_manual_duplicate_host_for_operator_review(tmp_path, monkeypatch):
    monkeypatch.setattr("ae.ingress.caddy.subprocess.run", lambda *_args, **_kwargs: None)
    manager = CaddyIngressManager(config_root=tmp_path / "sites", caddy_binary="caddy")
    manager._config_root.mkdir(parents=True, exist_ok=True)  # noqa: SLF001
    manual = manager._config_root / "manual.caddy"  # noqa: SLF001
    manual.write_text(
        """https://demo.local {
    reverse_proxy 10.0.0.10:8080
}
""",
        encoding="utf-8",
    )

    site_path, changed = manager.apply(build_manifest(), upstream="127.0.0.1:32000")

    assert changed is True
    assert site_path.is_file()
    assert manual.is_file()
    assert not (manager._config_root / ".ae-caddy-quarantine").exists()  # noqa: SLF001
