"""Metadata-safe, pure K1s evidence projection for WorkerBee consumers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Literal, NoReturn, cast
from urllib.parse import urlsplit

from ae import __version__
from ae.controller.node_identity import NodeIdentityError, validate_node_id
from ae.kctl.profile_paths import (
    ProfilePathError,
    validate_bootstrap_id,
    validate_identifier,
    validate_run_id,
)
from ae.kctl.profiles import profile_catalog_payload, profile_plan_payload
from ae.resources import loader as resource_loader

ADAPTER_API_VERSION = "k1s.dev/workerbee-profile-adapter/v1"
EVIDENCE_API_VERSION = "k1s.dev/workerbee-profile-evidence/v1"
NODE_OBSERVATION_API_VERSION = "k1s.dev/node-lease-observation/v1"
QUALIFICATION_POSTURE = "source-validated/unqualified"
SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
MAX_LOG_LINES = 200
MAX_LOG_LINE_BYTES = 8192
MAX_NESTING = 8
MAX_INT64 = (1 << 63) - 1

_RESOURCE_PARTS = ("profiles", "workerbee-adapter-v1.json")
_RESOURCE_KEYS = {
    "apiVersion",
    "evidenceApiVersion",
    "kind",
    "operations",
    "qualificationPosture",
    "schemaVersion",
}
_OPERATION_KEYS = {
    "acceptedExitCodes",
    "adapterMutating",
    "name",
    "nodeObservation",
    "sourceApiVersion",
    "sourceKind",
    "sourceMode",
    "sourceMutating",
    "sourceSchemaVersion",
}
_EVIDENCE_KEYS = {
    "apiVersion",
    "evidenceId",
    "kind",
    "nodeIdentity",
    "operation",
    "payload",
    "provenance",
    "qualificationPosture",
    "schemaVersion",
}
_PROVENANCE_KEYS = {
    "adapterApiVersion",
    "distribution",
    "distributionVersion",
    "sourceApiVersion",
    "sourceExitCode",
    "sourceKind",
    "sourceMode",
    "sourceSchemaVersion",
}
_NODE_IDENTITY_KEYS = {
    "generation",
    "generationProvenance",
    "identityMode",
    "identityVersion",
    "nodeId",
}
_NODE_OBSERVATION_KEYS = {
    "apiVersion",
    "generation",
    "identityMode",
    "identityVersion",
    "kind",
    "nodeId",
    "provenance",
    "schemaVersion",
}
_PORTABLE_PROFILES = ("local", "core", "edge-gateway", "node")
_PROFILE_NAMES = ("local", "core", "edge-gateway", "node", "edge-dev", "core-ha")
_POSTURES = {"ga", "development-only", "preview"}
_EXECUTION_STATUSES = {"portable", "deferred"}
_QUALIFICATION_POSTURES = {QUALIFICATION_POSTURE, "not-applicable"}
_CHECK_STATUSES = {"pass", "warn", "fail"}
_RUN_PHASES = {"preparing", "starting", "running", "stopping", "stopped", "failed", "orphaned"}
_LIFECYCLE_STATUSES = {*_RUN_PHASES, "absent", "deleted"}
_BACKENDS = {"podman", "docker", "gateway", "strict-cri"}
_COMPONENTS = {
    "local": ("controller", "node-local"),
    "core": ("controller",),
    "edge-gateway": ("gateway",),
    "node": ("node-agent",),
}
_DOCTOR_COMMON_CHECKS = (
    "catalog.valid",
    "python.supported",
    "platform.linux",
    "import.ae",
    "distribution.k1s-core",
    "console.k1s",
    "profile.posture",
)
_WARNING_CODES = {
    (
        "Legacy 'core-edge' records the nats-core transport variant; no transport is started."
    ): "compatibility.core-edge.nats-core",
    (
        "Legacy 'edge' selects the development worker-stub profile, not edge-gateway."
    ): "compatibility.edge.edge-dev",
    (
        "Legacy 'edge-core' remains development-only and never resolves to edge-gateway."
    ): "compatibility.edge-core.edge-dev",
    (
        "The installed portable source path is validated; live Podman/Docker qualification "
        "requires the separately reviewed isolated runtime lane."
    ): "execution.portable.source-validated-unqualified",
    "Stateful execution is deferred to later lifecycle checkpoints.": "execution.deferred",
    "This profile is development-only and is not production-eligible.": "posture.development-only",
    (
        "This profile is preview and is not production-eligible until a later "
        "release-qualification checkpoint promotes it."
    ): "posture.preview",
}
_ERROR_CODES = {
    "invalid-input",
    "unsupported-schema",
    "unsupported-operation",
    "source-exit-mismatch",
    "source-shape-mismatch",
    "unsafe-metadata",
    "size-limit",
    "node-observation-mismatch",
    "internal",
}
_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FORBIDDEN_EVIDENCE_KEYS = {
    "credential",
    "credentialDigest",
    "createdAt",
    "engineCapability",
    "environment",
    "executable",
    "fd",
    "host",
    "hostPlanDigest",
    "hostReceiptDigest",
    "lines",
    "logDigest",
    "message",
    "ownerUid",
    "path",
    "pid",
    "privateBootstrapDigest",
    "raw",
    "runId",
    "secret",
    "session",
    "socket",
    "summary",
    "timestamp",
    "token",
    "warning",
    "warnings",
}

OperationName = Literal["list", "doctor", "plan", "status", "logs", "bootstrap"]


class ProfileAdapterError(ValueError):
    """One stable fail-closed adapter error without source-value disclosure."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "internal"
        super().__init__("workerbee profile adapter failed closed")


@dataclass(frozen=True, slots=True)
class OperationDescriptor:
    accepted_exit_codes: tuple[int, ...]
    adapter_mutating: bool
    name: OperationName
    node_observation: Literal["forbidden", "optional"]
    source_api_version: str | None
    source_kind: str
    source_mode: str
    source_mutating: bool
    source_schema_version: int | None


