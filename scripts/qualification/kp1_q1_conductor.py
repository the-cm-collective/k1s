#!/usr/bin/env python3
"""Deterministic source-side conductor contract for K1s Q1 qualification.

QH3 defines the closed 14-stage order, authority binding, bounded adapter
surface, and canonical receipts used by a later live capsule.  This module does
not implement SSH, QMP, process discovery, privilege changes, or teardown.  A
caller must supply those adapters explicitly; tests use synthetic adapters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

try:
    from scripts.qualification import kp1_q1_capsule as q1_capsule
except ModuleNotFoundError:  # Direct execution from scripts/qualification.
    import kp1_q1_capsule as q1_capsule  # type: ignore[import-not-found,no-redef]

API_VERSION = "k1s.dev/q1-conductor/v1"
SCHEMA_VERSION = 1
RECEIPT_KIND = "K1sQ1ConductorReceipt"
SCHEMA_KIND = "K1sQ1ConductorSchema"

RUN_ID_RE = re.compile(r"q1-[0-9a-f]{32}")
TRANSACTION_ID_RE = re.compile(r"q1tx-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
FULL_OBJECT_RE = re.compile(r"[0-9a-f]{40}")
EVIDENCE_KIND_RE = re.compile(r"[a-z][a-z0-9.-]{0,63}")

MAX_STAGE_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_STAGE_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_STAGE_EVIDENCE_ITEMS = 64
MAX_RECEIPT_BYTES = 512 * 1024
DEFAULT_STAGE_TIMEOUT_SECONDS = 5 * 60
DEFAULT_TOTAL_TIMEOUT_SECONDS = 45 * 60

ACCEPTANCE_STAGES = (
    "boot-containment",
    "offline-install",
    "pure-contracts",
    "strict-cri-prepare-rollback",
    "core-podman",
    "core-docker",
    "edge-gateway",
    "node-strict-cri",
    "node-fencing",
    "security-context",
    "workload-lifecycle",
    "workerbee-evidence",
    "crash-recovery",
    "teardown",
)


class ConductorPolicyError(RuntimeError):
    """The conductor request or result failed the closed QH3 contract."""


def _fail() -> ConductorPolicyError:
    return ConductorPolicyError("K1s Q1 qualification conductor failed closed")


def canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    return (encoded + "\n").encode("utf-8")


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        raise _fail()
    return cast(dict[str, object], value)


def _bind(value: Mapping[str, object]) -> dict[str, object]:
    if "receiptDigest" in value:
        raise _fail()
    result = dict(value)
    result["receiptDigest"] = _digest_bytes(canonical_json_bytes(result))
    return result


@dataclass(frozen=True)
class RunAuthority:
    run_id: str
    plan_digest: str
    bundle_id: str
    bundle_sha256: str
    source_commit: str
    source_tree: str
    transaction_id: str

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "bundleId": self.bundle_id,
            "bundleSha256": self.bundle_sha256,
            "planDigest": self.plan_digest,
            "runId": self.run_id,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "transactionId": self.transaction_id,
        }
        return validate_authority(value)


def validate_authority(value: object) -> dict[str, object]:
    authority = _closed(
        value,
        {
            "bundleId",
            "bundleSha256",
            "planDigest",
            "runId",
            "sourceCommit",
            "sourceTree",
            "transactionId",
        },
    )
    run_id = authority["runId"]
    transaction_id = authority["transactionId"]
    if type(run_id) is not str or RUN_ID_RE.fullmatch(run_id) is None:
        raise _fail()
    if type(transaction_id) is not str or TRANSACTION_ID_RE.fullmatch(transaction_id) is None:
        raise _fail()
    for key in ("bundleId", "bundleSha256", "planDigest"):
        _digest(authority[key])
    for key in ("sourceCommit", "sourceTree"):
        candidate = authority[key]
        if type(candidate) is not str or FULL_OBJECT_RE.fullmatch(candidate) is None:
            raise _fail()
    return dict(authority)


@dataclass(frozen=True)
class StageSpec:
    name: str
    transport: str
    target: str
    argv: tuple[str, ...]
    timeout_seconds: int = DEFAULT_STAGE_TIMEOUT_SECONDS

    def as_dict(self) -> dict[str, object]:
        if (
            self.name not in ACCEPTANCE_STAGES
            or self.transport not in {"ssh", "qmp", "process"}
            or not self.target
            or not self.argv
            or any(type(item) is not str or not item or "\x00" in item for item in self.argv)
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= DEFAULT_TOTAL_TIMEOUT_SECONDS
        ):
            raise _fail()
        return {
            "argvSha256": _digest_bytes(canonical_json_bytes(list(self.argv))),
            "name": self.name,
            "target": self.target,
            "timeoutSeconds": self.timeout_seconds,
            "transport": self.transport,
        }


def _ssh_spec(name: str) -> StageSpec:
    return StageSpec(
        name=name,
        transport="ssh",
        target="q1-guest",
        argv=(
            "/usr/bin/python3.11",
            "/opt/k1s-q1/stages/kp1_q1_guest_stages.py",
            "--stage",
            name,
            "--protocol",
            API_VERSION,
        ),
    )


STAGE_SPECS = (
    StageSpec("boot-containment", "qmp", "q1-vm", ("query-status",)),
    _ssh_spec("offline-install"),
    _ssh_spec("pure-contracts"),
    _ssh_spec("strict-cri-prepare-rollback"),
    _ssh_spec("core-podman"),
    _ssh_spec("core-docker"),
    _ssh_spec("edge-gateway"),
    _ssh_spec("node-strict-cri"),
    _ssh_spec("node-fencing"),
    _ssh_spec("security-context"),
    _ssh_spec("workload-lifecycle"),
    _ssh_spec("workerbee-evidence"),
    StageSpec("crash-recovery", "qmp", "q1-vm", ("query-status",)),
    _ssh_spec("teardown"),
)

if tuple(spec.name for spec in STAGE_SPECS) != ACCEPTANCE_STAGES:
    raise RuntimeError("QH3 stage specifications diverge from the closed acceptance order")
if tuple(q1_capsule.ACCEPTANCE_STAGES) != ACCEPTANCE_STAGES:
    raise RuntimeError("QH3 stage order diverges from the capsule plan")


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, object]:
        if (
            type(self.kind) is not str
            or EVIDENCE_KIND_RE.fullmatch(self.kind) is None
            or type(self.size) is not int
            or not 0 <= self.size <= MAX_STAGE_EVIDENCE_BYTES
        ):
            raise _fail()
        return {"kind": self.kind, "sha256": _digest(self.sha256), "size": self.size}


@dataclass(frozen=True)
class StageObservation:
    identity: Mapping[str, object]
    exit_code: int
    timed_out: bool
    stdout: bytes = b""
    stderr: bytes = b""
    evidence: tuple[EvidenceItem, ...] = ()


@dataclass(frozen=True)
class StageRequest:
    authority: Mapping[str, object]
    stage: StageSpec
    deadline_monotonic: float
    maximum_output_bytes: int = MAX_STAGE_OUTPUT_BYTES
    maximum_evidence_bytes: int = MAX_STAGE_EVIDENCE_BYTES
    maximum_evidence_items: int = MAX_STAGE_EVIDENCE_ITEMS


class StageAdapter(Protocol):
    def execute(self, request: StageRequest) -> StageObservation: ...


@dataclass(frozen=True)
class AdapterSet:
    ssh: StageAdapter
    qmp: StageAdapter
    process: StageAdapter

    def for_transport(self, transport: str) -> StageAdapter:
        if transport == "ssh":
            return self.ssh
        if transport == "qmp":
            return self.qmp
        if transport == "process":
            return self.process
        raise _fail()


class BundleObserver(Protocol):
    def sha256(self) -> str: ...


class ReplayLedger(Protocol):
    def claim(self, receipt_id: str) -> bool: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_time(self) -> int: ...


@dataclass
class MemoryReplayLedger:
    claimed: set[str] = field(default_factory=set)

    def claim(self, receipt_id: str) -> bool:
        if receipt_id in self.claimed:
            return False
        self.claimed.add(receipt_id)
        return True


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_time(self) -> int:
        return int(time.time())


def stage_identity(authority: Mapping[str, object]) -> dict[str, object]:
    return validate_authority(authority)


def receipt_id(authority: Mapping[str, object]) -> str:
    payload = {
        "apiVersion": API_VERSION,
        "authority": validate_authority(authority),
        "kind": RECEIPT_KIND,
        "schemaVersion": SCHEMA_VERSION,
        "stageSpecifications": [spec.as_dict() for spec in STAGE_SPECS],
        "totalTimeoutSeconds": DEFAULT_TOTAL_TIMEOUT_SECONDS,
    }
    return _digest_bytes(b"k1s-q1-conductor-receipt-id\0" + canonical_json_bytes(payload))


def _output_descriptor(raw: bytes) -> dict[str, object]:
    return {
        "sha256": _digest_bytes(raw),
        "size": min(len(raw), MAX_STAGE_OUTPUT_BYTES + 1),
    }


def _empty_output() -> dict[str, object]:
    return {"stderr": _output_descriptor(b""), "stdout": _output_descriptor(b"")}


def _not_run_receipt(index: int, spec: StageSpec) -> dict[str, object]:
    return _bind(
        {
            "argvSha256": spec.as_dict()["argvSha256"],
            "bundleSha256After": None,
            "bundleSha256Before": None,
            "classification": "prior-stage-failed",
            "completedAtUnix": None,
            "evidence": [],
            "index": index,
            "name": spec.name,
            "output": _empty_output(),
            "startedAtUnix": None,
            "status": "not-run",
            "target": spec.target,
            "transport": spec.transport,
        }
    )


def _observation_details(
    observation: object,
    *,
    expected_identity: Mapping[str, object],
    elapsed: float,
    spec: StageSpec,
    bundle_before: str,
    bundle_after: str,
    expected_bundle: str,
) -> tuple[str, dict[str, object], list[dict[str, object]]]:
    if bundle_before != expected_bundle or bundle_after != expected_bundle:
        return "identity-drift", _empty_output(), []
    if type(observation) is not StageObservation:
        return "malformed-output", _empty_output(), []
    item = observation
    if type(item.exit_code) is not int or type(item.timed_out) is not bool:
        return "malformed-output", _empty_output(), []
    if type(item.stdout) is not bytes or type(item.stderr) is not bytes:
        return "malformed-output", _empty_output(), []
    output: dict[str, object] = {
        "stderr": _output_descriptor(item.stderr),
        "stdout": _output_descriptor(item.stdout),
    }
    try:
        observed_identity = validate_authority(item.identity)
    except ConductorPolicyError:
        return "malformed-output", output, []
    if observed_identity != dict(expected_identity):
        return "identity-drift", output, []
    if len(item.stdout) > MAX_STAGE_OUTPUT_BYTES or len(item.stderr) > MAX_STAGE_OUTPUT_BYTES:
        return "output-limit", output, []
    if type(item.evidence) is not tuple or len(item.evidence) > MAX_STAGE_EVIDENCE_ITEMS:
        return "evidence-limit", output, []
    try:
        evidence = [entry.as_dict() for entry in item.evidence]
    except (AttributeError, ConductorPolicyError):
        return "malformed-output", output, []
    if sum(cast(int, entry["size"]) for entry in evidence) > MAX_STAGE_EVIDENCE_BYTES:
        return "evidence-limit", output, []
    if item.timed_out or elapsed >= spec.timeout_seconds:
        return "timeout", output, evidence
    if item.exit_code != 0:
        return "exit-nonzero", output, evidence
    return "passed", output, evidence


def _stage_receipt(
    *,
    index: int,
    spec: StageSpec,
    authority: Mapping[str, object],
    adapters: AdapterSet,
    bundle: BundleObserver,
    clock: Clock,
    total_deadline: float,
) -> tuple[dict[str, object], bool]:
    started_wall = clock.wall_time()
    started = clock.monotonic()
    expected_bundle = cast(str, authority["bundleSha256"])
    try:
        bundle_before = _digest(bundle.sha256())
    except Exception:  # Adapter detail must not cross the public receipt boundary.
        bundle_before = "sha256:" + "0" * 64
    classification = "adapter-error"
    output = _empty_output()
    evidence: list[dict[str, object]] = []
    bundle_after = bundle_before
    if bundle_before != expected_bundle:
        classification = "identity-drift"
    elif started >= total_deadline:
        classification = "total-timeout"
    else:
        request = StageRequest(
            authority=dict(authority),
            stage=spec,
            deadline_monotonic=min(total_deadline, started + spec.timeout_seconds),
        )
        try:
            observation: object = adapters.for_transport(spec.transport).execute(request)
            elapsed = clock.monotonic() - started
            try:
                bundle_after = _digest(bundle.sha256())
            except Exception:
                bundle_after = "sha256:" + "0" * 64
            classification, output, evidence = _observation_details(
                observation,
                expected_identity=stage_identity(authority),
                elapsed=elapsed,
                spec=spec,
                bundle_before=bundle_before,
                bundle_after=bundle_after,
                expected_bundle=expected_bundle,
            )
            if clock.monotonic() >= total_deadline and classification != "identity-drift":
                classification = "total-timeout"
        except Exception:
            classification = "adapter-error"
    completed_wall = clock.wall_time()
    passed = classification == "passed"
    receipt = _bind(
        {
            "argvSha256": spec.as_dict()["argvSha256"],
            "bundleSha256After": bundle_after,
            "bundleSha256Before": bundle_before,
            "classification": classification,
            "completedAtUnix": completed_wall,
            "evidence": evidence,
            "index": index,
            "name": spec.name,
            "output": output,
            "startedAtUnix": started_wall,
            "status": "passed" if passed else "failed",
            "target": spec.target,
            "transport": spec.transport,
        }
    )
    return receipt, passed


def run_conductor(
    authority: RunAuthority | Mapping[str, object],
    *,
    adapters: AdapterSet,
    bundle: BundleObserver,
    replay_ledger: ReplayLedger,
    clock: Clock | None = None,
    total_timeout_seconds: int = DEFAULT_TOTAL_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Execute the closed stage contract through caller-supplied adapters."""

    if (
        type(total_timeout_seconds) is not int
        or total_timeout_seconds != DEFAULT_TOTAL_TIMEOUT_SECONDS
    ):
        raise _fail()
    authority_value = (
        authority.as_dict() if type(authority) is RunAuthority else validate_authority(authority)
    )
    expected_bundle = cast(str, authority_value["bundleSha256"])
    try:
        initial_bundle = _digest(bundle.sha256())
    except Exception as exc:
        raise _fail() from exc
    if initial_bundle != expected_bundle:
        raise _fail()
    rid = receipt_id(authority_value)
    try:
        claimed = replay_ledger.claim(rid)
    except Exception as exc:
        raise _fail() from exc
    if type(claimed) is not bool or not claimed:
        raise _fail()
    active_clock = clock or SystemClock()
    started_wall = active_clock.wall_time()
    total_deadline = active_clock.monotonic() + total_timeout_seconds
    receipts: list[dict[str, object]] = []
    failed_stage: str | None = None
    for index, spec in enumerate(STAGE_SPECS, 1):
        if failed_stage is not None:
            receipts.append(_not_run_receipt(index, spec))
            continue
        receipt, passed = _stage_receipt(
            index=index,
            spec=spec,
            authority=authority_value,
            adapters=adapters,
            bundle=bundle,
            clock=active_clock,
            total_deadline=total_deadline,
        )
        receipts.append(receipt)
        if not passed:
            failed_stage = spec.name
    result = _bind(
        {
            "apiVersion": API_VERSION,
            "authority": authority_value,
            "completedAtUnix": active_clock.wall_time(),
            "failedStage": failed_stage,
            "kind": RECEIPT_KIND,
            "receiptId": rid,
            "schemaVersion": SCHEMA_VERSION,
            "stageOrder": list(ACCEPTANCE_STAGES),
            "stageReceipts": receipts,
            "startedAtUnix": started_wall,
            "status": "passed" if failed_stage is None else "failed",
            "teardownScheduled": failed_stage is not None,
            "totalTimeoutSeconds": DEFAULT_TOTAL_TIMEOUT_SECONDS,
        }
    )
    if len(canonical_json_bytes(result)) > MAX_RECEIPT_BYTES:
        raise _fail()
    return verify_receipt(result, authority_value)


