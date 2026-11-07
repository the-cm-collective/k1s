"""Pure installed-profile contracts for non-mutating ``k1s profile`` commands."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Literal, Protocol, cast

from ae.kctl.lifecycle import PROFILE_CONTRACTS
from ae.kctl.profile_engine import (
    QUALIFICATION_POSTURE,
    EngineCapability,
    EngineCapabilityError,
)
from ae.resources import loader as resource_loader

API_VERSION = "k1s.dev/v1alpha1"
SCHEMA_VERSION = 1
PROFILE_RESOURCE_PARTS = ("profiles", "profile-catalog-v1.json")
MAX_PROFILE_RESOURCE_BYTES = 64 * 1024

ProfilePosture = Literal["ga", "development-only", "preview"]
CheckStatus = Literal["pass", "warn", "fail"]
ExecutionStatus = Literal["portable", "deferred"]
QualificationPosture = Literal["source-validated/unqualified", "not-applicable"]


class ProfileResourceError(ValueError):
    """The installed profile catalog is absent, corrupt, or noncanonical."""


@dataclass(frozen=True, slots=True)
class ProfileDescriptor:
    """One canonical, installed profile contract."""

    name: str
    contract_posture: ProfilePosture
    production_eligible: bool
    summary: str
    components: tuple[str, ...]
    requirements: tuple[str, ...]
    execution_status: ExecutionStatus
    qualification_posture: QualificationPosture


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


class ReadOnlyEngineDoctorProbe(Protocol):
    """Explicit, bounded engine probe used only when the caller requests it."""

    def probe(self, backend: str) -> EngineCapability: ...


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


_EXPECTED_PROFILE_NAMES = ("local", "core", "edge-gateway", "node", "edge-dev", "core-ha")
_EXPECTED_ALIAS_NAMES = ("edge", "core-edge", "edge-core")
_PROFILE_KEYS = {
    "aliases",
    "components",
    "contractPosture",
    "executionStatus",
    "name",
    "productionEligible",
    "qualificationPosture",
    "requirements",
    "summary",
}
_ALIAS_KEYS = {"compatibilityVariant", "name", "warning"}


def _without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileResourceError(f"duplicate profile resource key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ProfileResourceError(f"unsupported profile resource number: {value}")


def _mapping(value: object, *, context: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ProfileResourceError(f"{context} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, *, context: str) -> list[object]:
    if type(value) is not list:
        raise ProfileResourceError(f"{context} must be an array")
    return cast(list[object], value)


def _text(value: object, *, context: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProfileResourceError(f"{context} must be nonblank canonical text")
    return value


def _string_tuple(value: object, *, context: str) -> tuple[str, ...]:
    values = tuple(
        _text(item, context=f"{context}[{index}]")
        for index, item in enumerate(_list(value, context=context))
    )
    if len(values) != len(set(values)):
        raise ProfileResourceError(f"{context} contains duplicate values")
    return values


def _exact_keys(value: dict[str, object], expected: set[str], *, context: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ProfileResourceError(f"{context} keys differ: missing={missing}, extra={extra}")


def _validate_catalog_semantics(
    catalog: tuple[ProfileDescriptor, ...], aliases: tuple[ProfileAlias, ...]
) -> None:
    if tuple(profile.name for profile in catalog) != _EXPECTED_PROFILE_NAMES:
        raise ProfileResourceError("canonical profile order or names changed")
    if tuple(alias.name for alias in aliases) != _EXPECTED_ALIAS_NAMES:
        raise ProfileResourceError("profile alias order or names changed")

    profiles_by_name = {profile.name: profile for profile in catalog}
    aliases_by_name = {alias.name: alias for alias in aliases}
    if len(profiles_by_name) != len(catalog):
        raise ProfileResourceError("duplicate canonical profile name")
    if len(aliases_by_name) != len(aliases):
        raise ProfileResourceError("duplicate profile alias")
    overlap = set(profiles_by_name) & set(aliases_by_name)
    if overlap:
        raise ProfileResourceError(f"canonical profile and alias overlap: {sorted(overlap)}")
    unknown_targets = {
        alias.canonical_name for alias in aliases if alias.canonical_name not in profiles_by_name
    }
    if unknown_targets:
        raise ProfileResourceError(
            f"profile aliases target unknown profiles: {sorted(unknown_targets)}"
        )

    expected_posture = {
        "local": ("ga", True),
        "core": ("ga", True),
        "edge-gateway": ("ga", True),
        "node": ("ga", True),
        "edge-dev": ("development-only", False),
        "core-ha": ("preview", False),
    }
    actual_posture = {
        profile.name: (profile.contract_posture, profile.production_eligible) for profile in catalog
    }
    if actual_posture != expected_posture:
        raise ProfileResourceError("profile posture or production eligibility changed")
    if profiles_by_name["edge-gateway"].components != ("gateway",):
        raise ProfileResourceError("edge-gateway must remain gateway-only")
    if profiles_by_name["edge-dev"].components != ("gateway", "worker-stub"):
        raise ProfileResourceError("edge-dev must retain its explicit worker-stub component")
    if aliases_by_name["edge"].canonical_name != "edge-dev":
        raise ProfileResourceError("legacy edge alias must remain development-only")
    if aliases_by_name["edge-core"].canonical_name != "edge-dev":
        raise ProfileResourceError("legacy edge-core alias must remain development-only")


def _parse_profile_catalog_bytes(
    raw: bytes,
) -> tuple[tuple[ProfileDescriptor, ...], tuple[ProfileAlias, ...]]:
    if not raw or len(raw) > MAX_PROFILE_RESOURCE_BYTES:
        raise ProfileResourceError("profile resource size is outside the accepted range")
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw or b"\r" in raw:
        raise ProfileResourceError("profile resource encoding is not canonical UTF-8/LF")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProfileResourceError("profile resource is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProfileResourceError("profile resource is not valid JSON") from exc
    canonical = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if text != canonical:
        raise ProfileResourceError("profile resource bytes are not canonical JSON")

    root = _mapping(value, context="profile resource")
    _exact_keys(
        root,
        {"apiVersion", "kind", "mutating", "profiles", "schemaVersion"},
        context="profile resource",
    )
    if root["apiVersion"] != API_VERSION:
        raise ProfileResourceError("profile resource API version mismatch")
    if root["kind"] != "ProfileCatalog":
        raise ProfileResourceError("profile resource kind mismatch")
    if root["mutating"] is not False:
        raise ProfileResourceError("profile resource must be non-mutating")
    if type(root["schemaVersion"]) is not int or root["schemaVersion"] != SCHEMA_VERSION:
        raise ProfileResourceError("profile resource schema version mismatch")

    profiles: list[ProfileDescriptor] = []
    aliases_by_name: dict[str, ProfileAlias] = {}
    for profile_index, raw_profile in enumerate(
        _list(root["profiles"], context="profile resource profiles")
    ):
        context = f"profile resource profiles[{profile_index}]"
        profile = _mapping(raw_profile, context=context)
        _exact_keys(profile, _PROFILE_KEYS, context=context)
        name = _text(profile["name"], context=f"{context}.name")
        posture = _text(profile["contractPosture"], context=f"{context}.contractPosture")
        if posture not in {"ga", "development-only", "preview"}:
            raise ProfileResourceError(f"{context}.contractPosture is unsupported")
        production_eligible = profile["productionEligible"]
        if type(production_eligible) is not bool:
            raise ProfileResourceError(f"{context}.productionEligible must be a boolean")
        execution_status = _text(profile["executionStatus"], context=f"{context}.executionStatus")
        expected_execution = "portable" if name in {"local", "core"} else "deferred"
        if execution_status != expected_execution:
            raise ProfileResourceError(f"{context}.executionStatus differs")
        qualification_posture = _text(
            profile["qualificationPosture"], context=f"{context}.qualificationPosture"
        )
        expected_qualification = (
            QUALIFICATION_POSTURE if expected_execution == "portable" else "not-applicable"
        )
        if qualification_posture != expected_qualification:
            raise ProfileResourceError(f"{context}.qualificationPosture differs")
        profiles.append(
            ProfileDescriptor(
                name=name,
                contract_posture=cast(ProfilePosture, posture),
                production_eligible=production_eligible,
                summary=_text(profile["summary"], context=f"{context}.summary"),
                components=_string_tuple(profile["components"], context=f"{context}.components"),
                requirements=_string_tuple(
                    profile["requirements"], context=f"{context}.requirements"
                ),
                execution_status=cast(ExecutionStatus, execution_status),
                qualification_posture=cast(QualificationPosture, qualification_posture),
            )
        )
        raw_aliases = _list(profile["aliases"], context=f"{context}.aliases")
        for alias_index, raw_alias in enumerate(raw_aliases):
            alias_context = f"{context}.aliases[{alias_index}]"
            alias = _mapping(raw_alias, context=alias_context)
            _exact_keys(alias, _ALIAS_KEYS, context=alias_context)
            alias_name = _text(alias["name"], context=f"{alias_context}.name")
            if alias_name in aliases_by_name:
                raise ProfileResourceError(f"duplicate profile alias: {alias_name!r}")
            variant = alias["compatibilityVariant"]
            if variant is not None:
                variant = _text(variant, context=f"{alias_context}.compatibilityVariant")
            aliases_by_name[alias_name] = ProfileAlias(
                name=alias_name,
                canonical_name=name,
                compatibility_variant=variant,
                warning=_text(alias["warning"], context=f"{alias_context}.warning"),
            )

    try:
        aliases = tuple(aliases_by_name[name] for name in _EXPECTED_ALIAS_NAMES)
    except KeyError as exc:
        raise ProfileResourceError(f"missing required profile alias: {exc.args[0]}") from exc
    if set(aliases_by_name) != set(_EXPECTED_ALIAS_NAMES):
        raise ProfileResourceError("profile alias names changed")
    catalog = tuple(profiles)
    _validate_catalog_semantics(catalog, aliases)
    return catalog, aliases


def _load_profile_catalog_resource() -> tuple[
    tuple[ProfileDescriptor, ...], tuple[ProfileAlias, ...]
]:
    try:
        raw = resource_loader.load_binary(*PROFILE_RESOURCE_PARTS)
    except OSError as exc:
        raise ProfileResourceError("installed profile resource is unavailable") from exc
    return _parse_profile_catalog_bytes(raw)


PROFILE_CATALOG, PROFILE_ALIASES = _load_profile_catalog_resource()

PROFILE_NAMES: tuple[str, ...] = tuple(profile.name for profile in PROFILE_CATALOG)
PROFILE_INPUT_NAMES: tuple[str, ...] = PROFILE_NAMES + tuple(
    alias.name for alias in PROFILE_ALIASES
)

_PROFILES_BY_NAME = {profile.name: profile for profile in PROFILE_CATALOG}
_ALIASES_BY_NAME = {alias.name: alias for alias in PROFILE_ALIASES}
_EXECUTION_DEFERRED = "Stateful execution is deferred to later lifecycle checkpoints."
_PORTABLE_UNQUALIFIED = (
    "The installed portable source path is validated; live Podman/Docker qualification "
    "requires the separately reviewed isolated runtime lane."
)


def validate_profile_catalog() -> None:
    """Reject internal ambiguity or compatibility drift deterministically."""

    _validate_catalog_semantics(PROFILE_CATALOG, PROFILE_ALIASES)

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
        "executionStatus": profile.execution_status,
        "qualificationPosture": profile.qualification_posture,
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
        "executionStatus": resolved.profile.execution_status,
        "qualificationPosture": resolved.profile.qualification_posture,
    }


def profile_plan_payload(name: str) -> dict[str, object]:
    """Return a declarative plan without resolving or executing a backend."""

    resolved = resolve_profile(name)
    warnings = [
        _PORTABLE_UNQUALIFIED
        if resolved.profile.execution_status == "portable"
        else _EXECUTION_DEFERRED
    ]
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
        "supportedBackends": (
            ["podman", "docker"] if resolved.profile.execution_status == "portable" else []
        ),
        "warnings": warnings,
    }


def _doctor_check(check_id: str, status: CheckStatus, summary: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "summary": summary}


def profile_doctor_payload(
    name: str,
    probe: ReadOnlyDoctorProbe,
    *,
    engine: str | None = None,
    engine_probe: ReadOnlyEngineDoctorProbe | None = None,
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
    if resolved.profile.execution_status == "portable":
        checks.append(_doctor_check("execution.portable", "pass", _PORTABLE_UNQUALIFIED))
        checks.append(_doctor_check("backend.qualification", "warn", _PORTABLE_UNQUALIFIED))
    else:
        checks.append(_doctor_check("execution.deferred", "warn", _EXECUTION_DEFERRED))

    engine_payload: dict[str, object] | None = None
    if engine is not None:
        if resolved.profile.execution_status != "portable":
            checks.append(
                _doctor_check(
                    "backend.capability",
                    "fail",
                    "An engine probe is not applicable to a deferred profile.",
                )
            )
        elif engine_probe is None:
            checks.append(
                _doctor_check(
                    "backend.capability",
                    "fail",
                    "The explicitly requested read-only engine probe is unavailable.",
                )
            )
        else:
            try:
                capability = engine_probe.probe(engine)
            except EngineCapabilityError as exc:
                checks.append(_doctor_check("backend.capability", "fail", str(exc)))
            else:
                checks.append(
                    _doctor_check(
                        "backend.capability",
                        "pass",
                        f"Explicit {capability.backend} capability query passed; live "
                        "qualification remains unclaimed.",
                    )
                )
                engine_payload = {
                    "backend": capability.backend,
                    "executable": str(capability.executable),
                    "qualificationPosture": capability.qualification_posture,
                    "rootless": capability.rootless,
                    "version": capability.version,
                }

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
        "engineCapability": engine_payload,
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