@dataclass(frozen=True, slots=True)
class WorkerBeeAdapterContract:
    operations: tuple[OperationDescriptor, ...]

    def descriptor(self, name: str) -> OperationDescriptor:
        for descriptor in self.operations:
            if descriptor.name == name:
                return descriptor
        raise ProfileAdapterError("unsupported-operation")


def _fail(code: str) -> NoReturn:
    raise ProfileAdapterError(code)


def _reject_constant(_value: str) -> object:
    _fail("invalid-input")


def _without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid-input")
        result[key] = value
    return result


def _mapping(value: object, *, code: str = "source-shape-mismatch") -> dict[str, object]:
    if type(value) is not dict:
        _fail(code)
    return cast(dict[str, object], value)


def _array(value: object, *, code: str = "source-shape-mismatch") -> list[object]:
    if type(value) is not list:
        _fail(code)
    return cast(list[object], value)


def _exact_keys(value: Mapping[str, object], keys: set[str], *, code: str) -> None:
    if set(value) != keys:
        _fail(code)


def _text(value: object, *, maximum: int = 1024, code: str = "source-shape-mismatch") -> str:
    if type(value) is not str:
        _fail(code)
    text = value
    if not text or len(text.encode("utf-8")) > maximum:
        _fail(code)
    if unicodedata.normalize("NFC", text) != text:
        _fail(code)
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in text):
        _fail(code)
    return text


def _optional_text(value: object, *, maximum: int = 1024) -> str | None:
    return None if value is None else _text(value, maximum=maximum)


def _integer(value: object, *, minimum: int, maximum: int = MAX_INT64) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("source-shape-mismatch")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        _fail("source-shape-mismatch")
    return value


def _validate_depth(value: object, depth: int = 0) -> None:
    if depth > MAX_NESTING:
        _fail("size-limit")
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                _fail("invalid-input")
            _text(key, maximum=128, code="invalid-input")
            _validate_depth(item, depth + 1)
    elif type(value) is list:
        for item in cast(list[object], value):
            _validate_depth(item, depth + 1)
    elif value is not None and type(value) not in {str, int, bool}:
        _fail("invalid-input")
    elif type(value) is str:
        _text(value, maximum=MAX_INPUT_BYTES, code="invalid-input")


def _canonical_json(value: object, *, pretty: bool) -> bytes:
    _validate_depth(value)
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    return (text + "\n").encode("utf-8")