_STAGE_RECEIPT_KEYS = {
    "argvSha256",
    "bundleSha256After",
    "bundleSha256Before",
    "classification",
    "completedAtUnix",
    "evidence",
    "index",
    "name",
    "output",
    "receiptDigest",
    "startedAtUnix",
    "status",
    "target",
    "transport",
}


def _verify_stage_receipt(
    value: object,
    index: int,
    spec: StageSpec,
    expected_bundle: str,
) -> dict[str, object]:
    receipt = _closed(value, _STAGE_RECEIPT_KEYS)
    unsigned = dict(receipt)
    digest = unsigned.pop("receiptDigest")
    if _digest(digest) != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    if (
        type(receipt["index"]) is not int
        or receipt["index"] != index
        or receipt["name"] != spec.name
        or receipt["transport"] != spec.transport
        or receipt["target"] != spec.target
        or receipt["argvSha256"] != spec.as_dict()["argvSha256"]
    ):
        raise _fail()
    if receipt["status"] not in {"passed", "failed", "not-run"}:
        raise _fail()
    output = _closed(receipt["output"], {"stderr", "stdout"})
    for stream in output.values():
        descriptor = _closed(stream, {"sha256", "size"})
        _digest(descriptor["sha256"])
        if (
            type(descriptor["size"]) is not int
            or not 0 <= descriptor["size"] <= MAX_STAGE_OUTPUT_BYTES + 1
        ):
            raise _fail()
    evidence = receipt["evidence"]
    if type(evidence) is not list or len(evidence) > MAX_STAGE_EVIDENCE_ITEMS:
        raise _fail()
    evidence_size = 0
    for raw in evidence:
        item = _closed(raw, {"kind", "sha256", "size"})
        if type(item["kind"]) is not str or EVIDENCE_KIND_RE.fullmatch(item["kind"]) is None:
            raise _fail()
        _digest(item["sha256"])
        if type(item["size"]) is not int or not 0 <= item["size"] <= MAX_STAGE_EVIDENCE_BYTES:
            raise _fail()
        evidence_size += item["size"]
    if evidence_size > MAX_STAGE_EVIDENCE_BYTES:
        raise _fail()
    status = receipt["status"]
    if status == "not-run":
        if (
            receipt["classification"] != "prior-stage-failed"
            or receipt["startedAtUnix"] is not None
            or receipt["completedAtUnix"] is not None
            or receipt["bundleSha256Before"] is not None
            or receipt["bundleSha256After"] is not None
            or evidence
            or output != _empty_output()
        ):
            raise _fail()
    else:
        if type(receipt["startedAtUnix"]) is not int or type(receipt["completedAtUnix"]) is not int:
            raise _fail()
        if receipt["completedAtUnix"] < receipt["startedAtUnix"]:
            raise _fail()
        _digest(receipt["bundleSha256Before"])
        _digest(receipt["bundleSha256After"])
        bundle_matches_authority = (
            receipt["bundleSha256Before"] == expected_bundle
            and receipt["bundleSha256After"] == expected_bundle
        )
        if receipt["status"] == "passed" and not bundle_matches_authority:
            raise _fail()
        if receipt["classification"] != "identity-drift" and not bundle_matches_authority:
            raise _fail()
        if status == "passed" and receipt["classification"] != "passed":
            raise _fail()
        if status == "failed" and receipt["classification"] not in {
            "adapter-error",
            "evidence-limit",
            "exit-nonzero",
            "identity-drift",
            "malformed-output",
            "output-limit",
            "timeout",
            "total-timeout",
        }:
            raise _fail()
    return dict(receipt)


