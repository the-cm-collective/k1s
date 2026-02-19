from __future__ import annotations

from pathlib import Path


def test_hub_controller_publishes_route_bundle_subject_in_dev_conf() -> None:
    text = Path("ops/dev/nats-hub.conf").read_text(encoding="utf-8")
    assert "user: \"hub-controller\"" in text
    assert "k1s.v1.site.*.routes.bundle" in text


def test_nsc_bootstrap_grants_route_bundle_publish_for_hub_controller() -> None:
    text = Path("ops/dev/nsc-bootstrap.sh").read_text(encoding="utf-8")
    assert 'add user --name hub-controller' in text
    assert '"$PUB_FLAG" "k1s.v1.site.*.routes.bundle"' in text
