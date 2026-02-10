import base64
from pathlib import Path

from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata
from ae.controller.state import SQLiteStateStore
from ae.ingress.caddy import CaddyIngressManager
from ae.ingress.service import IngressService
from ae.ingress.tls_sync import TlsSecretResolver


def make_secret_yaml(tmpdir: Path, name: str, crt_text: str, key_text: str) -> Path:
    data = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "kubernetes.io/tls",
        "metadata": {"name": name},
        "data": {
            "tls.crt": base64.b64encode(crt_text.encode()).decode(),
            "tls.key": base64.b64encode(key_text.encode()).decode(),
        },
    }
    p = tmpdir / f"{name}.yaml"
    import yaml

    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return p


def test_resolver_decodes_yaml(tmp_path: Path) -> None:
    name = "mycert"
    make_secret_yaml(tmp_path, name, "CERTDATA", "KEYDATA")
    r = TlsSecretResolver(tmp_path)
    out = r.resolve(name)
    assert out is not None
    crt, key = out
    assert crt.read_text() == "CERTDATA"
    assert key.read_text() == "KEYDATA"


def test_ingress_service_uses_resolved_tls(tmp_path, monkeypatch):
    # Prepare TLS secret YAML
    name = "mycert"
    make_secret_yaml(tmp_path, name, "CERTDATA", "KEYDATA")
    monkeypatch.setenv("AE_TLS_DIR", str(tmp_path))
    # Manager
    monkeypatch.setattr("ae.ingress.caddy.subprocess.run", lambda *_args, **_kwargs: None)
    manager = CaddyIngressManager(config_root=tmp_path / "sites", caddy_binary="caddy")
    svc = IngressService(manager)
    # Manifest with tlsSecretName only
    m = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            ingress=IngressSpec(host="demo.local", path="/", tls=True, tlsSecretName=name),
        ),
    )
    res = svc.apply(m, upstream="127.0.0.1:32000")
    assert Path(res.config_path).exists()
    text = Path(res.config_path).read_text()
    assert "tls " in text and "rendered" in text


def test_ingress_service_emits_event_when_tls_secret_missing(tmp_path, monkeypatch):
    # No secret files present
    monkeypatch.setenv("AE_TLS_DIR", str(tmp_path))
    # Fake reload
    monkeypatch.setattr("ae.ingress.caddy.subprocess.run", lambda *_args, **_kwargs: None)
    manager = CaddyIngressManager(config_root=tmp_path / "sites", caddy_binary="caddy")
    store = SQLiteStateStore(tmp_path / "state.db")
    svc = IngressService(manager, store=store)
    m = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            ingress=IngressSpec(host="demo.local", path="/", tls=True, tlsSecretName="missing"),
        ),
    )
    svc.apply(m, upstream="127.0.0.1:32000")
    events = store.list_events("demo", limit=5)
    assert any(e.event_type == "IngressTLSMissing" for e in events)
