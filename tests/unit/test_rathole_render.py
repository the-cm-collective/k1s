from ae.ingress.rathole import (
    RatholeClientConfig,
    RatholeServerConfig,
    render_rathole_client,
    render_rathole_server,
)


def test_render_rathole_server_includes_services_table_when_empty() -> None:
    text = render_rathole_server(
        RatholeServerConfig(
            bind_addr="0.0.0.0:2333",
            default_token="dev",  # noqa: S106 - synthetic test value
            services=[],
        )
    )
    assert "[server.services]" in text


def test_render_rathole_client_includes_services_table_when_empty() -> None:
    text = render_rathole_client(
        RatholeClientConfig(
            remote_addr="127.0.0.1:2333",
            default_token="dev",  # noqa: S106 - synthetic test value
            services=[],
        )
    )
    assert "[client.services]" in text
