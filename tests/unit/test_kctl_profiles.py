"""Pure contract tests for installed, non-mutating k1s profiles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ae.kctl.profile_engine import EngineCapability
from ae.kctl.profiles import (
    MAX_PROFILE_RESOURCE_BYTES,
    PROFILE_INPUT_NAMES,
    PROFILE_NAMES,
    PROFILE_RESOURCE_PARTS,
    InstalledProfileFacts,
    ProfileResourceError,
    _load_profile_catalog_resource,
    _parse_profile_catalog_bytes,
    legacy_migration_target,
    profile_catalog_payload,
    profile_doctor_payload,
    profile_plan_payload,
    resolve_profile,
    validate_profile_catalog,
)
from ae.resources import loader as resource_loader


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
    distribution_version: str | None = "1.0.0rc2",
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
    assert [profile["executionStatus"] for profile in profiles] == [
        "portable",
        "portable",
        "portable",
        "portable",
        "deferred",
        "deferred",
    ]
    assert [profile["qualificationPosture"] for profile in profiles] == [
        "source-validated/unqualified",
        "source-validated/unqualified",
        "source-validated/unqualified",
        "source-validated/unqualified",
        "not-applicable",
        "not-applicable",
    ]
    assert [profile["productionEligible"] for profile in profiles] == [
        True,
        True,
        True,
        True,
        False,
        False,
    ]
    assert PROFILE_NAMES == ("local", "core", "edge-gateway", "node", "edge-dev", "core-ha")


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def test_profile_catalog_is_derived_from_canonical_package_resource() -> None:
    raw = resource_loader.load_binary(*PROFILE_RESOURCE_PARTS)
    parsed = json.loads(raw)

    assert len(raw) <= MAX_PROFILE_RESOURCE_BYTES
    assert raw == _canonical_bytes(parsed)
    assert parsed == profile_catalog_payload()
    catalog, aliases = _parse_profile_catalog_bytes(raw)
    assert tuple(profile.name for profile in catalog) == PROFILE_NAMES
    assert tuple(alias.name for alias in aliases) == ("edge", "core-edge", "edge-core")


def test_profile_resource_contains_no_operational_defaults() -> None:
    payload = json.loads(resource_loader.load_binary(*PROFILE_RESOURCE_PARTS))
    rendered = json.dumps(payload, sort_keys=True)

    for forbidden in (
        "command",
        "credential",
        "environment",
        "hostPath",
        "pidFile",
        "port",
        "service",
        "statePath",
        "supervisor",
    ):
        assert f'"{forbidden}"' not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"\xef\xbb\xbf{}\n",
        b'{"apiVersion":"a","apiVersion":"b"}\n',
        resource_loader.load_binary(*PROFILE_RESOURCE_PARTS).replace(b"\n", b"\r\n"),
    ],
)
def test_profile_resource_rejects_corrupt_or_noncanonical_bytes(payload: bytes) -> None:
    with pytest.raises(ProfileResourceError):
        _parse_profile_catalog_bytes(payload)


def test_profile_resource_rejects_unknown_keys_and_semantic_drift() -> None:
    original = json.loads(resource_loader.load_binary(*PROFILE_RESOURCE_PARTS))
    unknown = {**original, "unexpected": True}
    with pytest.raises(ProfileResourceError, match="keys differ"):
        _parse_profile_catalog_bytes(_canonical_bytes(unknown))

    core_ha = next(profile for profile in original["profiles"] if profile["name"] == "core-ha")
    core_ha["productionEligible"] = True
    with pytest.raises(ProfileResourceError, match="posture or production eligibility"):
        _parse_profile_catalog_bytes(_canonical_bytes(original))


def test_missing_profile_resource_fails_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_parts: str) -> bytes:
        raise FileNotFoundError("missing profile resource")

    monkeypatch.setattr(resource_loader, "load_binary", missing)
    with pytest.raises(ProfileResourceError, match="unavailable"):
        _load_profile_catalog_resource()


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


@pytest.mark.parametrize(
    ("legacy", "canonical", "variant", "components"),
    [
        ("edge", "edge-dev", None, ("gateway", "worker-stub")),
        ("edge-core", "edge-dev", "core-peering", ("gateway", "worker-stub")),
        ("core-edge", "core", "nats-core", ("controller", "nats-core")),
    ],
)
def test_legacy_migration_targets_preserve_development_and_composition_contracts(
    legacy: str,
    canonical: str,
    variant: str | None,
    components: tuple[str, ...],
) -> None:
    target = legacy_migration_target(legacy)

    assert target.legacy_name == legacy
    assert target.canonical_name == canonical
    assert target.compatibility_variant == variant
    assert target.components == components


@pytest.mark.parametrize(
    "unsupported",
    ["local", "core", "edge-dev", "edge-gateway", "node", "core-ha"],
)
def test_legacy_migration_never_promotes_canonical_or_preview_roles(unsupported: str) -> None:
    with pytest.raises(ValueError, match="unsupported legacy migration profile"):
        legacy_migration_target(unsupported)


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


def test_node_catalog_adds_strict_cri_without_changing_other_role_backends() -> None:
    node = profile_plan_payload("node")
    gateway = profile_plan_payload("edge-gateway")
    local = profile_plan_payload("local")
    core = profile_plan_payload("core")

    assert node["supportedBackends"] == ["podman", "docker", "strict-cri"]
    assert "strict-cri-host-receipt" in node["requirements"]
    assert gateway["supportedBackends"] == []
    assert local["supportedBackends"] == ["podman", "docker"]
    assert core["supportedBackends"] == ["podman", "docker"]


def test_plan_is_declarative_and_contains_no_executable_or_state_paths() -> None:
    for name in PROFILE_INPUT_NAMES:
        plan = profile_plan_payload(name)
        assert plan["mutating"] is False
        expected = (
            "portable"
            if plan["canonicalName"] in {"local", "core", "edge-gateway", "node"}
            else "deferred"
        )
        assert plan["executionStatus"] == expected
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
    assert checks["execution.portable"]["status"] == "pass"
    assert checks["backend.qualification"]["status"] == "warn"
    assert probe.calls == 1


def test_doctor_engine_probe_is_explicit_bounded_and_remains_unqualified() -> None:
    class EngineProbe:
        calls: list[str] = []

        def probe(self, backend: str) -> EngineCapability:
            self.calls.append(backend)
            return EngineCapability(
                backend="podman",
                executable=Path("/usr/bin/podman"),
                executable_device=1,
                executable_inode=2,
                version="5.0.0",
                rootless=True,
            )

    engine_probe = EngineProbe()
    catalog_only, catalog_exit = profile_doctor_payload("local", StaticProbe(healthy_facts()))
    probed, probed_exit = profile_doctor_payload(
        "local",
        StaticProbe(healthy_facts()),
        engine="podman",
        engine_probe=engine_probe,
    )

    assert catalog_exit == probed_exit == 0
    assert catalog_only["engineCapability"] is None
    assert engine_probe.calls == ["podman"]
    assert probed["engineCapability"]["qualificationPosture"] == ("source-validated/unqualified")
    checks = {check["id"]: check for check in probed["checks"]}
    assert checks["backend.capability"]["status"] == "pass"
    assert checks["backend.qualification"]["status"] == "warn"


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
