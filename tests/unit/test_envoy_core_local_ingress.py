from pathlib import Path

import pytest
import yaml

from ae.controller.health import HealthReport, PodHealth
from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata, ServiceSpec, app_key
from ae.controller.state import PodStatus, ServiceEndpoint, SQLiteStateStore
from ae.ingress import _atomic as atomic_writes
from ae.ingress.edge_core_proxy import (
    EdgeCoreProxyConfig,
    EdgeCoreProxyRenderer,
    _ensure_fallback_tls,
    build_core_proxy_config,
    render_core_proxy_bootstrap_from_env,
)
from ae.ingress.envoy_core_proxy import (
    CoreProxyCluster,
    CoreProxyRoute,
    EnvoyRenderConfig,
    write_envoy_config,
)
from ae.ingress.rathole import (
    RatholeServerConfig,
    RatholeServerService,
    write_rathole_server,
)
from ae.runtime import PodState, RuntimeResult


def _find_vhost(vhosts: list[dict], domain: str) -> dict:
    return next(vhost for vhost in vhosts if domain in vhost["domains"])


def _find_route(vhost: dict, prefix: str) -> dict:
    return next(route for route in vhost["routes"] if route["match"]["prefix"] == prefix)


def test_envoy_core_local_ingress_renders_tls(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)

    app_name = app_key("demo", "default")
    store.upsert_service_endpoints(
        app_name,
        [
            ServiceEndpoint(
                app_name=app_name,
                port=8080,
                ip="127.0.0.1",
                target_port=8080,
                ready=True,
            )
        ],
    )

    tls_root = tmp_path / "tls"
    tls_root.mkdir(parents=True, exist_ok=True)
    cert_path = tls_root / "demo-cert.crt"
    key_path = tls_root / "demo-cert.key"
    cert_path.write_text("dummy cert", encoding="utf-8")
    key_path.write_text("dummy key", encoding="utf-8")

    store.upsert_edge_ingress_route(
        name="demo-ingress",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "host": "demo.local",
            "paths": [
                {
                    "path": "/",
                    "serviceRef": {"name": "demo", "namespace": "default", "port": 8080},
                }
            ],
            "exposure": {
                "mode": "core-local",
                "tls": {
                    "mode": "terminate-core",
                    "terminateCore": {
                        "secretName": "demo-cert",
                        "redirectHttpToHttps": True,
                    },
                },
            },
        },
    )

    config_dir = tmp_path / "edge-ingress"
    envoy_path = config_dir / "envoy.yaml"
    rathole_path = config_dir / "rathole-server.toml"
    cfg = EdgeCoreProxyConfig(
        config_dir=config_dir,
        envoy_config_path=envoy_path,
        rathole_server_path=rathole_path,
        rathole_client_dir=None,
        site_domain_suffix="edge.local",
        http_listen_port=10080,
        tls_listen_port=10443,
        tls_root=tls_root,
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )

    renderer = EdgeCoreProxyRenderer(store, cfg)
    renderer.render()

    text = envoy_path.read_text(encoding="utf-8")
    assert "demo.local" in text
    assert "core_default_demo_8080" in text
    assert "edge_listener_tls" in text
    assert "codec_type: AUTO" in text
    assert "alpn_protocols" in text
    assert "- h2" in text
    assert "- http/1.1" in text
    assert str(cert_path) in text


