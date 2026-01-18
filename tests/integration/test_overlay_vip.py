import tempfile
from pathlib import Path

import pytest

from ae.controller.state import ServiceEndpoint, SQLiteStateStore
from ae.network.provider_overlay import OverlayProvider


@pytest.mark.integration
def test_overlay_provider_renders_endpoints():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.db"
        store = SQLiteStateStore(db)
        provider = OverlayProvider(
            store,
            manage_network=False,  # don't touch host
            proxy_image="haproxy:2.9-alpine",
            docker_bin="echo",  # avoid docker dependency for test
        )
        # Seed a service
        ip = provider.ensure_service("echo-mn", {"80": {"target_port": 8080}})
        assert ip
        # Seed endpoints on two nodes/ports
        store.record_service_endpoints(
            "echo-mn",
            [
                ServiceEndpoint(
                    app_name="echo-mn", port=80, ip="10.42.0.2", target_port=8080, ready=True
                ),
                ServiceEndpoint(
                    app_name="echo-mn", port=80, ip="10.42.1.3", target_port=8080, ready=True
                ),
            ],
        )
        provider.update_service_endpoints(
            "echo-mn", {80: [("10.42.0.2", 8080), ("10.42.1.3", 8080)]}
        )
        # Rendered config should include both endpoints
        cfg = provider._render_haproxy(
            "echo-mn", ip, {80: [("10.42.0.2", 8080), ("10.42.1.3", 8080)]}
        )
        assert "10.42.0.2:8080" in cfg
        assert "10.42.1.3:8080" in cfg


# ruff: noqa: E501
