"""Pure contract tests for installed, non-mutating k1s profiles."""

from __future__ import annotations

import pytest

from ae.kctl.profiles import (
    PROFILE_INPUT_NAMES,
    PROFILE_NAMES,
    InstalledProfileFacts,
    profile_catalog_payload,
    profile_doctor_payload,
    profile_plan_payload,
    resolve_profile,
    validate_profile_catalog,
)


class StaticProbe:
    def __init__(self, facts: InstalledProfileFacts) -> None:
        self.facts = facts
        self.calls = 0

    def inspect(self) -> InstalledProfileFacts:
        self.calls += 1
        return self.facts


def healthy_facts(
    *,
    python_version: tuple[int, int] = (3, 13),
    platform: str = "linux",
    ae_importable: bool = True,
    distribution_version: str | None = "0.1.8a1",
    k1s_console_present: bool = True,
) -> InstalledProfileFacts:
    return InstalledProfileFacts(
        python_version=python_version,
        platform=platform,
        ae_importable=ae_importable,
        distribution_version=distribution_version,
        k1s_console_present=k1s_console_present,
    )


def test_profile_catalog_is_ordered_and_has_frozen_postures() -> None:
    validate_profile_catalog()
    payload = profile_catalog_payload()

    assert payload["apiVersion"] == "k1s.dev/v1alpha1"
    assert payload["schemaVersion"] == 1
    assert payload["kind"] == "ProfileCatalog"
    assert payload["mutating"] is False
    profiles = payload["profiles"]
    assert isinstance(profiles, list)
    assert [profile["name"] for profile in profiles] == [
        "local",
        "core",
        "edge-gateway",
        "node",
        "edge-dev",
        "core-ha",
    ]
    assert [profile["contractPosture"] for profile in profiles] == [
        "ga",
        "ga",
        "ga",
        "ga",
        "development-only",
        "preview",
    ]
    assert all(profile["executionStatus"] == "deferred" for profile in profiles)
    assert [profile["productionEligible"] for profile in profiles] == [
        True,
        True,
        True,
        True,
        False,
        False,
    ]
    assert PROFILE_NAMES == ("local", "core", "edge-gateway", "node", "edge-dev", "core-ha")


@pytest.mark.parametrize(
    ("requested", "canonical", "variant"),
    [
        ("core", "core", None),
        ("edge", "edge-dev", None),
        ("core-edge", "core", "nats-core"),
        ("edge-core", "edge-dev", "core-peering"),
    ],
)
def test_profile_aliases_resolve_exactly(
    requested: str,
    canonical: str,
    variant: str | None,
) -> None:
    resolved = resolve_profile(requested)

    assert resolved.profile.name == canonical
    assert (resolved.alias.compatibility_variant if resolved.alias else None) == variant


def test_production_edge_gateway_never_uses_the_development_stub() -> None:
    gateway = profile_plan_payload("edge-gateway")
    development = profile_plan_payload("edge-dev")

    assert gateway["canonicalName"] == "edge-gateway"
    assert gateway["productionEligible"] is True
    assert gateway["components"] == ["gateway"]
    assert "worker-stub" not in gateway["components"]
    assert development["components"] == ["gateway", "worker-stub"]
    assert development["productionEligible"] is False

    for alias in ("edge", "edge-core"):
        plan = profile_plan_payload(alias)
        assert plan["canonicalName"] == "edge-dev"
        assert plan["productionEligible"] is False
        assert "worker-stub" in plan["components"]
        warnings = plan["warnings"]
        assert isinstance(warnings, list)
        assert any(
            "never resolves to edge-gateway" in warning or "not edge-gateway" in warning
            for warning in warnings
        )


def test_plan_is_declarative_and_contains_no_executable_or_state_paths() -> None:
    for name in PROFILE_INPUT_NAMES:
        plan = profile_plan_payload(name)
        assert plan["mutating"] is False
        assert plan["executionStatus"] == "deferred"
        assert "command" not in plan
        assert "statePath" not in plan
        assert "pid" not in plan
        assert "environment" not in plan


@pytest.mark.parametrize("minor", [11, 12, 13])
def test_doctor_accepts_blocking_supported_python_lanes(minor: int) -> None:
    probe = StaticProbe(healthy_facts(python_version=(3, minor)))

    payload, exit_code = profile_doctor_payload("core", probe)

    assert exit_code == 0
    assert payload["overallStatus"] == "warn"
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["python.supported"]["status"] == "pass"
    assert checks["execution.deferred"]["status"] == "warn"
    assert probe.calls == 1


def test_doctor_marks_exploratory_python_and_non_ga_profiles_as_warnings() -> None:
    probe = StaticProbe(healthy_facts(python_version=(3, 14)))

    payload, exit_code = profile_doctor_payload("core-ha", probe)

    assert exit_code == 0
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["python.supported"]["status"] == "warn"
    assert checks["profile.posture"]["status"] == "warn"


def test_core_ha_preview_is_not_production_eligible() -> None:
    plan = profile_plan_payload("core-ha")

    assert plan["contractPosture"] == "preview"
    assert plan["productionEligible"] is False
    warnings = plan["warnings"]
    assert isinstance(warnings, list)
    assert any("not production-eligible" in warning for warning in warnings)
    assert any("release-qualification checkpoint" in warning for warning in warnings)


@pytest.mark.parametrize(
    "facts",
    [
        healthy_facts(python_version=(3, 10)),
        healthy_facts(platform="darwin"),
        healthy_facts(ae_importable=False),
        healthy_facts(distribution_version=None),
        healthy_facts(k1s_console_present=False),
    ],
)
def test_doctor_fails_closed_for_missing_installed_contracts(
    facts: InstalledProfileFacts,
) -> None:
    payload, exit_code = profile_doctor_payload("local", StaticProbe(facts))

    assert exit_code == 1
    assert payload["overallStatus"] == "fail"
    assert any(check["status"] == "fail" for check in payload["checks"])


def test_unknown_profile_is_rejected_without_probe_execution() -> None:
    probe = StaticProbe(healthy_facts())

    with pytest.raises(ValueError, match="unsupported profile"):
        profile_doctor_payload("production-edge", probe)

    assert probe.calls == 0


def test_catalog_aliases_are_unambiguous() -> None:
    assert len(PROFILE_INPUT_NAMES) == len(set(PROFILE_INPUT_NAMES))
    assert set(PROFILE_INPUT_NAMES) == {
        "local",
        "core",
        "edge-gateway",
        "node",
        "edge-dev",
        "core-ha",
        "edge",
        "core-edge",
        "edge-core",
    }