def verify_receipt(
    value: object,
    expected_authority: RunAuthority | Mapping[str, object],
) -> dict[str, object]:
    expected = (
        expected_authority.as_dict()
        if type(expected_authority) is RunAuthority
        else validate_authority(expected_authority)
    )
    receipt = _closed(
        value,
        {
            "apiVersion",
            "authority",
            "completedAtUnix",
            "failedStage",
            "kind",
            "receiptDigest",
            "receiptId",
            "schemaVersion",
            "stageOrder",
            "stageReceipts",
            "startedAtUnix",
            "status",
            "teardownScheduled",
            "totalTimeoutSeconds",
        },
    )
    unsigned = dict(receipt)
    digest = unsigned.pop("receiptDigest")
    if _digest(digest) != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != RECEIPT_KIND
        or type(receipt["schemaVersion"]) is not int
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or validate_authority(receipt["authority"]) != expected
        or receipt["receiptId"] != receipt_id(expected)
        or receipt["stageOrder"] != list(ACCEPTANCE_STAGES)
        or type(receipt["startedAtUnix"]) is not int
        or type(receipt["completedAtUnix"]) is not int
        or type(receipt["teardownScheduled"]) is not bool
        or type(receipt["totalTimeoutSeconds"]) is not int
        or receipt["totalTimeoutSeconds"] != DEFAULT_TOTAL_TIMEOUT_SECONDS
    ):
        raise _fail()
    if receipt["completedAtUnix"] < receipt["startedAtUnix"]:
        raise _fail()
    raw_stages = receipt["stageReceipts"]
    if type(raw_stages) is not list or len(raw_stages) != len(STAGE_SPECS):
        raise _fail()
    stages = [
        _verify_stage_receipt(raw, index, spec, cast(str, expected["bundleSha256"]))
        for index, (raw, spec) in enumerate(zip(raw_stages, STAGE_SPECS, strict=True), 1)
    ]
    failed = [stage for stage in stages if stage["status"] == "failed"]
    first_not_run = next(
        (i for i, stage in enumerate(stages) if stage["status"] == "not-run"), None
    )
    if receipt["status"] == "passed":
        if (
            failed
            or first_not_run is not None
            or receipt["failedStage"] is not None
            or receipt["teardownScheduled"]
        ):
            raise _fail()
    elif receipt["status"] == "failed":
        if (
            len(failed) != 1
            or receipt["failedStage"] != failed[0]["name"]
            or not receipt["teardownScheduled"]
        ):
            raise _fail()
        failed_index = cast(int, failed[0]["index"])
        if any(stage["status"] != "passed" for stage in stages[: failed_index - 1]):
            raise _fail()
        if any(stage["status"] != "not-run" for stage in stages[failed_index:]):
            raise _fail()
    else:
        raise _fail()
    if len(canonical_json_bytes(receipt)) > MAX_RECEIPT_BYTES:
        raise _fail()
    return dict(receipt)


def schema_document() -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "kind": SCHEMA_KIND,
        "limits": {
            "maximumEvidenceBytesPerStage": MAX_STAGE_EVIDENCE_BYTES,
            "maximumEvidenceItemsPerStage": MAX_STAGE_EVIDENCE_ITEMS,
            "maximumOutputBytesPerStream": MAX_STAGE_OUTPUT_BYTES,
            "maximumReceiptBytes": MAX_RECEIPT_BYTES,
            "totalTimeoutSeconds": DEFAULT_TOTAL_TIMEOUT_SECONDS,
        },
        "schemaVersion": SCHEMA_VERSION,
        "stages": [spec.as_dict() for spec in STAGE_SPECS],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("schema", help="print the non-mutating closed QH3 stage schema")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "schema":
        print(canonical_json_bytes(schema_document()).decode("utf-8"), end="")
        return 0
    raise _fail()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConductorPolicyError as exc:
        print(str(exc), file=__import__("sys").stderr)
        raise SystemExit(2) from None
