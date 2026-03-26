from __future__ import annotations

from pathlib import Path


def test_dashboard_template_contains_ha_dashboard_containers() -> None:
    html = Path("src/ae/resources/observability/dashboard.html").read_text(encoding="utf-8")

    assert 'id="ha-section"' in html
    assert 'id="ha-issues-banner"' in html
    assert 'id="ha-summary"' in html
    assert 'id="ha-disabled-note"' in html
    assert 'id="ha-grid"' in html
    assert 'id="ha-authority"' in html
    assert 'id="ha-etcd"' in html
    assert 'id="ha-transport"' in html
    assert 'id="ha-edge-sites"' in html
    assert (
        "HA disabled for this profile. Use make k1s-ha-core to view authority, etcd, transport, and edge site health."
        in html
    )
    assert "summaryEl.classList.add('hidden');" in html
    assert "noteEl.classList.remove('hidden');" in html
    assert "gridEl.classList.add('hidden');" in html
    assert "summaryEl.classList.remove('hidden');" in html
    assert "gridEl.classList.remove('hidden');" in html
    assert 'id="overlays"' in html
    assert "HA members" in html
    assert "authority-member-pip" in html
    assert "authorityMembersFromSnapshot" in html
    assert "authorityMemberFreshness" in html
    assert "authorityMemberHeartbeatAgeText" in html
    assert "graphHoverOwner" in html
    assert "authorityMemberPipOffsets" in html
    assert "formatGraphNodeHover" in html
    assert "showGraphNodeHoverCard" in html
    assert "graphNodeHoverOwner" in html
    assert "graphAuthorityMemberHoverOwner" in html
    assert "freshness-stale" in html
    assert "freshness-unknown" in html
    assert "last_heartbeat_at" in html
    assert "last_heartbeat_age_s" in html
    assert "heartbeat age: <code>" in html
    assert "heartbeat age=" in html
    assert "freshness: <code>" in html
    assert "title:'DNS'" in html
    assert "title: 'Host'" in html
    assert "max-width:min(340px, calc(100% - 24px))" in html
    assert "overflow-wrap:anywhere" in html
    assert "function hoverCardPosition(evt, cardWidth, cardHeight)" in html
    assert "graphHover.offsetWidth" in html
    assert "graphHover.style.visibility = 'hidden'" in html
    assert "Reconciliation loop for registered apps from controller desired state." in html
    assert "HA uses shared etcd-backed controller state; local specs import is disabled." in html
    assert "Local specs import feeds the registry in single-node and dev flows." in html
    assert "dashboard_interactive_tools" in html
    assert "setDashboardInteractiveToolsEnabled" in html
    assert "loadDashboardFeatures()" in html
    assert "imported from specs/" not in html
    assert "var edgeInsetStart = 0.48;" in html
    assert "var edgeInsetEnd = 0.98;" in html
    assert "String(8.5 * localScale)" in html
    assert "createElementNS('http://www.w3.org/2000/svg','title')" not in html
