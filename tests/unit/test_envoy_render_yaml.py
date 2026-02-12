import yaml

from ae.ingress.envoy_core_proxy import EnvoyRenderConfig, render_envoy_config


def test_render_envoy_default_route_keeps_wildcard_domain_literal() -> None:
    text = render_envoy_config([], [], EnvoyRenderConfig())
    payload = yaml.safe_load(text)
    vhosts = (
        payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]["route_config"]["virtual_hosts"]
    )
    assert vhosts[0]["domains"] == ["*"]