def test_envoy_core_local_ingress_uses_service_port_hint_with_target_port(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")

    app_name = app_key("k1s-dev-anchor", "default")
    store.upsert_service(
        app_name,
        "10.241.0.20",
        {
            "ports": [
                {
                    "name": "http",
                    "port": 18086,
                    "targetPort": 5678,
                    "protocol": "TCP",
                }
            ]
        },
    )
    store.upsert_service_endpoints(
        app_name,
        [
            ServiceEndpoint(
                app_name=app_name,
                port=18086,
                ip="10.210.0.12",
                target_port=5678,
                ready=True,
            )
        ],
    )

    store.upsert_edge_ingress_route(
        name="k1s-dev-anchor-ingress",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "k1s-dev-anchor-ingress", "namespace": "default"},
            "spec": {
                "host": "demo.apps.k1s-dev-a.core.home.arpa",
                "paths": [
                    {
                        "path": "/",
                        "serviceRef": {
                            "name": "k1s-dev-anchor",
                            "namespace": "default",
                            "port": 18086,
                        },
                    }
                ],
                "exposure": {"mode": "core-local"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    hcm_http = payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
        "typed_config"
    ]
    vhost = _find_vhost(
        hcm_http["route_config"]["virtual_hosts"], "demo.apps.k1s-dev-a.core.home.arpa"
    )
    route = _find_route(vhost, "/")
    assert route["route"]["cluster"] == "core_default_k1s-dev-anchor_18086"

    cluster = next(
        item
        for item in payload["static_resources"]["clusters"]
        if item["name"] == "core_default_k1s-dev-anchor_18086"
    )
    endpoints = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"]
    assert endpoints[0]["endpoint"]["address"]["socket_address"] == {
        "address": "10.210.0.12",
        "port_value": 5678,
    }


def test_envoy_core_local_ingress_falls_back_to_pod_target_port_for_service_port_hint(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")

    app_name = app_key("k1s-dev-anchor", "default")
    store.upsert_service(
        app_name,
        "10.241.0.20",
        {
            "ports": [
                {
                    "name": "http",
                    "port": 18087,
                    "targetPort": 5678,
                    "protocol": "TCP",
                }
            ]
        },
    )
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="k1s-dev-anchor", namespace="default"),
        spec=AppSpec(
            image="hashicorp/http-echo:1.0",
            replicas=1,
            ingress=IngressSpec(host="demo.apps.k1s-dev-a.core.home.arpa", path="/"),
            service=ServiceSpec(port=18087, targetPort=5678),
        ),
    )
    store.record_snapshot(
        manifest,
        RuntimeResult(
            revision=1,
            created=1,
            updated=0,
            removed=0,
            pod_states=[
                PodState(
                    pod_name="k1s-dev-anchor-rev1-0",
                    ready=True,
                    endpoint="10.210.0.12:5678",
                )
            ],
        ),
        HealthReport(
            ready_replicas=1,
            live_replicas=1,
            pods=[
                PodHealth(
                    pod_name="k1s-dev-anchor-rev1-0",
                    ready=True,
                    live=True,
                    readiness_message="ok",
                    liveness_message="ok",
                )
            ],
        ),
        revision=1,
        revision_status="ready",
    )
    store.upsert_edge_ingress_route(
        name="k1s-dev-anchor-ingress",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "k1s-dev-anchor-ingress", "namespace": "default"},
            "spec": {
                "host": "demo.apps.k1s-dev-a.core.home.arpa",
                "paths": [
                    {
                        "path": "/",
                        "serviceRef": {
                            "name": "k1s-dev-anchor",
                            "namespace": "default",
                            "port": 18087,
                        },
                    }
                ],
                "exposure": {"mode": "core-local"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    hcm_http = payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
        "typed_config"
    ]
    vhost = _find_vhost(
        hcm_http["route_config"]["virtual_hosts"], "demo.apps.k1s-dev-a.core.home.arpa"
    )
    route = _find_route(vhost, "/")
    assert route["route"]["cluster"] == "core_default_k1s-dev-anchor_18087"

    cluster = next(
        item
        for item in payload["static_resources"]["clusters"]
        if item["name"] == "core_default_k1s-dev-anchor_18087"
    )
    endpoints = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"]
    assert endpoints[0]["endpoint"]["address"]["socket_address"] == {
        "address": "10.210.0.12",
        "port_value": 5678,
    }


def test_envoy_core_local_ingress_falls_back_to_pod_service_port_for_cri_host_port(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")

    app_name = app_key("k1s-dev-anchor", "default")
    store.upsert_service(
        app_name,
        "10.241.0.20",
        {
            "ports": [
                {
                    "name": "http",
                    "port": 18087,
                    "targetPort": 5678,
                    "protocol": "TCP",
                }
            ]
        },
    )
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="k1s-dev-anchor", namespace="default"),
        spec=AppSpec(
            image="hashicorp/http-echo:1.0",
            replicas=1,
            ingress=IngressSpec(host="demo.apps.k1s-dev-a.core.home.arpa", path="/"),
            service=ServiceSpec(port=18087, targetPort=5678),
        ),
    )
    store.record_snapshot(
        manifest,
        RuntimeResult(
            revision=1,
            created=1,
            updated=0,
            removed=0,
            pod_states=[
                PodState(
                    pod_name="k1s-dev-anchor-rev1-0",
                    ready=True,
                    endpoint="192.168.29.15:18087",
                )
            ],
        ),
        HealthReport(
            ready_replicas=1,
            live_replicas=1,
            pods=[
                PodHealth(
                    pod_name="k1s-dev-anchor-rev1-0",
                    ready=True,
                    live=True,
                    readiness_message="ok",
                    liveness_message="ok",
                )
            ],
        ),
        revision=1,
        revision_status="ready",
    )
    store.upsert_edge_ingress_route(
        name="k1s-dev-anchor-ingress",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "k1s-dev-anchor-ingress", "namespace": "default"},
            "spec": {
                "host": "demo.apps.k1s-dev-a.core.home.arpa",
                "paths": [
                    {
                        "path": "/",
                        "serviceRef": {
                            "name": "k1s-dev-anchor",
                            "namespace": "default",
                            "port": 18087,
                        },
                    }
                ],
                "exposure": {"mode": "core-local"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    hcm_http = payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
        "typed_config"
    ]
    vhost = _find_vhost(
        hcm_http["route_config"]["virtual_hosts"], "demo.apps.k1s-dev-a.core.home.arpa"
    )
    route = _find_route(vhost, "/")
    assert route["route"]["cluster"] == "core_default_k1s-dev-anchor_18087"

    cluster = next(
        item
        for item in payload["static_resources"]["clusters"]
        if item["name"] == "core_default_k1s-dev-anchor_18087"
    )
    endpoints = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"]
    assert endpoints[0]["endpoint"]["address"]["socket_address"] == {
        "address": "192.168.29.15",
        "port_value": 18087,
    }


def test_build_core_proxy_config_normalizes_relative_tls_root(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "edge-ingress"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AE_EDGE_INGRESS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AE_TLS_DIR", "state/tls")
    monkeypatch.delenv("AE_EDGE_INGRESS_ENVOY_CONFIG", raising=False)
    monkeypatch.delenv("AE_RATHOLE_SERVER_CONFIG", raising=False)

    config = build_core_proxy_config()

    assert config is not None
    assert config.tls_root == (tmp_path / "state" / "tls").resolve()


def test_core_proxy_config_writers_replace_targets_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    real_replace = atomic_writes.os.replace
    replace_calls: list[tuple[Path, Path]] = []

    def fake_replace(src: str, dst: str) -> None:
        src_path = Path(src)
        dst_path = Path(dst)
        assert src_path.parent == dst_path.parent
        assert src_path.name.startswith(f".{dst_path.name}.")
        assert src_path.read_text(encoding="utf-8")
        replace_calls.append((src_path, dst_path))
        real_replace(src, dst)

    monkeypatch.setattr(atomic_writes.os, "replace", fake_replace)

    rathole_path = tmp_path / "edge" / "rathole-server.toml"
    rathole_text = write_rathole_server(
        rathole_path,
        RatholeServerConfig(
            bind_addr="0.0.0.0:2333",
            default_token="dev",  # noqa: S106 - static test fixture token
            services=[RatholeServerService(name="site-a", bind_addr="0.0.0.0:18080")],
        ),
    )
    envoy_path = tmp_path / "edge" / "envoy.yaml"
    envoy_text = write_envoy_config(
        envoy_path,
        [CoreProxyRoute(host="site-a.apps.local", path_prefix="/", cluster="site_a")],
        [CoreProxyCluster(name="site_a", endpoints=[("127.0.0.1", 18080)])],
        EnvoyRenderConfig(domain_suffix="apps.local"),
    )

    assert rathole_path.read_text(encoding="utf-8") == rathole_text
    assert envoy_path.read_text(encoding="utf-8") == envoy_text
    assert rathole_path.stat().st_mode & 0o777 == 0o644
    assert envoy_path.stat().st_mode & 0o777 == 0o644
    assert [dst for _, dst in replace_calls] == [rathole_path, envoy_path]
    assert not list((tmp_path / "edge").glob(".*.tmp"))


def test_core_proxy_render_read_failure_preserves_existing_config(tmp_path: Path) -> None:
    class FailingStore:
        def list_site_ingress_endpoints(self):
            raise RuntimeError("etcd request failed: read timed out")

    config_dir = tmp_path / "edge-ingress"
    config_dir.mkdir(parents=True)
    envoy_path = config_dir / "envoy.yaml"
    rathole_path = config_dir / "rathole-server.toml"
    envoy_path.write_text("existing envoy", encoding="utf-8")
    rathole_path.write_text("existing rathole", encoding="utf-8")
    cfg = EdgeCoreProxyConfig(
        config_dir=config_dir,
        envoy_config_path=envoy_path,
        rathole_server_path=rathole_path,
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )

    with pytest.raises(RuntimeError, match="etcd request failed"):
        EdgeCoreProxyRenderer(FailingStore(), cfg).render()  # type: ignore[arg-type]

    assert envoy_path.read_text(encoding="utf-8") == "existing envoy"
    assert rathole_path.read_text(encoding="utf-8") == "existing rathole"


def test_core_proxy_policy_least_request_sets_cluster_lb_policy(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=18081,
    )
    store.upsert_edge_ingress_policy(
        name="lb-policy",
        namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressPolicy",
            "metadata": {"name": "lb-policy", "namespace": "default"},
            "spec": {"loadBalancing": {"strategy": "least_request"}},
        },
    )
    store.upsert_edge_ingress_route(
        name="app-core-proxy",
        namespace="default",
        site_id="sea-edge-02",
        policy_name="lb-policy",
        policy_namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-core-proxy", "namespace": "default"},
            "spec": {
                "host": "app-core-proxy.home.arpa",
                "paths": [
                    {
                        "path": "/",
                        "serviceRef": {"name": "app", "namespace": "default", "port": 8080},
                    }
                ],
                "exposure": {
                    "mode": "core-proxy",
                    "placement": {"site": "sea-edge-02"},
                },
                "policyRef": {"name": "lb-policy", "namespace": "default"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    clusters = payload["static_resources"]["clusters"]
    least_request = [c for c in clusters if c.get("lb_policy") == "LEAST_REQUEST"]
    assert least_request


def test_core_proxy_policy_stickiness_sets_ring_hash_and_cookie(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=18081,
    )
    store.upsert_edge_ingress_policy(
        name="sticky-policy",
        namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressPolicy",
            "metadata": {"name": "sticky-policy", "namespace": "default"},
            "spec": {
                "stickiness": {
                    "mode": "cookie",
                    "cookie": {"name": "k1s_route", "ttlSeconds": 900},
                }
            },
        },
    )
    store.upsert_edge_ingress_route(
        name="app-sticky",
        namespace="default",
        site_id="sea-edge-02",
        policy_name="sticky-policy",
        policy_namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-sticky", "namespace": "default"},
            "spec": {
                "host": "sticky-core-proxy.home.arpa",
                "paths": [{"path": "/"}],
                "exposure": {
                    "mode": "core-proxy",
                    "placement": {"site": "sea-edge-02"},
                },
                "policyRef": {"name": "sticky-policy", "namespace": "default"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    vhosts = payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
        "typed_config"
    ]["route_config"]["virtual_hosts"]
    sticky_vhost = _find_vhost(vhosts, "sticky-core-proxy.home.arpa")
    assert "sticky-core-proxy.home.arpa:10080" in sticky_vhost["domains"]
    route_action = sticky_vhost["routes"][0]["route"]
    cookie_policy = route_action["hash_policy"][0]["cookie"]
    assert cookie_policy["name"] == "k1s_route"
    assert cookie_policy["ttl"] == "900s"

    cluster_name = route_action["cluster"]
    clusters = payload["static_resources"]["clusters"]
    cluster = next(c for c in clusters if c["name"] == cluster_name)
    assert cluster["lb_policy"] == "RING_HASH"


def test_core_proxy_policy_websocket_enabled_renders_upgrade_settings(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=18081,
    )
    store.upsert_edge_ingress_policy(
        name="ws-policy",
        namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressPolicy",
            "metadata": {"name": "ws-policy", "namespace": "default"},
            "spec": {
                "websockets": {
                    "enabled": True,
                    "idleMs": 120000,
                    "maxConnectionDurationMs": 300000,
                }
            },
        },
    )
    store.upsert_edge_ingress_route(
        name="app-ws",
        namespace="default",
        site_id="sea-edge-02",
        policy_name="ws-policy",
        policy_namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-ws", "namespace": "default"},
            "spec": {
                "host": "ws-core-proxy.home.arpa",
                "paths": [{"path": "/ws"}],
                "exposure": {
                    "mode": "core-proxy",
                    "placement": {"site": "sea-edge-02"},
                },
                "policyRef": {"name": "ws-policy", "namespace": "default"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=10443,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    hcm_http = payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
        "typed_config"
    ]
    assert hcm_http["upgrade_configs"] == [{"upgrade_type": "websocket"}]

    vhosts = hcm_http["route_config"]["virtual_hosts"]
    ws_vhost = _find_vhost(vhosts, "ws-core-proxy.home.arpa")
    assert "ws-core-proxy.home.arpa:10080" in ws_vhost["domains"]
    ws_route = ws_vhost["routes"][0]["route"]
    assert ws_route["idle_timeout"] == "120.000s"
    assert ws_route["max_stream_duration"]["max_stream_duration"] == "300.000s"


def test_core_proxy_policy_websocket_disabled_disables_upgrade_for_route(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=18081,
    )
    store.upsert_edge_ingress_policy(
        name="ws-policy-off",
        namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressPolicy",
            "metadata": {"name": "ws-policy-off", "namespace": "default"},
            "spec": {"websockets": {"enabled": False}},
        },
    )
    store.upsert_edge_ingress_route(
        name="app-ws-off",
        namespace="default",
        site_id="sea-edge-02",
        policy_name="ws-policy-off",
        policy_namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-ws-off", "namespace": "default"},
            "spec": {
                "host": "ws-off-core-proxy.home.arpa",
                "paths": [{"path": "/ws"}],
                "exposure": {
                    "mode": "core-proxy",
                    "placement": {"site": "sea-edge-02"},
                },
                "policyRef": {"name": "ws-policy-off", "namespace": "default"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    hcm_http = payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
        "typed_config"
    ]
    vhosts = hcm_http["route_config"]["virtual_hosts"]
    ws_vhost = _find_vhost(vhosts, "ws-off-core-proxy.home.arpa")
    assert "ws-off-core-proxy.home.arpa:10080" in ws_vhost["domains"]
    ws_route = ws_vhost["routes"][0]["route"]
    assert ws_route["upgrade_configs"] == [{"upgrade_type": "websocket", "enabled": False}]


def test_controlplane_public_routes_reserve_hosts_and_skip_conflicting_routes(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    app_name = app_key("demo", "default")
    store.upsert_service_endpoints(
        app_name,
        [
            ServiceEndpoint(
                app_name=app_name,
                port=8080,
                ip="127.0.0.1",
                target_port=8080,
                ready=True,
            )
        ],
    )
    store.upsert_edge_ingress_route(
        name="reserved-host",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "reserved-host", "namespace": "default"},
            "spec": {
                "host": "dash.home.arpa",
                "paths": [
                    {
                        "path": "/",
                        "serviceRef": {"name": "demo", "namespace": "default", "port": 8080},
                    }
                ],
                "exposure": {"mode": "core-local"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=10443,
        tls_root=tmp_path / "tls",
        tls_default_secret=None,
        tls_fallback=True,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
        controlplane_public_enable=True,
    )
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    clusters = payload["static_resources"]["clusters"]
    cluster_names = {cluster["name"] for cluster in clusters}
    tls_listener = next(
        listener
        for listener in payload["static_resources"]["listeners"]
        if listener["name"] == "edge_listener_tls"
    )
    vhosts = tls_listener["filter_chains"][0]["filters"][0]["typed_config"]["route_config"][
        "virtual_hosts"
    ]

    assert "core_default_demo_8080" not in cluster_names
    assert {"controlplane_proxy", "controlplane_api_controller", "controlplane_api_apishim"} <= (
        cluster_names
    )
    assert (
        _find_vhost(vhosts, "dash.home.arpa")["routes"][0]["route"]["cluster"]
        == "controlplane_proxy"
    )
    assert "dash.home.arpa:10443" in _find_vhost(vhosts, "dash.home.arpa")["domains"]
    assert "docs.home.arpa:10443" in _find_vhost(vhosts, "docs.home.arpa")["domains"]
    assert "api.home.arpa:10443" in _find_vhost(vhosts, "api.home.arpa")["domains"]
    api_vhost = _find_vhost(vhosts, "api.home.arpa")
    assert _find_route(api_vhost, "/dashboard")["direct_response"] == {"status": 404}
    assert _find_route(api_vhost, "/playground")["direct_response"] == {"status": 404}
    assert _find_route(api_vhost, "/swagger")["route"]["cluster"] == "controlplane_api_controller"
    assert _find_route(api_vhost, "/redoc")["route"]["cluster"] == "controlplane_api_controller"
    assert _find_route(api_vhost, "/openapi")["route"]["cluster"] == "controlplane_api_controller"
    assert _find_route(api_vhost, "/openapi.json")["route"]["cluster"] == "controlplane_api_controller"
    assert _find_route(api_vhost, "/swagger.json")["route"]["cluster"] == "controlplane_api_controller"
    assert _find_route(api_vhost, "/system")["route"]["cluster"] == "controlplane_api_controller"
    assert _find_route(api_vhost, "/api/v1")["route"]["cluster"] == "controlplane_api_apishim"
    assert _find_route(api_vhost, "/apis")["route"]["cluster"] == "controlplane_api_apishim"


def test_build_core_proxy_config_reads_controlplane_api_upstreams(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "edge-ingress"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AE_EDGE_INGRESS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AE_CONTROLPLANE_PUBLIC_ENABLE", "1")
    monkeypatch.setenv("AE_CONTROLPLANE_API_CONTROLLER_UPSTREAM", "https://leader.home.arpa:9443")
    monkeypatch.setenv("AE_CONTROLPLANE_API_APISHIM_UPSTREAM", "https://leader.home.arpa:9444")
    monkeypatch.setenv("AE_CONTROLPLANE_API_APISHIM_TLS", "1")

    config = build_core_proxy_config()

    assert config is not None
    assert config.controlplane_api_controller_addr == "leader.home.arpa"
    assert config.controlplane_api_controller_port == 9443
    assert config.controlplane_api_apishim_addr == "leader.home.arpa"
    assert config.controlplane_api_apishim_port == 9444
    assert config.controlplane_api_apishim_use_tls is True
    assert set(config.tls_fallback_sans) == {
        "dash.home.arpa",
        "docs.home.arpa",
        "api.home.arpa",
    }


def test_render_core_proxy_bootstrap_from_env_renders_controlplane_tls(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "edge-ingress"
    tls_root = tmp_path / "tls"
    tls_root.mkdir(parents=True, exist_ok=True)
    (tls_root / "envoy-fallback.crt").write_text("dummy cert", encoding="utf-8")
    (tls_root / "envoy-fallback.key").write_text("dummy key", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AE_EDGE_INGRESS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AE_EDGE_INGRESS_ENVOY_CONFIG", str(config_dir / "envoy.yaml"))
    monkeypatch.setenv("AE_EDGE_INGRESS_HTTP_PORT", "10080")
    monkeypatch.setenv("AE_EDGE_INGRESS_TLS_PORT", "10443")
    monkeypatch.setenv("AE_CONTROLPLANE_PUBLIC_ENABLE", "1")
    monkeypatch.setenv("AE_CONTROLPLANE_PROXY_PORT", "10081")
    monkeypatch.setenv("AE_TLS_DIR", str(tls_root))

    rendered = render_core_proxy_bootstrap_from_env()

    assert rendered is True
    payload = yaml.safe_load((config_dir / "envoy.yaml").read_text(encoding="utf-8"))
    listeners = {
        listener["name"]: listener
        for listener in payload["static_resources"]["listeners"]
    }
    assert {"edge_listener_http", "edge_listener_tls"} <= set(listeners)
    assert listeners["edge_listener_tls"]["address"]["socket_address"]["port_value"] == 10443
    tls_vhosts = listeners["edge_listener_tls"]["filter_chains"][0]["filters"][0]["typed_config"][
        "route_config"
    ]["virtual_hosts"]
    assert {"dash.home.arpa", "dash.home.arpa:10443"} <= set(
        _find_vhost(tls_vhosts, "dash.home.arpa")["domains"]
    )
    assert {"docs.home.arpa", "docs.home.arpa:10443"} <= set(
        _find_vhost(tls_vhosts, "docs.home.arpa")["domains"]
    )
    assert {"api.home.arpa", "api.home.arpa:10443"} <= set(
        _find_vhost(tls_vhosts, "api.home.arpa")["domains"]
    )
    cluster_names = {
        cluster["name"] for cluster in payload["static_resources"]["clusters"]
    }
    assert {
        "controlplane_proxy",
        "controlplane_api_controller",
        "controlplane_api_apishim",
    } <= cluster_names


def test_ensure_fallback_tls_sets_cert_and_key_modes(tmp_path: Path) -> None:
    resolved = _ensure_fallback_tls(tmp_path / "tls", "edge.local", 1, sans=("dash.home.arpa",))

    assert resolved is not None
    crt_path, key_path = resolved
    assert oct(crt_path.stat().st_mode & 0o777) == "0o644"
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"
