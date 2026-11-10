from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_conductor as conductor
from scripts.qualification import kp1_q1_teardown as teardown


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


def _process() -> teardown.ProcessIdentity:
    return teardown.ProcessIdentity(
        pid=4242,
        start_time_ticks=100_001,
        executable="/usr/bin/qemu-system-x86_64",
        argv=(
            "/usr/bin/qemu-system-x86_64",
            "-name",
            "k1s-q1",
            "-qmp",
            "unix:/run/private/qmp.sock,server=on,wait=off",
        ),
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
class FakeQmp:
    responses: list[teardown.QmpResponse]
    clock: FakeClock
    advance_seconds: float = 0.0
    calls: list[teardown.QmpRequest] = field(default_factory=list)

    def execute(self, request: teardown.QmpRequest) -> teardown.QmpResponse:
        self.calls.append(request)
        self.clock.advance(self.advance_seconds)
        if not self.responses:
            raise RuntimeError("private-qmp-detail")
        return self.responses.pop(0)


@dataclass
class FakeProcessObserver:
    observations: list[teardown.ProcessIdentity | None]
    calls: list[int] = field(default_factory=list)

    def observe(self, pid: int) -> teardown.ProcessIdentity | None:
        self.calls.append(pid)
        if not self.observations:
            raise RuntimeError("private-process-detail")
        return self.observations.pop(0)


def _qmp_responses(*poll_statuses: str) -> list[teardown.QmpResponse]:
    return [
        teardown.QmpResponse("query-status", "running", b"initial"),
        teardown.QmpResponse("system_powerdown", "accepted", b"powerdown"),
        *(
            teardown.QmpResponse("query-status", status, status.encode())
            for status in poll_statuses
        ),
        teardown.QmpResponse("quit", "accepted", b"quit"),
    ]


def _shutdown_harness(
    *,
    poll_statuses: tuple[str, ...] = ("shutdown",),
    process_observations: list[teardown.ProcessIdentity | None] | None = None,
):
    authority = _authority()
    process = _process()
    clock = FakeClock()
    qmp = FakeQmp(_qmp_responses(*poll_statuses), clock)
    observer = FakeProcessObserver(
        process_observations
        if process_observations is not None
        else [process] * (len(poll_statuses) + 4) + [None]
    )
    receipt = teardown.exact_qmp_shutdown(
        authority,
        process_identity=process,
        qmp=qmp,
        process_observer=observer,
        clock=clock,
    )
    return authority, process, clock, qmp, observer, receipt


@dataclass
class ConductorBundle:
    digest: str

    def sha256(self) -> str:
        return self.digest


@dataclass
class ConductorAdapter:
    def execute(self, request: conductor.StageRequest) -> conductor.StageObservation:
        return conductor.StageObservation(
            identity=dict(request.authority),
            exit_code=0,
            timed_out=False,
        )


def _conductor_receipt(authority: conductor.RunAuthority) -> dict[str, object]:
    adapter = ConductorAdapter()
    return conductor.run_conductor(
        authority,
        adapters=conductor.AdapterSet(ssh=adapter, qmp=adapter, process=adapter),
        bundle=ConductorBundle(authority.bundle_sha256),
        replay_ledger=conductor.MemoryReplayLedger(),
        clock=FakeClock(),
    )


def _inventory(authority: conductor.RunAuthority, *, when: int = 1):
    return teardown.build_inventory(
        authority,
        captured_at_unix=when,
        entries=(
            teardown.InventoryEntry("listener", "sha256:" + "a" * 64),
            teardown.InventoryEntry("process", "sha256:" + "b" * 64),
            teardown.InventoryEntry("file", "sha256:" + "c" * 64),
        ),
    )


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(teardown.canonical_json_bytes(value))
    path.chmod(0o600)


def _evidence_fixture(tmp_path: Path):
    authority, process, _clock, _qmp, _observer, shutdown = _shutdown_harness()
    before = _inventory(authority, when=10)
    after = _inventory(authority, when=20)
    comparison = teardown.compare_inventories(before, after, authority)
    capsule_parent = _private_directory(tmp_path / "capsules")
    evidence = _private_directory(tmp_path / "retained-evidence")
    zero = teardown.build_zero_residue_receipt(
        capsule_parent=capsule_parent,
        retained_evidence_root=evidence,
        authority=authority,
        shutdown_receipt=shutdown,
        inventory_before=before,
        inventory_after=after,
        checked_at_unix=30,
    )
    payloads = {
        "conductor-receipt.json": _conductor_receipt(authority),
        "inventory-after.json": after,
        "inventory-before.json": before,
        "inventory-comparison.json": comparison,
        "shutdown-receipt.json": shutdown,
        "zero-residue.json": zero,
    }
    for name, value in payloads.items():
        _write_json(evidence / name, value)
    return authority, process, capsule_parent, evidence, payloads


def _resign(value: dict[str, object], *, field: str = "receiptDigest") -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop(field, None)
    unsigned[field] = _digest(teardown.canonical_json_bytes(unsigned))
    return unsigned


def test_exact_qmp_shutdown_uses_closed_order_and_proves_exact_absence() -> None:
    authority, process, _clock, qmp, observer, receipt = _shutdown_harness(
        poll_statuses=("running", "shutdown")
    )
    assert [request.command for request in qmp.calls] == [
        "query-status",
        "system_powerdown",
        "query-status",
        "query-status",
        "quit",
    ]
    assert [request.sequence for request in qmp.calls] == [1, 2, 3, 4, 5]
    assert all(request.authority == authority.as_dict() for request in qmp.calls)
    assert all(request.process_identity_digest == process.digest() for request in qmp.calls)
    assert observer.calls == [process.pid] * 7
    assert receipt["status"] == "stopped"
    assert receipt["absenceProof"] == {"observations": 2, "status": "absent"}
    assert (
        teardown.verify_shutdown_receipt(
            receipt, authority, expected_process_identity_digest=process.digest()
        )
        == receipt
    )


def test_shutdown_receipt_is_metadata_safe() -> None:
    _authority_value, process, _clock, _qmp, _observer, receipt = _shutdown_harness()
    raw = teardown.canonical_json_bytes(receipt)
    assert str(process.pid).encode() not in raw
    assert process.executable.encode() not in raw
    assert b"qmp.sock" not in raw
    assert process.digest().encode() in raw


@pytest.mark.parametrize(
    "identity",
    [
        teardown.ProcessIdentity(True, 1, "/usr/bin/qemu", ("/usr/bin/qemu",)),
        teardown.ProcessIdentity(1, True, "/usr/bin/qemu", ("/usr/bin/qemu",)),
        teardown.ProcessIdentity(1, 1, "usr/bin/qemu", ("usr/bin/qemu",)),
        teardown.ProcessIdentity(1, 1, "/usr/bin/qemu", ("/different",)),
        teardown.ProcessIdentity(1, 1, "/usr/bin/qemu", ("/usr/bin/qemu", "bad\x00")),
    ],
)
def test_process_identity_rejects_ambiguous_values(
    identity: teardown.ProcessIdentity,
) -> None:
    with pytest.raises(teardown.TeardownPolicyError):
        identity.digest()


def test_initial_process_absence_or_identity_substitution_blocks_qmp() -> None:
    authority = _authority()
    process = _process()
    for observed in (
        None,
        teardown.ProcessIdentity(
            process.pid,
            process.start_time_ticks + 1,
            process.executable,
            process.argv,
        ),
    ):
        clock = FakeClock()
        qmp = FakeQmp(_qmp_responses("shutdown"), clock)
        observer = FakeProcessObserver([observed])
        with pytest.raises(teardown.TeardownPolicyError):
            teardown.exact_qmp_shutdown(
                authority,
                process_identity=process,
                qmp=qmp,
                process_observer=observer,
                clock=clock,
            )
        assert qmp.calls == []


def test_pid_reuse_after_qmp_is_rejected_without_signalling() -> None:
    process = _process()
    replacement = teardown.ProcessIdentity(
        process.pid,
        process.start_time_ticks + 10,
        process.executable,
        process.argv,
    )
    with pytest.raises(teardown.TeardownPolicyError):
        _shutdown_harness(process_observations=[process] * 4 + [replacement])


def test_midflight_process_identity_substitution_stops_before_next_qmp() -> None:
    authority = _authority()
    process = _process()
    replacement = teardown.ProcessIdentity(
        process.pid,
        process.start_time_ticks + 1,
        process.executable,
        process.argv,
    )
    clock = FakeClock()
    qmp = FakeQmp(_qmp_responses("shutdown"), clock)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.exact_qmp_shutdown(
            authority,
            process_identity=process,
            qmp=qmp,
            process_observer=FakeProcessObserver([process, replacement]),
            clock=clock,
        )
    assert [request.command for request in qmp.calls] == ["query-status"]


def test_still_running_process_and_qmp_poll_exhaustion_fail_closed() -> None:
    process = _process()
    with pytest.raises(teardown.TeardownPolicyError):
        _shutdown_harness(process_observations=[process] * (teardown.MAX_PROCESS_POLLS + 1))
    with pytest.raises(teardown.TeardownPolicyError):
        _shutdown_harness(poll_statuses=("running",) * teardown.MAX_QMP_POLLS)


@pytest.mark.parametrize(
    "responses",
    [
        [teardown.QmpResponse("query-status", "unknown")],
        [teardown.QmpResponse("wrong-command", "running")],
        [teardown.QmpResponse("query-status", "running", b"x" * 65_537)],
        [
            teardown.QmpResponse("query-status", "running"),
            teardown.QmpResponse("system_powerdown", "wrong"),
        ],
    ],
)
def test_qmp_malformed_status_command_or_output_fails_closed(
    responses: list[teardown.QmpResponse],
) -> None:
    authority = _authority()
    process = _process()
    clock = FakeClock()
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.exact_qmp_shutdown(
            authority,
            process_identity=process,
            qmp=FakeQmp(responses, clock),
            process_observer=FakeProcessObserver([process] * 10),
            clock=clock,
        )


def test_qmp_deadline_is_bounded() -> None:
    authority = _authority()
    process = _process()
    clock = FakeClock()
    qmp = FakeQmp(_qmp_responses("shutdown"), clock, advance_seconds=61)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.exact_qmp_shutdown(
            authority,
            process_identity=process,
            qmp=qmp,
            process_observer=FakeProcessObserver([process]),
            clock=clock,
        )


def test_shutdown_receipt_tampering_and_boolean_fields_fail_closed() -> None:
    authority, _process_value, _clock, _qmp, _observer, receipt = _shutdown_harness()
    for key, replacement in (
        ("schemaVersion", True),
        ("timeoutSeconds", True),
        ("startedAtUnix", True),
        ("completedAtUnix", True),
    ):
        tampered = _resign({**receipt, key: replacement})
        with pytest.raises(teardown.TeardownPolicyError):
            teardown.verify_shutdown_receipt(tampered, authority)
    transcript = copy.deepcopy(receipt)
    transcript["qmpTranscript"][0]["command"] = "quit"
    transcript["qmpTranscript"][0] = _resign(transcript["qmpTranscript"][0])
    transcript = _resign(transcript)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.verify_shutdown_receipt(transcript, authority)


def test_inventory_is_sorted_deduplicated_and_exactly_comparable() -> None:
    authority = _authority()
    inventory = _inventory(authority)
    assert [entry["kind"] for entry in inventory["entries"]] == [
        "file",
        "listener",
        "process",
    ]
    assert teardown.verify_inventory(inventory, authority) == inventory
    comparison = teardown.compare_inventories(inventory, inventory, authority)
    assert comparison["status"] == "equal"
    assert comparison["entryCount"] == 3


def test_inventory_drift_duplicate_unknown_kind_and_boolean_time_fail() -> None:
    authority = _authority()
    before = _inventory(authority)
    after = teardown.build_inventory(
        authority,
        captured_at_unix=2,
        entries=(teardown.InventoryEntry("file", "sha256:" + "d" * 64),),
    )
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.compare_inventories(before, after, authority)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.build_inventory(
            authority,
            captured_at_unix=1,
            entries=(
                teardown.InventoryEntry("file", "sha256:" + "a" * 64),
                teardown.InventoryEntry("file", "sha256:" + "a" * 64),
            ),
        )
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.InventoryEntry("socket-path", "sha256:" + "a" * 64).as_dict()
    tampered = _resign({**before, "capturedAtUnix": True})
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.verify_inventory(tampered, authority)
    comparison = teardown.compare_inventories(before, before, authority)
    comparison = _resign({**comparison, "entryCount": True})
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.verify_inventory_comparison(comparison, before, before, authority)


def test_zero_residue_requires_absent_exact_run_root_and_equal_inventory(
    tmp_path: Path,
) -> None:
    authority, _process_value, _clock, _qmp, _observer, shutdown = _shutdown_harness()
    before = _inventory(authority)
    capsule_parent = _private_directory(tmp_path / "capsules")
    evidence = _private_directory(tmp_path / "evidence")
    run_root = capsule_parent / f"k1s-{authority.run_id}"
    run_root.mkdir(mode=0o700)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.build_zero_residue_receipt(
            capsule_parent=capsule_parent,
            retained_evidence_root=evidence,
            authority=authority,
            shutdown_receipt=shutdown,
            inventory_before=before,
            inventory_after=before,
            checked_at_unix=1,
        )
    run_root.rmdir()
    receipt = teardown.build_zero_residue_receipt(
        capsule_parent=capsule_parent,
        retained_evidence_root=evidence,
        authority=authority,
        shutdown_receipt=shutdown,
        inventory_before=before,
        inventory_after=before,
        checked_at_unix=1,
    )
    raw = teardown.canonical_json_bytes(receipt)
    assert str(capsule_parent).encode() not in raw
    assert str(evidence).encode() not in raw
    assert receipt["status"] == "zero-residue"


def test_zero_residue_rejects_unsafe_parent_or_evidence_root(tmp_path: Path) -> None:
    authority, _process_value, _clock, _qmp, _observer, shutdown = _shutdown_harness()
    inventory = _inventory(authority)
    capsule_parent = _private_directory(tmp_path / "capsules")
    evidence = _private_directory(tmp_path / "evidence")
    evidence.chmod(0o755)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.build_zero_residue_receipt(
            capsule_parent=capsule_parent,
            retained_evidence_root=evidence,
            authority=authority,
            shutdown_receipt=shutdown,
            inventory_before=inventory,
            inventory_after=inventory,
            checked_at_unix=1,
        )
    evidence.chmod(0o700)
    alias = tmp_path / "capsules-link"
    alias.symlink_to(capsule_parent, target_is_directory=True)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.build_zero_residue_receipt(
            capsule_parent=alias,
            retained_evidence_root=evidence,
            authority=authority,
            shutdown_receipt=shutdown,
            inventory_before=inventory,
            inventory_after=inventory,
            checked_at_unix=1,
        )


def test_full_synthetic_evidence_seal_and_independent_replay(tmp_path: Path) -> None:
    authority, process, _capsule_parent, evidence, _payloads = _evidence_fixture(tmp_path)
    seal = teardown.seal_evidence(evidence, authority)
    seal_path = evidence / "Q1-EVIDENCE-SEAL.json"
    replayed = teardown.replay_evidence(
        evidence,
        authority,
        expected_seal_file_sha256=_digest(seal_path.read_bytes()),
    )
    assert replayed == seal
    assert seal["memberCount"] == len(teardown.EVIDENCE_MEMBERS)
    assert seal["authority"] == authority.as_dict()
    retained = b"".join(path.read_bytes() for path in evidence.iterdir())
    assert process.executable.encode() not in retained
    assert b"qmp.sock" not in retained


def test_seal_is_once_only_and_replay_rejects_extra_or_missing_member(
    tmp_path: Path,
) -> None:
    authority, _process_value, _capsule_parent, evidence, _payloads = _evidence_fixture(tmp_path)
    teardown.seal_evidence(evidence, authority)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.seal_evidence(evidence, authority)
    extra = evidence / "unexpected.json"
    _write_json(extra, {"unexpected": True})
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.replay_evidence(evidence, authority)
    extra.unlink()
    missing = evidence / "inventory-before.json"
    missing.unlink()
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.replay_evidence(evidence, authority)


@pytest.mark.parametrize("target", teardown.EVIDENCE_MEMBERS)
def test_seal_rejects_symlink_substitution_for_every_member(tmp_path: Path, target: str) -> None:
    authority, _process_value, _capsule_parent, evidence, _payloads = _evidence_fixture(tmp_path)
    member = evidence / target
    replacement = evidence.parent / f"replacement-{target}"
    replacement.write_bytes(member.read_bytes())
    replacement.chmod(0o600)
    member.unlink()
    member.symlink_to(replacement)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.seal_evidence(evidence, authority)


def test_seal_rejects_hardlink_mode_and_special_entry(tmp_path: Path) -> None:
    authority, _process_value, _capsule_parent, evidence, _payloads = _evidence_fixture(tmp_path)
    member = evidence / "inventory-before.json"
    hardlink = evidence.parent / "hardlink.json"
    os.link(member, hardlink)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.seal_evidence(evidence, authority)
    hardlink.unlink()
    member.chmod(0o644)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.seal_evidence(evidence, authority)
    member.chmod(0o600)
    member.unlink()
    os.mkfifo(member, mode=0o600)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.seal_evidence(evidence, authority)


def test_replay_rejects_member_manifest_sums_or_seal_tamper(tmp_path: Path) -> None:
    authority, _process_value, _capsule_parent, evidence, _payloads = _evidence_fixture(tmp_path)
    teardown.seal_evidence(evidence, authority)
    for name in (
        "inventory-after.json",
        "Q1-EVIDENCE-MANIFEST.json",
        "SHA256SUMS",
        "Q1-EVIDENCE-SEAL.json",
    ):
        original = (evidence / name).read_bytes()
        (evidence / name).write_bytes(original + b"x")
        with pytest.raises(teardown.TeardownPolicyError):
            teardown.replay_evidence(evidence, authority)
        (evidence / name).write_bytes(original)
        (evidence / name).chmod(0o600)


def test_replay_rejects_duplicate_json_keys_and_noncanonical_json(tmp_path: Path) -> None:
    authority, _process_value, _capsule_parent, evidence, _payloads = _evidence_fixture(tmp_path)
    path = evidence / "inventory-before.json"
    path.write_bytes(b'{"apiVersion":"x","apiVersion":"y"}\n')
    path.chmod(0o600)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.seal_evidence(evidence, authority)
    _write_json(path, _inventory(authority))
    raw = path.read_bytes()
    path.write_bytes(b" " + raw)
    with pytest.raises(teardown.TeardownPolicyError):
        teardown.seal_evidence(evidence, authority)


def test_cli_schema_help_seal_and_replay_are_bounded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authority, _process_value, _capsule_parent, evidence, _payloads = _evidence_fixture(tmp_path)
    authority_path = tmp_path / "authority.json"
    _write_json(authority_path, authority.as_dict())
    assert teardown.main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["qmpCommands"] == list(teardown.QMP_COMMANDS)
    assert schema["evidenceMembers"] == list(teardown.EVIDENCE_MEMBERS)
    authority_digest = _digest(authority_path.read_bytes())
    assert (
        teardown.main(
            [
                "seal",
                "--root",
                str(evidence),
                "--authority",
                str(authority_path),
                "--expect-authority-sha256",
                authority_digest,
            ]
        )
        == 0
    )
    capsys.readouterr()
    seal_path = evidence / "Q1-EVIDENCE-SEAL.json"
    assert (
        teardown.main(
            [
                "replay",
                "--root",
                str(evidence),
                "--authority",
                str(authority_path),
                "--expect-authority-sha256",
                authority_digest,
                "--expect-seal-sha256",
                _digest(seal_path.read_bytes()),
            ]
        )
        == 0
    )
    replay = json.loads(capsys.readouterr().out)
    assert replay["kind"] == teardown.SEAL_KIND


def test_cli_malformed_authority_fails_generically(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = _private_directory(tmp_path / "evidence")
    authority_path = tmp_path / "authority.json"
    authority_path.write_bytes(b'{"schemaVersion":true}\n')
    authority_path.chmod(0o600)
    with pytest.raises(teardown.TeardownPolicyError) as failure:
        teardown.main(
            [
                "seal",
                "--root",
                str(evidence),
                "--authority",
                str(authority_path),
                "--expect-authority-sha256",
                _digest(authority_path.read_bytes()),
            ]
        )
    assert str(failure.value) == "K1s Q1 teardown and replay failed closed"
    assert "authority.json" not in capsys.readouterr().err


def test_descriptor_parent_drift_and_protected_authority_path_fail_closed(
    tmp_path: Path,
) -> None:
    first = _private_directory(tmp_path / "first")
    second = _private_directory(tmp_path / "second")
    with pytest.raises(teardown.TeardownPolicyError):
        teardown._open_private_directory(first, expected=os.lstat(second))
    with pytest.raises(teardown.TeardownPolicyError):
        teardown._read_private_json(
            tmp_path / ".env",
            "sha256:" + "0" * 64,
        )


def test_schema_has_closed_limits_and_no_live_implementation() -> None:
    schema = teardown.schema_document()
    assert schema["limits"] == {
        "maximumEvidenceMemberBytes": teardown.MAX_EVIDENCE_MEMBER_BYTES,
        "maximumEvidenceTotalBytes": teardown.MAX_EVIDENCE_TOTAL_BYTES,
        "maximumInventoryItems": teardown.MAX_INVENTORY_ITEMS,
        "maximumProcessPolls": teardown.MAX_PROCESS_POLLS,
        "maximumQmpPolls": teardown.MAX_QMP_POLLS,
        "maximumQmpResponseBytes": teardown.MAX_QMP_RESPONSE_BYTES,
        "shutdownTimeoutSeconds": teardown.SHUTDOWN_TIMEOUT_SECONDS,
    }
    source = Path(teardown.__file__).read_text(encoding="utf-8")
    assert "os.kill" not in source
    assert "subprocess" not in source
    assert "import socket" not in source
    assert "import signal" not in source
