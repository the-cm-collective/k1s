"""Pure installed-profile contracts for non-mutating ``k1s profile`` commands."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Literal, Protocol, cast

from ae.kctl.lifecycle import PROFILE_CONTRACTS

API_VERSION = "k1s.dev/v1alpha1"
SCHEMA_VERSION = 1

ProfilePosture = Literal["ga", "development-only", "preview"]
CheckStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class ProfileDescriptor:
    """One canonical, installed profile contract."""

    name: str
    contract_posture: ProfilePosture
    production_eligible: bool
    summary: str
    components: tuple[str, ...]
    requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProfileAlias:
    """Compatibility-only spelling that resolves to a canonical profile."""

    name: str
    canonical_name: str
    compatibility_variant: str | None
    warning: str


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """A canonical profile plus optional compatibility-alias context."""

    requested_name: str
    profile: ProfileDescriptor
    alias: ProfileAlias | None = None


@dataclass(frozen=True, slots=True)
class InstalledProfileFacts:
    """Read-only installation facts consumed by the pure doctor evaluator."""

    python_version: tuple[int, int]
    platform: str
    ae_importable: bool
    distribution_version: str | None
    k1s_console_present: bool


class ReadOnlyDoctorProbe(Protocol):
    """Static inspection boundary; implementations must never mutate the host."""

    def inspect(self) -> InstalledProfileFacts: ...


class SystemDoctorProbe:
    """Inspect only interpreter and installed-package metadata."""

    def inspect(self) -> InstalledProfileFacts:
        distribution_version: str | None = None
        k1s_console_present = False
        try:
            distribution = metadata.distribution("k1s-core")
        except metadata.PackageNotFoundError:
            pass
        else:
            distribution_version = distribution.version
            k1s_console_present = any(
                entry_point.group == "console_scripts"
                and entry_point.name == "k1s"
                and entry_point.value == "ae.kctl.__main__:main"
                for entry_point in distribution.entry_points
            )
        return InstalledProfileFacts(
            python_version=(sys.version_info.major, sys.version_info.minor),
            platform=sys.platform,
            ae_importable=importlib.util.find_spec("ae") is not None,
            distribution_version=distribution_version,
            k1s_console_present=k1s_console_present,
        )


PROFILE_CATALOG: tuple[ProfileDescriptor, ...] = (
    ProfileDescriptor(
        name="local",
        contract_posture="ga",
        production_eligible=True,
        summary="Local controller and local-node contract.",
        components=("controller", "node-local"),
        requirements=("runtime-backend",),
    ),
    ProfileDescriptor(
        name="core",
        contract_posture="ga",
        production_eligible=True,
        summary="Single core-controller contract.",
        components=("controller",),
        requirements=("runtime-backend",),
    ),
    ProfileDescriptor(
        name="edge-gateway",
        contract_posture="ga",
        production_eligible=True,
        summary="Production gateway-only edge contract.",
        components=("gateway",),
        requirements=("explicit-node-identity", "controller-endpoint"),
    ),
    ProfileDescriptor(
        name="node",
        contract_posture="ga",
        production_eligible=True,
        summary="Explicit node-agent contract.",
        components=("node-agent",),
        requirements=("explicit-node-identity", "controller-endpoint", "runtime-backend"),
    ),
    ProfileDescriptor(
        name="edge-dev",
        contract_posture="development-only",
        production_eligible=False,
        summary="Development edge gateway with an in-process worker stub.",
        components=("gateway", "worker-stub"),
        requirements=(),
    ),
    ProfileDescriptor(
        name="core-ha",
        contract_posture="preview",
        production_eligible=False,
        summary="Preview high-availability core intent.",
        components=("controller-replica-set", "ha-control-plane"),
        requirements=("external-state-store", "ha-transport"),
    ),
)

PROFILE_ALIASES: tuple[ProfileAlias, ...] = (
    ProfileAlias(
        name="edge",
        canonical_name="edge-dev",
        compatibility_variant=None,
        warning="Legacy 'edge' selects the development worker-stub profile, not edge-gateway.",
    ),
    ProfileAlias(
        name="core-edge",
        canonical_name="core",
        compatibility_variant="nats-core",
        warning=(
            "Legacy 'core-edge' records the nats-core transport variant; no transport is started."
        ),
    ),
    ProfileAlias(
        name="edge-core",
        canonical_name="edge-dev",
        compatibility_variant="core-peering",
        warning="Legacy 'edge-core' remains development-only and never resolves to edge-gateway.",
    ),
)

PROFILE_NAMES: tuple[str, ...] = tuple(profile.name for profile in PROFILE_CATALOG)
PROFILE_INPUT_NAMES: tuple[str, ...] = PROFILE_NAMES + tuple(
    alias.name for alias in PROFILE_ALIASES
)

_PROFILES_BY_NAME = {profile.name: profile for profile in PROFILE_CATALOG}
_ALIASES_BY_NAME = {alias.name: alias for alias in PROFILE_ALIASES}
_EXECUTION_DEFERRED = "Stateful execution is deferred to later lifecycle checkpoints."


def validate_profile_catalog() -> None:
    """Reject internal ambiguity or compatibility drift deterministically."""

    if len(_PROFILES_BY_NAME) != len(PROFILE_CATALOG):
        raise ValueError("duplicate canonical profile name")
    if len(_ALIASES_BY_NAME) != len(PROFILE_ALIASES):
        raise ValueError("duplicate profile alias")
    overlap = set(_PROFILES_BY_NAME) & set(_ALIASES_BY_NAME)
    if overlap:
        raise ValueError(f"canonical profile and alias overlap: {sorted(overlap)}")
    unknown_targets = {
        alias.canonical_name
        for alias in PROFILE_ALIASES
        if alias.canonical_name not in _PROFILES_BY_NAME
    }
    if unknown_targets:
        raise ValueError(f"profile aliases target unknown profiles: {sorted(unknown_targets)}")
    if _PROFILES_BY_NAME["edge-gateway"].components != ("gateway",):
        raise ValueError("edge-gateway must remain gateway-only")
    if "worker-stub" not in _PROFILES_BY_NAME["edge-dev"].components:
        raise ValueError("edge-dev must retain its explicit worker-stub component")
    if _ALIASES_BY_NAME["edge"].canonical_name != "edge-dev":
        raise ValueError("legacy edge alias must remain development-only")
    if _ALIASES_BY_NAME["edge-core"].canonical_name != "edge-dev":
        raise ValueError("legacy edge-core alias must remain development-only")

    legacy_names = set(PROFILE_CONTRACTS)
    expected_legacy_names = {"core", "edge", "core-edge", "edge-core"}
    if legacy_names != expected_legacy_names:
        raise ValueError(
            "legacy lifecycle profile set changed without a catalog compatibility decision: "
            f"{sorted(legacy_names)}"
        )


def resolve_profile(name: str) -> ResolvedProfile:
    """Resolve a canonical role or an exact compatibility alias."""

    validate_profile_catalog()
    profile = _PROFILES_BY_NAME.get(name)
    if profile is not None:
        return ResolvedProfile(requested_name=name, profile=profile)
    alias = _ALIASES_BY_NAME.get(name)
    if alias is None:
        expected = ", ".join(PROFILE_INPUT_NAMES)
        raise ValueError(f"unsupported profile {name!r}; expected one of: {expected}")
    return ResolvedProfile(
        requested_name=name,
        profile=_PROFILES_BY_NAME[alias.canonical_name],
        alias=alias,
    )


def _alias_payload(alias: ProfileAlias) -> dict[str, object]:
    return {
        "name": alias.name,
        "compatibilityVariant": alias.compatibility_variant,
        "warning": alias.warning,
    }


def _profile_payload(profile: ProfileDescriptor) -> dict[str, object]:
    aliases = [
        _alias_payload(alias) for alias in PROFILE_ALIASES if alias.canonical_name == profile.name
    ]
    return {
        "name": profile.name,
        "contractPosture": profile.contract_posture,
        "productionEligible": profile.production_eligible,
        "executionStatus": "deferred",
        "summary": profile.summary,
        "components": list(profile.components),
        "requirements": list(profile.requirements),
        "aliases": aliases,
    }


def profile_catalog_payload() -> dict[str, object]:
    """Return the deterministic public catalog schema."""

    validate_profile_catalog()
    return {
        "apiVersion": API_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "kind": "ProfileCatalog",
        "mutating": False,
        "profiles": [_profile_payload(profile) for profile in PROFILE_CATALOG],
    }


def _resolution_payload(resolved: ResolvedProfile) -> dict[str, object]:
    alias = resolved.alias
    return {
        "requestedName": resolved.requested_name,
        "canonicalName": resolved.profile.name,
        "alias": alias is not None,
        "compatibilityVariant": alias.compatibility_variant if alias is not None else None,
        "contractPosture": resolved.profile.contract_posture,
        "productionEligible": resolved.profile.production_eligible,
        "executionStatus": "deferred",
    }


def profile_plan_payload(name: str) -> dict[str, object]:
    """Return a declarative plan without resolving or executing a backend."""

    resolved = resolve_profile(name)
    warnings = [_EXECUTION_DEFERRED]
    if resolved.alias is not None:
        warnings.insert(0, resolved.alias.warning)
    if resolved.profile.contract_posture == "development-only":
        warnings.append("This profile is development-only and is not production-eligible.")
    elif resolved.profile.contract_posture == "preview":
        warnings.append(
            "This profile is preview and is not production-eligible until a later "
            "release-qualification checkpoint promotes it."
        )
    return {
        "apiVersion": API_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "kind": "ProfilePlan",
        "mutating": False,
        **_resolution_payload(resolved),
        "components": list(resolved.profile.components),
        "requirements": list(resolved.profile.requirements),
        "warnings": warnings,
    }


def _doctor_check(check_id: str, status: CheckStatus, summary: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "summary": summary}


def profile_doctor_payload(
    name: str,
    probe: ReadOnlyDoctorProbe,
) -> tuple[dict[str, object], int]:
    """Evaluate static installation facts and return the report plus exit code."""

    resolved = resolve_profile(name)
    facts = probe.inspect()
    checks: list[dict[str, str]] = [
        _doctor_check(
            "catalog.valid", "pass", "Installed profile catalog is internally consistent."
        )
    ]

    major, minor = facts.python_version
    if major == 3 and 11 <= minor <= 13:
        checks.append(
            _doctor_check(
                "python.supported",
                "pass",
                f"CPython {major}.{minor} is a blocking supported lane.",
            )
        )
    elif major == 3 and minor >= 14:
        checks.append(
            _doctor_check(
                "python.supported",
                "warn",
                f"CPython {major}.{minor} is exploratory and is not a blocking supported lane.",
            )
        )
    else:
        checks.append(
            _doctor_check(
                "python.supported",
                "fail",
                f"Python {major}.{minor} is outside the supported CPython 3.11-3.13 lanes.",
            )
        )

    checks.append(
        _doctor_check(
            "platform.linux",
            "pass" if facts.platform.startswith("linux") else "fail",
            "Linux platform detected."
            if facts.platform.startswith("linux")
            else f"Unsupported platform {facts.platform!r}; k1s-core declares Linux support.",
        )
    )
    checks.append(
        _doctor_check(
            "import.ae",
            "pass" if facts.ae_importable else "fail",
            "Python import ae is available."
            if facts.ae_importable
            else "Python import ae is unavailable.",
        )
    )
    checks.append(
        _doctor_check(
            "distribution.k1s-core",
            "pass" if facts.distribution_version is not None else "fail",
            f"Distribution k1s-core {facts.distribution_version} is installed."
            if facts.distribution_version is not None
            else "Distribution metadata for k1s-core is unavailable.",
        )
    )
    checks.append(
        _doctor_check(
            "console.k1s",
            "pass" if facts.k1s_console_present else "fail",
            "The k1s console entry point targets ae.kctl.__main__:main."
            if facts.k1s_console_present
            else "The expected k1s console entry point is unavailable.",
        )
    )
    checks.append(
        _doctor_check(
            "profile.posture",
            "pass" if resolved.profile.contract_posture == "ga" else "warn",
            f"Profile posture is {resolved.profile.contract_posture}.",
        )
    )
    checks.append(_doctor_check("execution.deferred", "warn", _EXECUTION_DEFERRED))

    statuses = {check["status"] for check in checks}
    overall_status: CheckStatus = (
        "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    )
    payload: dict[str, object] = {
        "apiVersion": API_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "kind": "ProfileDoctorReport",
        "mutating": False,
        **_resolution_payload(resolved),
        "overallStatus": overall_status,
        "checks": checks,
    }
    return payload, 1 if overall_status == "fail" else 0


def render_profile_text(payload: dict[str, object]) -> str:
    """Render a concise deterministic text view of a machine payload."""

    kind = payload["kind"]
    if kind == "ProfileCatalog":
        lines = ["NAME\tPOSTURE\tPRODUCTION\tEXECUTION\tCOMPONENTS"]
        profiles = cast(list[dict[str, object]], payload["profiles"])
        for profile in profiles:
            components = cast(list[str], profile["components"])
            lines.append(
                "\t".join(
                    (
                        str(profile["name"]),
                        str(profile["contractPosture"]),
                        "yes" if profile["productionEligible"] else "no",
                        str(profile["executionStatus"]),
                        ",".join(components),
                    )
                )
            )
        return "\n".join(lines)

    lines = [
        f"profile={payload['canonicalName']}",
        f"requested={payload['requestedName']}",
        f"posture={payload['contractPosture']}",
        f"productionEligible={str(payload['productionEligible']).lower()}",
        "mutating=false",
        "execution=deferred",
    ]
    if kind == "ProfilePlan":
        components = cast(list[str], payload["components"])
        warnings = cast(list[str], payload["warnings"])
        lines.append(f"components={','.join(components)}")
        lines.extend(f"warning={warning}" for warning in warnings)
    else:
        lines.append(f"status={payload['overallStatus']}")
        checks = cast(list[dict[str, str]], payload["checks"])
        lines.extend(
            f"check={check['id']} status={check['status']} summary={check['summary']}"
            for check in checks
        )
    return "\n".join(lines)
