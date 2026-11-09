from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

import pytest
from scripts.qualification import kp1_q1_capsule as capsule
from scripts.qualification import kp1_q1_conductor as conductor


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _authority() -> conductor.RunAuthority:
    return conductor.RunAuthority(
        run_id="q1-1234567890abcdef1234567890abcdef",
        plan_digest="sha256:" + "1" * 64,
        bundle_id="sha256:" + "2" * 64,
        bundle_sha256="sha256:" + "3" * 64,
        source_commit="4" * 40,
        source_tree="5" * 40,
        transaction_id="q1tx-67890abcdef1234567890abcdef12345",
    )


@dataclass
class FakeClock:
    now: float = 100.0
    epoch: int = 2_000_000_000

    def monotonic(self) -> float:
        return self.now

    def wall_time(self) -> int:
        return self.epoch + int(self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeBundle:
    digest: str

    def sha256(self) -> str:
        return self.digest


@dataclass
class SequencedBundle:
    digests: list[str]
    calls: int = 0

    def sha256(self) -> str:
        if not self.digests:
            raise RuntimeError("private-bundle-observer-detail")
        index = min(self.calls, len(self.digests) - 1)
        self.calls += 1
        return self.digests[index]


@dataclass
class FailingBundle:
    calls: int = 0

    def sha256(self) -> str:
        self.calls += 1
        raise RuntimeError("private-bundle-observer-detail")


@dataclass
class FakeAdapter:
    authority: conductor.RunAuthority
    clock: FakeClock
    bundle: FakeBundle
    behavior: dict[str, str] = field(default_factory=dict)
    calls: list[conductor.StageRequest] = field(default_factory=list)

    def execute(self, request: conductor.StageRequest) -> conductor.StageObservation:
        self.calls.append(request)
        mode = self.behavior.get(request.stage.name, "pass")
        if mode == "raise":
            raise RuntimeError("private-adapter-secret")
        if mode == "slow":
            self.clock.advance(request.stage.timeout_seconds + 1)
        elif mode == "exact-timeout":
            self.clock.advance(request.stage.timeout_seconds)
        elif mode == "total-timeout":
            self.clock.advance(conductor.DEFAULT_TOTAL_TIMEOUT_SECONDS)
        else:
            self.clock.advance(1)
        identity = conductor.stage_identity(self.authority.as_dict())
        if mode == "identity-drift":
            identity["transactionId"] = "q1tx-" + "f" * 32
        if mode == "source-drift":
            identity["sourceCommit"] = "f" * 40
        if mode == "authority-drift":
            identity["bundleSha256"] = "sha256:" + "f" * 64
        if mode == "incomplete-authority":
            identity.pop("sourceTree")
        if mode == "bundle-drift":
            self.bundle.digest = "sha256:" + "e" * 64
        if mode == "malformed":
            return cast_observation(object())
        stdout = b"safe-output"
        if mode == "secret-output":
            stdout = b"SUPER_SECRET_VALUE"
        if mode == "output-limit":
            stdout = b"x" * (conductor.MAX_STAGE_OUTPUT_BYTES + 1)
        evidence: tuple[conductor.EvidenceItem, ...] = (
            conductor.EvidenceItem("stage-evidence", _digest(b"evidence"), len(b"evidence")),
        )
        if mode == "evidence-count":
            evidence = tuple(
                conductor.EvidenceItem(f"evidence-{index}", _digest(str(index).encode()), 1)
                for index in range(conductor.MAX_STAGE_EVIDENCE_ITEMS + 1)
            )
        if mode == "evidence-size":
            evidence = (
                conductor.EvidenceItem(
                    "oversized-evidence",
                    _digest(b"oversized"),
                    conductor.MAX_STAGE_EVIDENCE_BYTES,
                ),
                conductor.EvidenceItem("second-evidence", _digest(b"second"), 1),
            )
        return conductor.StageObservation(
            identity=identity,
            exit_code=1 if mode == "exit" else 0,
            timed_out=mode == "timeout",
            stdout=stdout,
            stderr=b"safe-error" if mode == "exit" else b"",
            evidence=evidence,
        )


def cast_observation(value: object) -> conductor.StageObservation:
    return value  # type: ignore[return-value]


def _harness(
    behavior: dict[str, str] | None = None,
) -> tuple[
    conductor.RunAuthority,
    FakeClock,
    FakeBundle,
    FakeAdapter,
    conductor.AdapterSet,
    conductor.MemoryReplayLedger,
]:
    authority = _authority()
    clock = FakeClock()
    bundle = FakeBundle(authority.bundle_sha256)
    adapter = FakeAdapter(authority, clock, bundle, behavior or {})
    adapters = conductor.AdapterSet(ssh=adapter, qmp=adapter, process=adapter)
    return authority, clock, bundle, adapter, adapters, conductor.MemoryReplayLedger()


def _run(behavior: dict[str, str] | None = None):
    authority, clock, bundle, adapter, adapters, ledger = _harness(behavior)
    receipt = conductor.run_conductor(
        authority,
        adapters=adapters,
        bundle=bundle,
        replay_ledger=ledger,
        clock=clock,
    )
    return authority, receipt, adapter, ledger


def _resign(value: dict[str, object]) -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop("receiptDigest", None)
    unsigned["receiptDigest"] = _digest(conductor.canonical_json_bytes(unsigned))
    return unsigned


def test_closed_stage_order_matches_capsule_and_uses_fixed_non_shell_requests() -> None:
    assert conductor.ACCEPTANCE_STAGES == capsule.ACCEPTANCE_STAGES
    assert tuple(spec.name for spec in conductor.STAGE_SPECS) == conductor.ACCEPTANCE_STAGES
    assert {spec.transport for spec in conductor.STAGE_SPECS} == {"ssh", "qmp", "process"}
    for spec in conductor.STAGE_SPECS:
        assert spec.target in {"q1-guest", "q1-host", "q1-vm"}
        assert isinstance(spec.argv, tuple)
        assert spec.argv
        assert not any(token in {"sh", "bash", "-c"} for token in spec.argv)


def test_all_stages_pass_in_exact_order_and_receipt_validation_is_idempotent() -> None:
    authority, receipt, adapter, _ledger = _run()
    assert receipt["status"] == "passed"
    assert receipt["failedStage"] is None
    assert receipt["teardownScheduled"] is False
    assert [request.stage.name for request in adapter.calls] == list(conductor.ACCEPTANCE_STAGES)
    assert [item["status"] for item in receipt["stageReceipts"]] == ["passed"] * 14
    assert conductor.verify_receipt(receipt, authority) == receipt
    assert conductor.verify_receipt(receipt, authority) == receipt


def test_first_failure_stops_execution_and_marks_every_later_stage_not_run() -> None:
    authority, receipt, adapter, _ledger = _run({"core-podman": "exit"})
    assert receipt["status"] == "failed"
    assert receipt["failedStage"] == "core-podman"
    assert receipt["teardownScheduled"] is True
    assert [request.stage.name for request in adapter.calls] == list(
        conductor.ACCEPTANCE_STAGES[:5]
    )
    stages = receipt["stageReceipts"]
    assert [item["status"] for item in stages[:4]] == ["passed"] * 4
    assert stages[4]["classification"] == "exit-nonzero"
    assert [item["status"] for item in stages[5:]] == ["not-run"] * 9
    assert stages[-1]["name"] == "teardown"
    assert stages[-1]["classification"] == "prior-stage-failed"
    conductor.verify_receipt(receipt, authority)


@pytest.mark.parametrize(
    ("mode", "classification"),
    [
        ("timeout", "timeout"),
        ("slow", "timeout"),
        ("exact-timeout", "timeout"),
        ("malformed", "malformed-output"),
        ("identity-drift", "identity-drift"),
        ("source-drift", "identity-drift"),
        ("authority-drift", "identity-drift"),
        ("incomplete-authority", "malformed-output"),
        ("bundle-drift", "identity-drift"),
        ("output-limit", "output-limit"),
        ("evidence-count", "evidence-limit"),
        ("evidence-size", "evidence-limit"),
        ("raise", "adapter-error"),
    ],
)
def test_stage_failures_are_deterministic_and_metadata_safe(mode: str, classification: str) -> None:
    authority, receipt, _adapter, _ledger = _run({"offline-install": mode})
    failed = receipt["stageReceipts"][1]
    assert receipt["failedStage"] == "offline-install"
    assert failed["status"] == "failed"
    assert failed["classification"] == classification
    serialized = conductor.canonical_json_bytes(receipt)
    assert b"private-adapter-secret" not in serialized
    conductor.verify_receipt(receipt, authority)


def test_raw_output_is_hashed_and_never_stored_in_receipt() -> None:
    authority, receipt, _adapter, _ledger = _run({"boot-containment": "secret-output"})
    serialized = conductor.canonical_json_bytes(receipt)
    assert b"SUPER_SECRET_VALUE" not in serialized
    first = receipt["stageReceipts"][0]
    assert first["output"]["stdout"] == {
        "sha256": _digest(b"SUPER_SECRET_VALUE"),
        "size": len(b"SUPER_SECRET_VALUE"),
    }
    conductor.verify_receipt(receipt, authority)


def test_fixed_total_deadline_fails_current_stage_and_schedules_teardown() -> None:
    authority, receipt, adapter, _ledger = _run({"boot-containment": "total-timeout"})
    assert receipt["status"] == "failed"
    assert receipt["failedStage"] == "boot-containment"
    assert receipt["teardownScheduled"] is True
    assert receipt["totalTimeoutSeconds"] == conductor.DEFAULT_TOTAL_TIMEOUT_SECONDS
    assert any(item["classification"] == "total-timeout" for item in receipt["stageReceipts"])
    assert len(adapter.calls) == 1
    conductor.verify_receipt(receipt, authority)


def test_initial_bundle_drift_blocks_replay_claim_and_every_adapter() -> None:
    authority, clock, bundle, adapter, adapters, ledger = _harness()
    bundle.digest = "sha256:" + "e" * 64
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.run_conductor(
            authority,
            adapters=adapters,
            bundle=bundle,
            replay_ledger=ledger,
            clock=clock,
        )
    assert adapter.calls == []
    assert ledger.claimed == set()

    bundle.digest = authority.bundle_sha256
    receipt = conductor.run_conductor(
        authority,
        adapters=adapters,
        bundle=bundle,
        replay_ledger=ledger,
        clock=clock,
    )
    assert receipt["status"] == "passed"


def test_initial_bundle_observer_error_blocks_replay_claim_and_every_adapter() -> None:
    authority, clock, _bundle, adapter, adapters, ledger = _harness()
    failing_bundle = FailingBundle()
    with pytest.raises(conductor.ConductorPolicyError) as failure:
        conductor.run_conductor(
            authority,
            adapters=adapters,
            bundle=failing_bundle,
            replay_ledger=ledger,
            clock=clock,
        )
    assert str(failure.value) == "K1s Q1 qualification conductor failed closed"
    assert failing_bundle.calls == 1
    assert adapter.calls == []
    assert ledger.claimed == set()


def test_unreadable_initial_bundle_and_invalid_replay_ledger_fail_generically() -> None:
    authority, clock, _bundle, adapter, adapters, ledger = _harness()
    unreadable = SequencedBundle([])
    with pytest.raises(
        conductor.ConductorPolicyError,
        match="K1s Q1 qualification conductor failed closed",
    ):
        conductor.run_conductor(
            authority,
            adapters=adapters,
            bundle=unreadable,
            replay_ledger=ledger,
            clock=clock,
        )
    assert adapter.calls == []
    assert ledger.claimed == set()

    class InvalidLedger:
        def claim(self, _receipt_id: str) -> int:
            return 1

    with pytest.raises(conductor.ConductorPolicyError):
        conductor.run_conductor(
            authority,
            adapters=adapters,
            bundle=FakeBundle(authority.bundle_sha256),
            replay_ledger=InvalidLedger(),  # type: ignore[arg-type]
            clock=clock,
        )
    assert adapter.calls == []


def test_bundle_drift_before_a_later_stage_blocks_that_adapter_call() -> None:
    authority, clock, _bundle, adapter, adapters, ledger = _harness()
    expected = authority.bundle_sha256
    observed = SequencedBundle([expected, expected, expected, "sha256:" + "e" * 64])
    receipt = conductor.run_conductor(
        authority,
        adapters=adapters,
        bundle=observed,
        replay_ledger=ledger,
        clock=clock,
    )
    assert [request.stage.name for request in adapter.calls] == ["boot-containment"]
    assert receipt["failedStage"] == "offline-install"
    assert receipt["stageReceipts"][1]["classification"] == "identity-drift"
    conductor.verify_receipt(receipt, authority)


def test_replay_ledger_rejects_duplicate_authority_before_adapter_execution() -> None:
    authority, clock, bundle, adapter, adapters, ledger = _harness()
    conductor.run_conductor(
        authority,
        adapters=adapters,
        bundle=bundle,
        replay_ledger=ledger,
        clock=clock,
    )
    prior_call_count = len(adapter.calls)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.run_conductor(
            authority,
            adapters=adapters,
            bundle=bundle,
            replay_ledger=ledger,
            clock=clock,
        )
    assert len(adapter.calls) == prior_call_count


@pytest.mark.parametrize(
    "field",
    [
        "runId",
        "planDigest",
        "bundleId",
        "bundleSha256",
        "sourceCommit",
        "sourceTree",
        "transactionId",
    ],
)
def test_authority_identity_drift_fails_closed(field: str) -> None:
    value = _authority().as_dict()
    value[field] = "invalid"
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.validate_authority(value)


def test_receipt_digest_stage_order_and_stage_identity_tampering_fail_closed() -> None:
    authority, receipt, _adapter, _ledger = _run()

    bad_digest = copy.deepcopy(receipt)
    bad_digest["status"] = "failed"
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(bad_digest, authority)

    bad_order = copy.deepcopy(receipt)
    bad_order["stageOrder"][0], bad_order["stageOrder"][1] = (
        bad_order["stageOrder"][1],
        bad_order["stageOrder"][0],
    )
    bad_order = _resign(bad_order)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(bad_order, authority)

    skipped = copy.deepcopy(receipt)
    skipped_stage = dict(skipped["stageReceipts"][3])
    skipped_stage["name"] = "core-podman"
    skipped["stageReceipts"][3] = _resign(skipped_stage)
    skipped = _resign(skipped)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(skipped, authority)


def test_passed_stage_bundle_identity_cannot_be_resigned_away_from_authority() -> None:
    authority, receipt, _adapter, _ledger = _run()
    tampered = copy.deepcopy(receipt)
    stage = dict(tampered["stageReceipts"][0])
    stage["bundleSha256Before"] = "sha256:" + "e" * 64
    stage["bundleSha256After"] = "sha256:" + "e" * 64
    tampered["stageReceipts"][0] = _resign(stage)
    tampered = _resign(tampered)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(tampered, authority)


def test_receipt_schema_rejects_boolean_substitution_and_not_run_output() -> None:
    authority, passed, _adapter, _ledger = _run()
    wrong_schema_type = copy.deepcopy(passed)
    wrong_schema_type["schemaVersion"] = True
    wrong_schema_type = _resign(wrong_schema_type)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(wrong_schema_type, authority)

    wrong_total = copy.deepcopy(passed)
    wrong_total["totalTimeoutSeconds"] = conductor.DEFAULT_TOTAL_TIMEOUT_SECONDS + 1
    wrong_total = _resign(wrong_total)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(wrong_total, authority)

    wrong_index_type = copy.deepcopy(passed)
    first = dict(wrong_index_type["stageReceipts"][0])
    first["index"] = True
    wrong_index_type["stageReceipts"][0] = _resign(first)
    wrong_index_type = _resign(wrong_index_type)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(wrong_index_type, authority)

    authority, failed, _adapter, _ledger = _run({"boot-containment": "exit"})
    contradictory = copy.deepcopy(failed)
    stage = dict(contradictory["stageReceipts"][1])
    stage["output"] = {
        "stderr": {"sha256": _digest(b""), "size": 0},
        "stdout": {"sha256": _digest(b"not-run-output"), "size": 14},
    }
    contradictory["stageReceipts"][1] = _resign(stage)
    contradictory = _resign(contradictory)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(contradictory, authority)


def test_adapter_identity_carries_complete_frozen_authority() -> None:
    authority, receipt, adapter, _ledger = _run()
    assert receipt["status"] == "passed"
    assert conductor.stage_identity(authority.as_dict()) == authority.as_dict()
    assert all(request.authority == authority.as_dict() for request in adapter.calls)


@pytest.mark.parametrize("mode", ["authority-drift", "incomplete-authority"])
def test_adapter_observation_requires_complete_exact_authority(mode: str) -> None:
    authority, receipt, adapter, _ledger = _run({"boot-containment": mode})
    assert len(adapter.calls) == 1
    assert receipt["status"] == "failed"
    assert receipt["failedStage"] == "boot-containment"
    assert receipt["stageReceipts"][0]["classification"] in {
        "identity-drift",
        "malformed-output",
    }
    conductor.verify_receipt(receipt, authority)


def test_failed_receipt_cannot_hide_a_later_passed_stage() -> None:
    authority, receipt, _adapter, _ledger = _run({"offline-install": "exit"})
    tampered = copy.deepcopy(receipt)
    later = dict(tampered["stageReceipts"][2])
    later.update(
        {
            "bundleSha256After": authority.bundle_sha256,
            "bundleSha256Before": authority.bundle_sha256,
            "classification": "passed",
            "completedAtUnix": 1,
            "startedAtUnix": 1,
            "status": "passed",
        }
    )
    tampered["stageReceipts"][2] = _resign(later)
    tampered = _resign(tampered)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(tampered, authority)


def test_receipt_is_canonical_bounded_and_contains_no_adapter_commands() -> None:
    _authority_value, receipt, _adapter, _ledger = _run()
    raw = conductor.canonical_json_bytes(receipt)
    assert len(raw) < conductor.MAX_RECEIPT_BYTES
    assert raw.endswith(b"\n")
    assert json.loads(raw) == receipt
    assert b"shell" not in raw.lower()


def test_schema_command_is_static_and_nonmutating(capsys: pytest.CaptureFixture[str]) -> None:
    assert conductor.main(["schema"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["kind"] == conductor.SCHEMA_KIND
    assert [item["name"] for item in parsed["stages"]] == list(conductor.ACCEPTANCE_STAGES)
    assert parsed["limits"]["maximumReceiptBytes"] == conductor.MAX_RECEIPT_BYTES


def test_invalid_limits_and_noncanonical_evidence_fail_closed() -> None:
    authority, clock, bundle, _adapter, adapters, ledger = _harness()
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.run_conductor(
            authority,
            adapters=adapters,
            bundle=bundle,
            replay_ledger=ledger,
            clock=clock,
            total_timeout_seconds=True,
        )
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.run_conductor(
            authority,
            adapters=adapters,
            bundle=bundle,
            replay_ledger=ledger,
            clock=clock,
            total_timeout_seconds=conductor.DEFAULT_TOTAL_TIMEOUT_SECONDS + 1,
        )
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.EvidenceItem("Bad Kind", _digest(b"x"), 1).as_dict()


def test_total_deadline_is_fixed_bound_and_cannot_be_omitted_or_resigned() -> None:
    authority, receipt, _adapter, _ledger = _run()
    assert receipt["totalTimeoutSeconds"] == conductor.DEFAULT_TOTAL_TIMEOUT_SECONDS

    omitted = copy.deepcopy(receipt)
    omitted.pop("totalTimeoutSeconds")
    omitted = _resign(omitted)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(omitted, authority)

    for replacement in (True, conductor.DEFAULT_TOTAL_TIMEOUT_SECONDS + 1):
        tampered = copy.deepcopy(receipt)
        tampered["totalTimeoutSeconds"] = replacement
        tampered = _resign(tampered)
        with pytest.raises(conductor.ConductorPolicyError):
            conductor.verify_receipt(tampered, authority)

    alternate_identity_payload = {
        "apiVersion": conductor.API_VERSION,
        "authority": authority.as_dict(),
        "kind": conductor.RECEIPT_KIND,
        "schemaVersion": conductor.SCHEMA_VERSION,
        "stageSpecifications": [spec.as_dict() for spec in conductor.STAGE_SPECS],
        "totalTimeoutSeconds": conductor.DEFAULT_TOTAL_TIMEOUT_SECONDS + 1,
    }
    alternate_id = _digest(
        b"k1s-q1-conductor-receipt-id\0"
        + conductor.canonical_json_bytes(alternate_identity_payload)
    )
    assert alternate_id != conductor.receipt_id(authority.as_dict())


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("index", True),
        ("startedAtUnix", True),
        ("completedAtUnix", True),
    ],
)
def test_stage_integer_fields_reject_boolean_substitution(field: str, replacement: bool) -> None:
    authority, receipt, _adapter, _ledger = _run()
    tampered = copy.deepcopy(receipt)
    stage = dict(tampered["stageReceipts"][0])
    stage[field] = replacement
    tampered["stageReceipts"][0] = _resign(stage)
    tampered = _resign(tampered)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(tampered, authority)


@pytest.mark.parametrize("contradiction", ["stdout", "stderr", "evidence"])
def test_not_run_receipts_require_canonical_empty_descriptors(contradiction: str) -> None:
    authority, receipt, _adapter, _ledger = _run({"boot-containment": "exit"})
    tampered = copy.deepcopy(receipt)
    stage = dict(tampered["stageReceipts"][1])
    if contradiction == "evidence":
        stage["evidence"] = [
            conductor.EvidenceItem("unexpected", _digest(b"unexpected"), 10).as_dict()
        ]
    else:
        output = copy.deepcopy(stage["output"])
        output[contradiction] = {
            "sha256": _digest(b"unexpected"),
            "size": len(b"unexpected"),
        }
        stage["output"] = output
    tampered["stageReceipts"][1] = _resign(stage)
    tampered = _resign(tampered)
    with pytest.raises(conductor.ConductorPolicyError):
        conductor.verify_receipt(tampered, authority)


def test_receipt_id_is_stable_but_authority_specific() -> None:
    first = _authority().as_dict()
    second = dict(first)
    second["transactionId"] = "q1tx-" + "a" * 32
    assert conductor.receipt_id(first) == conductor.receipt_id(first)
    assert conductor.receipt_id(first) != conductor.receipt_id(second)
