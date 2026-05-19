from __future__ import annotations

from pathlib import Path


def test_dashboard_template_contains_ha_dashboard_containers() -> None:
    html = Path("src/ae/resources/observability/dashboard.html").read_text(encoding="utf-8")

    assert 'id="ha-section"' in html
    assert 'id="ha-section" class="card hidden"' in html
    assert 'id="ha-issues-banner"' in html
    assert 'id="ha-summary"' in html
    assert 'id="ha-summary" class="row hidden"' in html
    assert 'id="ha-disabled-note"' in html
    assert 'id="ha-grid"' in html
    assert 'id="ha-grid" class="ha-grid hidden"' in html
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
    assert "sectionEl.classList.add('hidden');" in html
    assert "sectionEl.classList.remove('hidden');" in html
    assert "dashboardLayoutMode !== 'site'" in html
    assert "summaryEl.classList.remove('hidden');" in html
    assert "gridEl.classList.remove('hidden');" in html
    assert 'id="legend-ha-members" class="hidden"' in html
    assert 'id="overlays"' in html
    assert "HA members" in html
    assert "haLegendEl.classList.toggle('hidden', !useSiteLayout);" in html
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
    assert ".hidden { display:none !important; }" in html
    assert "function hoverCardPosition(evt, cardWidth, cardHeight)" in html
    assert "graphHover.offsetWidth" in html
    assert "graphHover.style.visibility = 'hidden'" in html
    assert "Reconciliation loop for registered apps from controller desired state." in html
    assert "HA uses shared etcd-backed controller state; local specs import is disabled." in html
    assert "Local specs import feeds the registry in single-node and dev flows." in html
    assert "dashboard_interactive_tools" in html
    assert "setDashboardInteractiveToolsEnabled" in html
    assert "loadDashboardFeatures()" in html
    assert "sys.dashboard" in html
    assert "dashboardLayoutMode" in html
    assert "var useSiteLayout = dashboardLayoutMode === 'site';" in html
    assert "var dashboardToken = __DASHBOARD_TOKEN__;" in html
    assert "function bootstrapDashboardToken()" in html
    assert "return dashboardToken || labsToken || '';" in html
    assert "return labsToken || activeToken() || '';" in html
    assert "function normalizeBase(val)" in html
    assert "isWorkerBeeHost(location.hostname)" in html
    assert "isClusterServiceHost(insecureBase.hostname || '')" in html
    assert "isLoopbackBase(base)) base = publicBase;" in html
    assert html.index("if (!base && publicBase)") < html.index(
        "if (!base) base = localStorage.getItem('ae_apishim_base') || '';"
    )
    assert "if (baseInput && !baseInput.value) baseInput.value = normalizeBase('');" in html
    assert "/static/dash-assets/" not in html
    assert "siteDetails && Object.keys(siteDetails).length" not in html
    assert "imported from specs/" not in html
    assert "var edgeInsetStart = 0.48;" in html
    assert "var edgeInsetEnd = 0.98;" in html
    assert "String(8.5 * localScale)" in html
    assert "createElementNS('http://www.w3.org/2000/svg','title')" not in html