def _loads(raw: bytes, *, pretty: bool, maximum: int) -> object:
    if not raw or len(raw) > maximum:
        _fail("size-limit")
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw or b"\r" in raw:
        _fail("invalid-input")
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_without_duplicates,
            parse_constant=_reject_constant,
            parse_float=lambda _value: _fail("invalid-input"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileAdapterError("invalid-input") from exc
    if _canonical_json(value, pretty=pretty) != raw:
        _fail("invalid-input")
    return value


def _loads_source(raw: bytes) -> object:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        _fail("size-limit")
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw or b"\r" in raw:
        _fail("invalid-input")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_without_duplicates,
            parse_constant=_reject_constant,
            parse_float=lambda _value: _fail("invalid-input"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileAdapterError("invalid-input") from exc
    _validate_depth(value)
    return value


def _operation_payload(descriptor: OperationDescriptor) -> dict[str, object]:
    return {
        "acceptedExitCodes": list(descriptor.accepted_exit_codes),
        "adapterMutating": descriptor.adapter_mutating,
        "name": descriptor.name,
        "nodeObservation": descriptor.node_observation,
        "sourceApiVersion": descriptor.source_api_version,
        "sourceKind": descriptor.source_kind,
        "sourceMode": descriptor.source_mode,
        "sourceMutating": descriptor.source_mutating,
        "sourceSchemaVersion": descriptor.source_schema_version,
    }


@cache
def load_workerbee_adapter_contract() -> WorkerBeeAdapterContract:
    """Load the exact installed adapter contract without checkout fallback."""

    try:
        raw = resource_loader.load_binary(*_RESOURCE_PARTS)
    except OSError as exc:
        raise ProfileAdapterError("unsupported-schema") from exc
    root = _mapping(_loads(raw, pretty=True, maximum=64 * 1024), code="unsupported-schema")
    _exact_keys(root, _RESOURCE_KEYS, code="unsupported-schema")
    if (
        root["apiVersion"] != ADAPTER_API_VERSION
        or root["evidenceApiVersion"] != EVIDENCE_API_VERSION
        or root["kind"] != "WorkerBeeProfileAdapterContract"
        or root["qualificationPosture"] != QUALIFICATION_POSTURE
        or type(root["schemaVersion"]) is not int
        or root["schemaVersion"] != SCHEMA_VERSION
    ):
        _fail("unsupported-schema")
    expected = (
        (
            "list",
            (0,),
            "forbidden",
            "k1s.dev/v1alpha1",
            "ProfileCatalog",
            "profile-list-json",
            False,
            1,
        ),
        (
            "doctor",
            (0, 1),
            "forbidden",
            "k1s.dev/v1alpha1",
            "ProfileDoctorReport",
            "profile-doctor-catalog-json",
            False,
            1,
        ),
        (
            "plan",
            (0,),
            "forbidden",
            "k1s.dev/v1alpha1",
            "ProfilePlan",
            "profile-plan-json",
            False,
            1,
        ),
        (
            "status",
            (0,),
            "forbidden",
            None,
            "PortableLifecycleResult",
            "profile-status-portable-json",
            False,
            None,
        ),
        (
            "logs",
            (0,),
            "forbidden",
            "k1s.dev/v1alpha1",
            "ProfileLogSnapshot",
            "profile-logs-owned-json",
            False,
            None,
        ),
        (
            "bootstrap",
            (0,),
            "optional",
            None,
            "ProfileBootstrapPublicProjection",
            "profile-bootstrap-create-json",
            True,
            None,
        ),
    )
    raw_operations = _array(root["operations"], code="unsupported-schema")
    if len(raw_operations) != len(expected):
        _fail("unsupported-schema")
    operations: list[OperationDescriptor] = []
    for raw_operation, identity in zip(raw_operations, expected, strict=True):
        operation = _mapping(raw_operation, code="unsupported-schema")
        _exact_keys(operation, _OPERATION_KEYS, code="unsupported-schema")
        codes = _array(operation["acceptedExitCodes"], code="unsupported-schema")
        if any(type(code) is not int for code in codes) or len(codes) != len(set(codes)):
            _fail("unsupported-schema")
        if type(operation["sourceMutating"]) is not bool or not (
            operation["sourceSchemaVersion"] is None
            or type(operation["sourceSchemaVersion"]) is int
        ):
            _fail("unsupported-schema")
        actual = (
            operation["name"],
            tuple(cast(list[int], codes)),
            operation["nodeObservation"],
            operation["sourceApiVersion"],
            operation["sourceKind"],
            operation["sourceMode"],
            operation["sourceMutating"],
            operation["sourceSchemaVersion"],
        )
        if operation["adapterMutating"] is not False or actual != identity:
            _fail("unsupported-schema")
        operations.append(
            OperationDescriptor(
                accepted_exit_codes=actual[1],
                adapter_mutating=False,
                name=cast(OperationName, actual[0]),
                node_observation=cast(Literal["forbidden", "optional"], actual[2]),
                source_api_version=actual[3],
                source_kind=actual[4],
                source_mode=actual[5],
                source_mutating=actual[6],
                source_schema_version=actual[7],
            )
        )
    contract = WorkerBeeAdapterContract(tuple(operations))
    expected_root = {
        "apiVersion": ADAPTER_API_VERSION,
        "evidenceApiVersion": EVIDENCE_API_VERSION,
        "kind": "WorkerBeeProfileAdapterContract",
        "operations": [_operation_payload(item) for item in contract.operations],
        "qualificationPosture": QUALIFICATION_POSTURE,
        "schemaVersion": SCHEMA_VERSION,
    }
    if _canonical_json(expected_root, pretty=True) != raw:
        _fail("unsupported-schema")
    return contract


def _identifier(value: object, *, field: str) -> str:
    text = _text(value, maximum=253)
    try:
        return validate_identifier(text, field=field)
    except ProfilePathError as exc:
        raise ProfileAdapterError("source-shape-mismatch") from exc


def _node_id(value: object) -> str:
    text = _text(value, maximum=253)
    try:
        return validate_node_id(text)
    except NodeIdentityError as exc:
        raise ProfileAdapterError("source-shape-mismatch") from exc


def _run_id(value: object) -> str:
    text = _text(value, maximum=35)
    try:
        return validate_run_id(text)
    except ProfilePathError as exc:
        raise ProfileAdapterError("source-shape-mismatch") from exc


def _bootstrap_id(value: object) -> str:
    text = _text(value, maximum=35)
    try:
        return validate_bootstrap_id(text)
    except ProfilePathError as exc:
        raise ProfileAdapterError("source-shape-mismatch") from exc


def _source_mapping(source_payload: Mapping[str, object] | bytes | str) -> dict[str, object]:
    if isinstance(source_payload, bytes):
        return _mapping(_loads_source(source_payload))
    if isinstance(source_payload, str):
        return _mapping(_loads_source(source_payload.encode("utf-8")))
    source = _mapping(source_payload)
    if len(_canonical_json(source, pretty=False)) > MAX_INPUT_BYTES:
        _fail("size-limit")
    return source


def _project_list(source: dict[str, object]) -> dict[str, object]:
    _exact_keys(
        source,
        {"apiVersion", "kind", "mutating", "profiles", "schemaVersion"},
        code="source-shape-mismatch",
    )
    if (
        source["apiVersion"] != "k1s.dev/v1alpha1"
        or source["kind"] != "ProfileCatalog"
        or source["mutating"] is not False
        or type(source["schemaVersion"]) is not int
        or source["schemaVersion"] != 1
    ):
        _fail("source-shape-mismatch")
    raw_profiles = _array(source["profiles"])
    for raw_profile in raw_profiles:
        profile = _mapping(raw_profile)
        if type(profile.get("productionEligible")) is not bool:
            _fail("source-shape-mismatch")
    if _canonical_json(source, pretty=False) != _canonical_json(
        profile_catalog_payload(), pretty=False
    ):
        _fail("source-shape-mismatch")
    profiles: list[dict[str, object]] = []
    for raw_profile in cast(list[dict[str, object]], source["profiles"]):
        aliases = [
            {
                "compatibilityVariant": alias["compatibilityVariant"],
                "name": alias["name"],
            }
            for alias in cast(list[dict[str, object]], raw_profile["aliases"])
        ]
        profiles.append(
            {
                "aliases": aliases,
                "components": raw_profile["components"],
                "contractPosture": raw_profile["contractPosture"],
                "executionStatus": raw_profile["executionStatus"],
                "name": raw_profile["name"],
                "productionEligible": raw_profile["productionEligible"],
                "qualificationPosture": raw_profile["qualificationPosture"],
                "requirements": raw_profile["requirements"],
            }
        )
    return {"profiles": profiles}


def _resolution_fields(source: Mapping[str, object]) -> dict[str, object]:
    alias = _boolean(source["alias"])
    requested = _identifier(source["requestedName"], field="requested name")
    canonical = _identifier(source["canonicalName"], field="canonical name")
    if canonical not in _PROFILE_NAMES:
        _fail("source-shape-mismatch")
    variant = _optional_text(source["compatibilityVariant"], maximum=32)
    if variant not in {None, "nats-core", "core-peering"}:
        _fail("source-shape-mismatch")
    posture = _text(source["contractPosture"], maximum=32)
    execution = _text(source["executionStatus"], maximum=32)
    qualification = _text(source["qualificationPosture"], maximum=64)
    if posture not in _POSTURES or execution not in _EXECUTION_STATUSES:
        _fail("source-shape-mismatch")
    if qualification not in _QUALIFICATION_POSTURES:
        _fail("source-shape-mismatch")
    return {
        "alias": alias,
        "canonicalName": canonical,
        "compatibilityVariant": variant,
        "contractPosture": posture,
        "executionStatus": execution,
        "productionEligible": _boolean(source["productionEligible"]),
        "qualificationPosture": qualification,
        "requestedName": requested,
    }


def _project_doctor(source: dict[str, object], source_exit_code: int) -> dict[str, object]:
    keys = {
        "alias",
        "apiVersion",
        "canonicalName",
        "checks",
        "compatibilityVariant",
        "contractPosture",
        "engineCapability",
        "executionStatus",
        "kind",
        "mutating",
        "overallStatus",
        "productionEligible",
        "qualificationPosture",
        "requestedName",
        "schemaVersion",
    }
    _exact_keys(source, keys, code="source-shape-mismatch")
    if (
        source["apiVersion"] != "k1s.dev/v1alpha1"
        or source["kind"] != "ProfileDoctorReport"
        or type(source["schemaVersion"]) is not int
        or source["schemaVersion"] != 1
        or source["mutating"] is not False
        or source["engineCapability"] is not None
    ):
        _fail("source-shape-mismatch")
    resolution = _resolution_fields(source)
    expected_plan = profile_plan_payload(cast(str, resolution["requestedName"]))
    expected_resolution = _resolution_fields(expected_plan)
    if _canonical_json(resolution, pretty=False) != _canonical_json(
        expected_resolution, pretty=False
    ):
        _fail("source-shape-mismatch")
    status = _text(source["overallStatus"], maximum=8)
    if status not in _CHECK_STATUSES:
        _fail("source-shape-mismatch")
    if source_exit_code != (1 if status == "fail" else 0):
        _fail("source-exit-mismatch")
    expected_ids = _DOCTOR_COMMON_CHECKS + (
        ("execution.portable", "backend.qualification")
        if resolution["executionStatus"] == "portable"
        else ("execution.deferred",)
    )
    raw_checks = _array(source["checks"])
    if len(raw_checks) != len(expected_ids):
        _fail("source-shape-mismatch")
    checks: list[dict[str, str]] = []
    statuses: list[str] = []
    for raw_check, expected_id in zip(raw_checks, expected_ids, strict=True):
        check = _mapping(raw_check)
        _exact_keys(check, {"id", "status", "summary"}, code="source-shape-mismatch")
        check_id = _text(check["id"], maximum=64)
        check_status = _text(check["status"], maximum=8)
        _text(check["summary"], maximum=512)
        if check_id != expected_id or check_status not in _CHECK_STATUSES:
            _fail("source-shape-mismatch")
        statuses.append(check_status)
        checks.append({"id": check_id, "status": check_status})
    recomputed = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    if recomputed != status:
        _fail("source-shape-mismatch")
    return {**resolution, "checks": checks, "status": status}


def _project_plan(source: dict[str, object]) -> dict[str, object]:
    keys = {
        "alias",
        "apiVersion",
        "bootstrapRequired",
        "canonicalName",
        "compatibilityVariant",
        "components",
        "contractPosture",
        "executionStatus",
        "kind",
        "mutating",
        "productionEligible",
        "qualificationPosture",
        "requestedName",
        "requirements",
        "schemaVersion",
        "supportedBackends",
        "warnings",
    }
    _exact_keys(source, keys, code="source-shape-mismatch")
    if source.get("requestedName") is None:
        _fail("source-shape-mismatch")
    requested = _identifier(source["requestedName"], field="requested name")
    if _canonical_json(source, pretty=False) != _canonical_json(
        profile_plan_payload(requested), pretty=False
    ):
        _fail("source-shape-mismatch")
    warnings = _array(source["warnings"])
    try:
        warning_codes = [_WARNING_CODES[cast(str, warning)] for warning in warnings]
    except (KeyError, TypeError) as exc:
        raise ProfileAdapterError("source-shape-mismatch") from exc
    if len(warning_codes) != len(set(warning_codes)) or len(warning_codes) > 3:
        _fail("source-shape-mismatch")
    resolution = _resolution_fields(source)
    return {
        **resolution,
        "bootstrapRequired": source["bootstrapRequired"],
        "components": source["components"],
        "requirements": source["requirements"],
        "supportedBackends": source["supportedBackends"],
        "warningCodes": warning_codes,
    }


def _run_ref(profile: str, run_id: str) -> str:
    preimage = _canonical_json({"profile": profile, "runId": run_id}, pretty=False)
    digest = hashlib.sha256(b"k1s-workerbee-run-ref-v1\0" + preimage).hexdigest()
    return f"sha256:{digest}"


def _project_status(source: dict[str, object]) -> dict[str, object]:
    keys = {
        "action",
        "backend",
        "exit_code",
        "generation",
        "phase",
        "profile",
        "qualification_posture",
        "run_id",
        "status",
    }
    _exact_keys(source, keys, code="source-shape-mismatch")
    if (
        source["action"] != "status"
        or type(source["exit_code"]) is not int
        or source["exit_code"] != 0
    ):
        _fail("source-shape-mismatch")
    profile = _text(source["profile"], maximum=32)
    if profile not in _PORTABLE_PROFILES:
        _fail("source-shape-mismatch")
    status = _text(source["status"], maximum=16)
    if status not in _LIFECYCLE_STATUSES:
        _fail("source-shape-mismatch")
    qualification = _text(source["qualification_posture"], maximum=64)
    if qualification != QUALIFICATION_POSTURE:
        _fail("source-shape-mismatch")
    if status == "absent":
        if any(source[key] is not None for key in ("backend", "generation", "phase", "run_id")):
            _fail("source-shape-mismatch")
        backend = phase = generation = run_ref = None
    else:
        backend = _text(source["backend"], maximum=16)
        phase = _text(source["phase"], maximum=16)
        generation = _integer(source["generation"], minimum=1)
        run_id = _run_id(source["run_id"])
        if backend not in _BACKENDS or phase not in _RUN_PHASES:
            _fail("source-shape-mismatch")
        if (status == "deleted" and phase != "stopped") or (
            status != "deleted" and status != phase
        ):
            _fail("source-shape-mismatch")
        run_ref = _run_ref(profile, run_id)
    return {
        "backend": backend,
        "lifecycleStatus": status,
        "phase": phase,
        "profile": profile,
        "profileGeneration": generation,
        "qualificationPosture": qualification,
        "runRef": run_ref,
    }


def _project_logs(source: dict[str, object]) -> dict[str, object]:
    keys = {"apiVersion", "component", "kind", "lines", "profile", "runId", "truncated"}
    _exact_keys(source, keys, code="source-shape-mismatch")
    if source["apiVersion"] != "k1s.dev/v1alpha1" or source["kind"] != "ProfileLogSnapshot":
        _fail("source-shape-mismatch")
    profile = _text(source["profile"], maximum=32)
    component = _identifier(source["component"], field="component")
    if profile not in _PORTABLE_PROFILES or component not in _COMPONENTS[profile]:
        _fail("source-shape-mismatch")
    run_id = _run_id(source["runId"])
    lines = _array(source["lines"])
    if len(lines) > MAX_LOG_LINES:
        _fail("size-limit")
    total = 0
    for line in lines:
        text = _text(line, maximum=MAX_LOG_LINE_BYTES)
        total += len(text.encode("utf-8"))
    if total > MAX_INPUT_BYTES:
        _fail("size-limit")
    return {
        "component": component,
        "contentPolicy": "omitted",
        "lineCount": len(lines),
        "profile": profile,
        "runRef": _run_ref(profile, run_id),
        "sourceTruncated": _boolean(source["truncated"]),
    }


def _absent_connection() -> dict[str, object]:
    return {
        "addressClass": None,
        "portPresent": False,
        "presence": "absent",
        "schemeClass": None,
        "securityClass": None,
    }


def _hostname_class(host: str, raw_authority: str, port: int | None) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if _DNS_NAME.fullmatch(host) is None or re.fullmatch(r"[0-9.]+", host):
            _fail("unsafe-metadata")
        canonical_host = host
        address_class = "hostname"
    else:
        canonical_host = str(address)
        address_class = "ipv4" if address.version == 4 else "ipv6"
        if host != canonical_host:
            _fail("unsafe-metadata")
    rendered_host = f"[{canonical_host}]" if address_class == "ipv6" else canonical_host
    expected = rendered_host + (f":{port}" if port is not None else "")
    if raw_authority != expected:
        _fail("unsafe-metadata")
    return address_class


def _connection_fact(value: object, *, allowed_schemes: set[str]) -> dict[str, object]:
    if value is None:
        return _absent_connection()
    endpoint = _text(value, maximum=2048, code="unsafe-metadata")
    if not endpoint.isascii() or "%" in endpoint or "@" in endpoint:
        _fail("unsafe-metadata")
    raw_scheme, separator, _remainder = endpoint.partition("://")
    if separator != "://" or raw_scheme != raw_scheme.lower():
        _fail("unsafe-metadata")
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProfileAdapterError("unsafe-metadata") from exc
    host = parsed.hostname
    if (
        parsed.scheme not in allowed_schemes
        or parsed.scheme != raw_scheme
        or parsed.scheme != parsed.scheme.lower()
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        _fail("unsafe-metadata")
    if port is not None and not 1 <= port <= 65535:
        _fail("unsafe-metadata")
    if host != host.lower():
        _fail("unsafe-metadata")
    address_class = _hostname_class(host, parsed.netloc, port)
    security = "tls" if parsed.scheme in {"https", "tls"} else "cleartext"
    return {
        "addressClass": address_class,
        "portPresent": port is not None,
        "presence": "present",
        "schemeClass": parsed.scheme,
        "securityClass": security,
    }


def _listen_fact(host_value: object, port_value: object) -> dict[str, object]:
    if host_value is None and port_value is None:
        return _absent_connection()
    host = _text(host_value, maximum=64, code="unsafe-metadata")
    if type(port_value) is not int or not 1 <= port_value <= 65535:
        _fail("unsafe-metadata")
    if "%" in host:
        _fail("unsafe-metadata")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ProfileAdapterError("unsafe-metadata") from exc
    if str(address) != host:
        _fail("unsafe-metadata")
    return {
        "addressClass": "ipv4" if address.version == 4 else "ipv6",
        "portPresent": True,
        "presence": "present",
        "schemeClass": None,
        "securityClass": None,
    }


def _validate_node_observation(value: object) -> dict[str, object]:
    observation = _mapping(value, code="node-observation-mismatch")
    _exact_keys(observation, _NODE_OBSERVATION_KEYS, code="node-observation-mismatch")
    if (
        observation["apiVersion"] != NODE_OBSERVATION_API_VERSION
        or observation["kind"] != "NodeLeaseObservation"
        or observation["identityMode"] != "explicit"
        or type(observation["identityVersion"]) is not int
        or observation["identityVersion"] != 1
        or observation["provenance"] != "k1s-controller-lease"
        or type(observation["schemaVersion"]) is not int
        or observation["schemaVersion"] != 1
    ):
        _fail("node-observation-mismatch")
    try:
        node_id = validate_node_id(cast(str, observation["nodeId"]))
    except (NodeIdentityError, TypeError) as exc:
        raise ProfileAdapterError("node-observation-mismatch") from exc
    generation = observation["generation"]
    if type(generation) is not int or not 1 <= generation <= MAX_INT64:
        _fail("node-observation-mismatch")
    return {"generation": generation, "nodeId": node_id}


def _project_bootstrap(
    source: dict[str, object], node_observation: Mapping[str, object] | None
) -> tuple[dict[str, object], dict[str, object]]:
    keys = {
        "advertiseEndpoint",
        "backendId",
        "bootstrapId",
        "controllerUrl",
        "identityVersion",
        "listenHost",
        "listenPort",
        "nodeId",
        "profile",
        "siteId",
        "transportUrl",
    }
    _exact_keys(source, keys, code="source-shape-mismatch")
    profile = _text(source["profile"], maximum=32)
    if profile not in {"edge-gateway", "node"}:
        _fail("source-shape-mismatch")
    if type(source["identityVersion"]) is not int or source["identityVersion"] != 1:
        _fail("source-shape-mismatch")
    node_id = _node_id(source["nodeId"])
    bootstrap_id = _bootstrap_id(source["bootstrapId"])
    backend = _text(source["backendId"], maximum=16)
    site_id = _optional_text(source["siteId"], maximum=253)
    if site_id is not None:
        _node_id(site_id)
    controller = _connection_fact(source["controllerUrl"], allowed_schemes={"http", "https"})
    transport = _connection_fact(source["transportUrl"], allowed_schemes={"nats", "tls"})
    advertise = _connection_fact(source["advertiseEndpoint"], allowed_schemes={"http", "https"})
    listen = _listen_fact(source["listenHost"], source["listenPort"])
    if profile == "edge-gateway":
        if (
            backend != "gateway"
            or site_id is None
            or not node_id.startswith(f"{site_id}--")
            or transport["presence"] != "present"
            or any(item["presence"] != "absent" for item in (controller, listen, advertise))
        ):
            _fail("source-shape-mismatch")
    elif (
        backend not in {"podman", "docker", "strict-cri"}
        or site_id is not None
        or transport["presence"] != "absent"
        or any(item["presence"] != "present" for item in (controller, listen, advertise))
    ):
        _fail("source-shape-mismatch")
    if node_observation is None:
        node_identity: dict[str, object] = {
            "generation": None,
            "generationProvenance": "unobserved",
            "identityMode": "explicit",
            "identityVersion": 1,
            "nodeId": node_id,
        }
    else:
        observed = _validate_node_observation(node_observation)
        if observed["nodeId"] != node_id:
            _fail("node-observation-mismatch")
        node_identity = {
            "generation": observed["generation"],
            "generationProvenance": "k1s-controller-lease",
            "identityMode": "explicit",
            "identityVersion": 1,
            "nodeId": node_id,
        }
    payload: dict[str, object] = {
        "backendId": backend,
        "bootstrapId": bootstrap_id,
        "connections": {
            "advertise": advertise,
            "controller": controller,
            "listen": listen,
            "transport": transport,
        },
        "profile": profile,
        "siteId": site_id,
    }
    return payload, node_identity


def _evidence_id(evidence_without_id: Mapping[str, object]) -> str:
    preimage = _canonical_json(dict(evidence_without_id), pretty=False)
    digest = hashlib.sha256(b"k1s-workerbee-profile-evidence-v1\0" + preimage).hexdigest()
    return f"sha256:{digest}"


def _provenance(descriptor: OperationDescriptor, source_exit_code: int) -> dict[str, object]:
    return {
        "adapterApiVersion": ADAPTER_API_VERSION,
        "distribution": "k1s-core",
        "distributionVersion": __version__,
        "sourceApiVersion": descriptor.source_api_version,
        "sourceExitCode": source_exit_code,
        "sourceKind": descriptor.source_kind,
        "sourceMode": descriptor.source_mode,
        "sourceSchemaVersion": descriptor.source_schema_version,
    }


def adapt_workerbee_profile_evidence(
    operation: str,
    source_payload: Mapping[str, object] | bytes | str,
    *,
    source_exit_code: int,
    node_observation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Project one exact K1s JSON result into metadata-safe evidence."""

    contract = load_workerbee_adapter_contract()
    descriptor = contract.descriptor(operation)
    if type(source_exit_code) is not int or source_exit_code not in descriptor.accepted_exit_codes:
        _fail("source-exit-mismatch")
    if descriptor.node_observation == "forbidden" and node_observation is not None:
        _fail("node-observation-mismatch")
    source = _source_mapping(source_payload)
    node_identity: dict[str, object] | None = None
    if operation == "list":
        payload = _project_list(source)
    elif operation == "doctor":
        payload = _project_doctor(source, source_exit_code)
    elif operation == "plan":
        payload = _project_plan(source)
    elif operation == "status":
        payload = _project_status(source)
    elif operation == "logs":
        payload = _project_logs(source)
    elif operation == "bootstrap":
        payload, node_identity = _project_bootstrap(source, node_observation)
    else:  # pragma: no cover - descriptor lookup is authoritative
        _fail("unsupported-operation")
    evidence_without_id: dict[str, object] = {
        "apiVersion": EVIDENCE_API_VERSION,
        "kind": "WorkerBeeProfileEvidence",
        "nodeIdentity": node_identity,
        "operation": operation,
        "payload": payload,
        "provenance": _provenance(descriptor, source_exit_code),
        "qualificationPosture": QUALIFICATION_POSTURE,
        "schemaVersion": SCHEMA_VERSION,
    }
    evidence = dict(evidence_without_id)
    evidence["evidenceId"] = _evidence_id(evidence_without_id)
    if len(_canonical_json(evidence, pretty=False)) > MAX_OUTPUT_BYTES:
        _fail("size-limit")
    return verify_workerbee_profile_evidence(evidence)


def _validate_connection(value: object) -> dict[str, object]:
    connection = _mapping(value, code="unsupported-schema")
    keys = {"addressClass", "portPresent", "presence", "schemeClass", "securityClass"}
    _exact_keys(connection, keys, code="unsupported-schema")
    presence = connection["presence"]
    if presence == "absent":
        if connection != _absent_connection():
            _fail("unsupported-schema")
    elif presence == "present":
        if connection["addressClass"] not in {"hostname", "ipv4", "ipv6"}:
            _fail("unsupported-schema")
        if type(connection["portPresent"]) is not bool:
            _fail("unsupported-schema")
        scheme = connection["schemeClass"]
        security = connection["securityClass"]
        if scheme is None:
            if security is not None or connection["portPresent"] is not True:
                _fail("unsupported-schema")
        elif scheme in {"http", "nats"}:
            if security != "cleartext":
                _fail("unsupported-schema")
        elif scheme in {"https", "tls"}:
            if security != "tls":
                _fail("unsupported-schema")
        else:
            _fail("unsupported-schema")
    else:
        _fail("unsupported-schema")
    return connection


def _validate_node_identity(value: object, *, operation: str) -> dict[str, object] | None:
    if operation != "bootstrap":
        if value is not None:
            _fail("unsupported-schema")
        return None
    identity = _mapping(value, code="unsupported-schema")
    _exact_keys(identity, _NODE_IDENTITY_KEYS, code="unsupported-schema")
    if (
        identity["identityMode"] != "explicit"
        or type(identity["identityVersion"]) is not int
        or identity["identityVersion"] != 1
    ):
        _fail("unsupported-schema")
    _node_id(identity["nodeId"])
    generation = identity["generation"]
    provenance = identity["generationProvenance"]
    if generation is None:
        if provenance != "unobserved":
            _fail("unsupported-schema")
    elif (
        type(generation) is not int
        or not 1 <= generation <= MAX_INT64
        or provenance != "k1s-controller-lease"
    ):
        _fail("unsupported-schema")
    return identity


def _validate_payload(operation: str, value: object) -> dict[str, object]:
    payload = _mapping(value, code="unsupported-schema")
    if operation == "list":
        expected = _project_list(profile_catalog_payload())
        if payload != expected:
            _fail("unsupported-schema")
    elif operation == "plan":
        _exact_keys(
            payload,
            {
                "alias",
                "bootstrapRequired",
                "canonicalName",
                "compatibilityVariant",
                "components",
                "contractPosture",
                "executionStatus",
                "productionEligible",
                "qualificationPosture",
                "requestedName",
                "requirements",
                "supportedBackends",
                "warningCodes",
            },
            code="unsupported-schema",
        )
        requested = _identifier(payload["requestedName"], field="requested name")
        expected = _project_plan(profile_plan_payload(requested))
        if payload != expected:
            _fail("unsupported-schema")
    elif operation == "doctor":
        _exact_keys(
            payload,
            {
                "alias",
                "canonicalName",
                "checks",
                "compatibilityVariant",
                "contractPosture",
                "executionStatus",
                "productionEligible",
                "qualificationPosture",
                "requestedName",
                "status",
            },
            code="unsupported-schema",
        )
        resolution = _resolution_fields(payload)
        requested = cast(str, resolution["requestedName"])
        expected_resolution = _resolution_fields(profile_plan_payload(requested))
        if _canonical_json(resolution, pretty=False) != _canonical_json(
            expected_resolution, pretty=False
        ):
            _fail("unsupported-schema")
        expected_ids = _DOCTOR_COMMON_CHECKS + (
            ("execution.portable", "backend.qualification")
            if resolution["executionStatus"] == "portable"
            else ("execution.deferred",)
        )
        checks = _array(payload["checks"], code="unsupported-schema")
        if len(checks) != len(expected_ids):
            _fail("unsupported-schema")
        statuses: list[str] = []
        for raw_check, expected_id in zip(checks, expected_ids, strict=True):
            check = _mapping(raw_check, code="unsupported-schema")
            _exact_keys(check, {"id", "status"}, code="unsupported-schema")
            if _text(check["id"], maximum=64, code="unsupported-schema") != expected_id:
                _fail("unsupported-schema")
            status = _text(check["status"], maximum=8, code="unsupported-schema")
            if status not in _CHECK_STATUSES:
                _fail("unsupported-schema")
            statuses.append(status)
        overall_status = payload["status"]
        recomputed = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
        if overall_status != recomputed:
            _fail("unsupported-schema")
    elif operation == "status":
        keys = {
            "backend",
            "lifecycleStatus",
            "phase",
            "profile",
            "profileGeneration",
            "qualificationPosture",
            "runRef",
        }
        _exact_keys(payload, keys, code="unsupported-schema")
        lifecycle_status = payload["lifecycleStatus"]
        if (
            lifecycle_status not in _LIFECYCLE_STATUSES
            or payload["profile"] not in _PORTABLE_PROFILES
        ):
            _fail("unsupported-schema")
        if payload["qualificationPosture"] != QUALIFICATION_POSTURE:
            _fail("unsupported-schema")
        if lifecycle_status == "absent":
            if any(
                payload[key] is not None
                for key in ("backend", "phase", "profileGeneration", "runRef")
            ):
                _fail("unsupported-schema")
        else:
            if payload["backend"] not in _BACKENDS or payload["phase"] not in _RUN_PHASES:
                _fail("unsupported-schema")
            _integer(payload["profileGeneration"], minimum=1)
            if type(payload["runRef"]) is not str or _DIGEST.fullmatch(payload["runRef"]) is None:
                _fail("unsupported-schema")
            if (lifecycle_status == "deleted" and payload["phase"] != "stopped") or (
                lifecycle_status != "deleted" and lifecycle_status != payload["phase"]
            ):
                _fail("unsupported-schema")
    elif operation == "logs":
        keys = {"component", "contentPolicy", "lineCount", "profile", "runRef", "sourceTruncated"}
        _exact_keys(payload, keys, code="unsupported-schema")
        profile = payload["profile"]
        if profile not in _PORTABLE_PROFILES or payload["component"] not in _COMPONENTS[profile]:
            _fail("unsupported-schema")
        if payload["contentPolicy"] != "omitted" or type(payload["sourceTruncated"]) is not bool:
            _fail("unsupported-schema")
        _integer(payload["lineCount"], minimum=0, maximum=MAX_LOG_LINES)
        if type(payload["runRef"]) is not str or _DIGEST.fullmatch(payload["runRef"]) is None:
            _fail("unsupported-schema")
    elif operation == "bootstrap":
        keys = {"backendId", "bootstrapId", "connections", "profile", "siteId"}
        _exact_keys(payload, keys, code="unsupported-schema")
        profile = payload["profile"]
        if profile not in {"edge-gateway", "node"}:
            _fail("unsupported-schema")
        _bootstrap_id(payload["bootstrapId"])
        connections = _mapping(payload["connections"], code="unsupported-schema")
        _exact_keys(
            connections,
            {"advertise", "controller", "listen", "transport"},
            code="unsupported-schema",
        )
        validated = {name: _validate_connection(item) for name, item in connections.items()}
        if validated["controller"]["presence"] == "present" and validated["controller"][
            "schemeClass"
        ] not in {"http", "https"}:
            _fail("unsupported-schema")
        if validated["advertise"]["presence"] == "present" and validated["advertise"][
            "schemeClass"
        ] not in {"http", "https"}:
            _fail("unsupported-schema")
        if validated["transport"]["presence"] == "present" and validated["transport"][
            "schemeClass"
        ] not in {"nats", "tls"}:
            _fail("unsupported-schema")
        if validated["listen"]["presence"] == "present" and (
            validated["listen"]["schemeClass"] is not None
            or validated["listen"]["securityClass"] is not None
        ):
            _fail("unsupported-schema")
        if profile == "edge-gateway":
            _node_id(payload["siteId"])
            if (
                payload["backendId"] != "gateway"
                or validated["transport"]["presence"] != "present"
                or any(
                    validated[name]["presence"] != "absent"
                    for name in ("advertise", "controller", "listen")
                )
            ):
                _fail("unsupported-schema")
        elif (
            payload["siteId"] is not None
            or payload["backendId"] not in {"podman", "docker", "strict-cri"}
            or validated["transport"]["presence"] != "absent"
            or any(
                validated[name]["presence"] != "present"
                for name in ("advertise", "controller", "listen")
            )
        ):
            _fail("unsupported-schema")
    else:
        _fail("unsupported-operation")
    if any(key in _FORBIDDEN_EVIDENCE_KEYS for key in payload):
        _fail("unsafe-metadata")
    return payload


def verify_workerbee_profile_evidence(
    value: Mapping[str, object] | bytes | str,
) -> dict[str, object]:
    """Verify exact evidence schema, identity, and canonical bytes when supplied."""

    raw: bytes | None = None
    if isinstance(value, str):
        raw = value.encode("utf-8")
        evidence = _mapping(
            _loads(raw, pretty=False, maximum=MAX_OUTPUT_BYTES), code="unsupported-schema"
        )
    elif isinstance(value, bytes):
        raw = value
        evidence = _mapping(
            _loads(raw, pretty=False, maximum=MAX_OUTPUT_BYTES), code="unsupported-schema"
        )
    else:
        evidence = _mapping(value, code="unsupported-schema")
        if len(_canonical_json(evidence, pretty=False)) > MAX_OUTPUT_BYTES:
            _fail("size-limit")
    _exact_keys(evidence, _EVIDENCE_KEYS, code="unsupported-schema")
    if (
        evidence["apiVersion"] != EVIDENCE_API_VERSION
        or evidence["kind"] != "WorkerBeeProfileEvidence"
        or evidence["qualificationPosture"] != QUALIFICATION_POSTURE
        or type(evidence["schemaVersion"]) is not int
        or evidence["schemaVersion"] != 1
    ):
        _fail("unsupported-schema")
    operation = evidence["operation"]
    if type(operation) is not str:
        _fail("unsupported-operation")
    descriptor = load_workerbee_adapter_contract().descriptor(operation)
    provenance = _mapping(evidence["provenance"], code="unsupported-schema")
    _exact_keys(provenance, _PROVENANCE_KEYS, code="unsupported-schema")
    source_exit_code = provenance["sourceExitCode"]
    if type(source_exit_code) is not int:
        _fail("unsupported-schema")
    expected_provenance = _provenance(descriptor, source_exit_code)
    if (
        provenance != expected_provenance
        or provenance["sourceExitCode"] not in descriptor.accepted_exit_codes
    ):
        _fail("unsupported-schema")
    payload = _validate_payload(operation, evidence["payload"])
    if operation == "doctor":
        expected_exit_code = 1 if payload["status"] == "fail" else 0
        if source_exit_code != expected_exit_code:
            _fail("unsupported-schema")
    node_identity = _validate_node_identity(evidence["nodeIdentity"], operation=operation)
    if operation == "bootstrap" and node_identity is not None:
        site_id = payload["siteId"]
        if site_id is not None and not cast(str, node_identity["nodeId"]).startswith(
            f"{site_id}--"
        ):
            _fail("unsupported-schema")
    evidence_without_id = {key: item for key, item in evidence.items() if key != "evidenceId"}
    expected_id = _evidence_id(evidence_without_id)
    evidence_id = evidence["evidenceId"]
    if type(evidence_id) is not str or not hmac.compare_digest(evidence_id, expected_id):
        _fail("unsupported-schema")
    canonical = _canonical_json(evidence, pretty=False)
    if raw is not None and raw != canonical:
        _fail("unsupported-schema")
    return dict(evidence)


def render_workerbee_profile_evidence_json(value: Mapping[str, object]) -> str:
    """Render one verified evidence object as exact compact JSON plus LF."""

    evidence = verify_workerbee_profile_evidence(value)
    return _canonical_json(evidence, pretty=False).decode("utf-8")


__all__ = [
    "ADAPTER_API_VERSION",
    "EVIDENCE_API_VERSION",
    "NODE_OBSERVATION_API_VERSION",
    "OperationDescriptor",
    "ProfileAdapterError",
    "WorkerBeeAdapterContract",
    "adapt_workerbee_profile_evidence",
    "load_workerbee_adapter_contract",
    "render_workerbee_profile_evidence_json",
    "verify_workerbee_profile_evidence",
]
