from __future__ import annotations

from pathlib import Path


def test_dashboard_template_contains_ha_dashboard_containers() -> None:
    html = Path("src/ae/resources/observability/dashboard.html").read_text(encoding="utf-8")

    assert 'id="ha-section"' in html
    assert 'id="ha-issues-banner"' in html
    assert 'id="ha-summary"' in html
    assert 'id="ha-authority"' in html
    assert 'id="ha-etcd"' in html
    assert 'id="ha-transport"' in html
    assert 'id="ha-edge-sites"' in html
    assert 'id="overlays"' in html
    assert "HA members" in html
    assert "authority-member-pip" in html
    assert "authorityMembersFromSnapshot" in html
    assert "graphHoverOwner" in html
    assert "authorityMemberPipOffsets" in html
    assert "formatGraphNodeHover" in html
    assert "showGraphNodeHoverCard" in html
    assert "graphNodeHoverOwner" in html
    assert "graphAuthorityMemberHoverOwner" in html
    assert "title:'DNS'" in html
    assert "title: 'Host'" in html
    assert "createElementNS('http://www.w3.org/2000/svg','title')" not in html
