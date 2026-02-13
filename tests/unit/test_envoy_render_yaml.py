import yaml

from ae.ingress.envoy_core_proxy import (
    CoreProxyCluster,
    CoreProxyRoute,
    EnvoyRenderConfig,
    render_envoy_config,
)


def test_render_envoy_default_route_keeps_wildcard_domain_literal() -> None:
    text = render_envoy_config([], [], EnvoyRenderConfig())
    payload = yaml.safe_load(text)
    vhosts = (
        payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]["route_config"]["virtual_hosts"]
    )
    assert vhosts[0]["domains"] == ["*"]


def test_render_envoy_respects_cluster_lb_policy() -> None:
    text = render_envoy_config(
        [
            CoreProxyRoute(
                host="app.example.test",
                path_prefix="/",
                cluster="site_a",
            )
        ],
        [
            CoreProxyCluster(
                name="site_a",
                endpoints=[("127.0.0.1", 18081)],
                lb_policy="LEAST_REQUEST",
            )
        ],
        EnvoyRenderConfig(),
    )
    payload = yaml.safe_load(text)
    clusters = payload["static_resources"]["clusters"]
    assert clusters[0]["name"] == "site_a"
    assert clusters[0]["lb_policy"] == "LEAST_REQUEST"


def test_render_envoy_sticky_cookie_adds_hash_policy() -> None:
    text = render_envoy_config(
        [
            CoreProxyRoute(
                host="app.example.test",
                path_prefix="/",
                cluster="site_a",
                sticky_cookie_name="k1s_route",
                sticky_cookie_ttl_seconds=3600,
            )
        ],
        [
            CoreProxyCluster(
                name="site_a",
                endpoints=[("127.0.0.1", 18081)],
                lb_policy="RING_HASH",
            )
        ],
        EnvoyRenderConfig(),
    )
    payload = yaml.safe_load(text)
    clusters = payload["static_resources"]["clusters"]
    assert clusters[0]["lb_policy"] == "RING_HASH"

    routes = (
        payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]["route_config"]["virtual_hosts"][0]["routes"]
    )
    hash_policy = routes[0]["route"]["hash_policy"]
    assert hash_policy[0]["cookie"]["name"] == "k1s_route"
    assert hash_policy[0]["cookie"]["ttl"] == "3600s"
