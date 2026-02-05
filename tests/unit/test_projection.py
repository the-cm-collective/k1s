from pathlib import Path
import os

from ae.controller.health import HealthManager
from ae.controller.reconciler import Reconciler
from ae.controller.spec import (
    AppManifest,
    AppSpec,
    ConfigEnvMapping,
    ConfigRef,
    Metadata,
    PortSpec,
    SecretEnvMapping,
    SecretRef,
)
from ae.controller.state import SQLiteStateStore
from ae.ingress.caddy import CaddyIngressManager
from ae.ingress.service import IngressService
from ae.runtime.docker_stub import StubRuntime


def make_manifest(tmp_path: Path) -> AppManifest:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("mode: demo\ncolor: blue\n")
    sec = tmp_path / "sec.json"
    sec.write_text('{"token": "abc123"}')
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="tproj"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            ports=[PortSpec(name="http", containerPort=8080)],
            config_refs=[
                ConfigRef(
                    name="cfg", path=str(cfg), env=[ConfigEnvMapping(name="APP_MODE", key="mode")]
                )
            ],
            secret_refs=[
                SecretRef(
                    name="sec", path=str(sec), env=[SecretEnvMapping(name="API_TOKEN", key="token")]
                )
            ],
        ),
    )


def test_projection_writes_files(tmp_path: Path):
    store = SQLiteStateStore(tmp_path / "state.db")
    runtime = StubRuntime()
    health = HealthManager()
    ingress = IngressService(
        CaddyIngressManager(config_root=tmp_path / "sites", caddy_binary="caddy")
    )
    reconciler = Reconciler(runtime, store, health_manager=health, ingress_service=ingress)

    m = make_manifest(tmp_path)
    report = reconciler.reconcile(m)
    base = Path(os.getenv("AE_PROJECTION_ROOT", "state/projections"))
    proj = base / f"{m.metadata.name}-rev{report.revision}"
    cfg_file = proj / "config" / "mode"
    sec_file = proj / "secret" / "token"
    assert cfg_file.exists()
    assert sec_file.exists()
    assert cfg_file.read_text().strip() == "demo"
    assert sec_file.read_text().strip() == "abc123"
