from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_capsule as capsule


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _write(path: Path, raw: bytes, *, mode: int = 0o600) -> capsule.FileIdentity:
    path.write_bytes(raw)
    path.chmod(mode)
    return capsule.file_identity(
        str(path),
        expected_digest=_digest(raw),
        executable=bool(mode & 0o111),
    )


def _component(version: str) -> dict[str, str]:
    return {"sha256": "sha256:" + "a" * 64, "version": version}


def _inputs(tmp_path: Path):
    base = _write(tmp_path / "base.qcow2", b"sealed-qcow2")
    helper = _write(tmp_path / "kp1_q1_guest_helper.py", b"#!/usr/bin/python3\n", mode=0o700)
    wheelhouse = _write(tmp_path / "wheelhouse-manifest.json", b'{"locked":true}\n')
    guest_value = {
        "apiVersion": capsule.API_VERSION,
        "cni": {
            "configTemplateSha256": "sha256:" + "b" * 64,
            "plugins": [
                {"name": name, "sha256": "sha256:" + f"{index:064x}", "version": "1.4.1"}
                for index, name in enumerate(capsule._CNI_NAMES, 1)
            ],
            "version": "1.0.0",
        },
        "helper": {"sha256": helper.sha256},
        "image": {"format": "qcow2", "sha256": base.sha256},
        "images": [
            {
                "archiveSha256": "sha256:" + "c" * 64,
                "manifestDigest": "sha256:" + "d" * 64,
                "name": "nats",
            },
            {
                "archiveSha256": "sha256:" + "e" * 64,
                "manifestDigest": "sha256:" + "f" * 64,
                "name": "pause",
            },
        ],
        "kernel": {
            "imageSha256": "sha256:" + "1" * 64,
            "package": "linux-image-5.15.0-q1",
            "release": "5.15.0-q1",
        },
        "kind": capsule.GUEST_MANIFEST_KIND,
        "os": {
            "id": "ubuntu",
            "releaseSha256": "sha256:" + "2" * 64,
            "version": "22.04",
        },
        "python": {"executableSha256": "sha256:" + "3" * 64, "version": "3.11.18"},
        "registry": {
            "archiveSha256": "sha256:" + "4" * 64,
            "imageDigest": "sha256:" + "5" * 64,
        },
        "runtime": {
            "containerd": _component("1.7.24"),
            "crictl": _component("1.30.0"),
            "docker": _component("27.5.1"),
            "podman": _component("5.4.0"),
            "runc": _component("1.2.4"),
        },
        "schemaVersion": capsule.SCHEMA_VERSION,
        "wheelhouse": {
            "manifestSha256": wheelhouse.sha256,
            "requirementsLockSha256": "sha256:" + "6" * 64,
        },
    }
    guest_raw = capsule.canonical_json_bytes(guest_value)
    guest = _write(tmp_path / "guest-manifest.json", guest_raw)
    wheel = _write(tmp_path / "k1s_core.whl", b"wheel")
    sdist = _write(tmp_path / "k1s_core.tar.gz", b"sdist")
    public_key = _write(tmp_path / "id_ed25519.pub", b"ssh-ed25519 AAAAQ1 fixture\n")
    qemu = _write(tmp_path / "qemu-system-x86_64", b"qemu", mode=0o700)
    qemu_img = _write(tmp_path / "qemu-img", b"qemu-img", mode=0o700)
    seed = _write(tmp_path / "genisoimage", b"genisoimage", mode=0o700)
    return {
        "base_image": base,
        "guest_manifest_identity": guest,
        "wheel": wheel,
        "sdist": sdist,
        "wheelhouse_manifest": wheelhouse,
        "helper": helper,
        "ssh_public_key": public_key,
        "qemu": qemu,
        "qemu_img": qemu_img,
        "seed_builder": seed,
    }


def _plan(tmp_path: Path, *, run_id: str = "q1-1234567890abcdef1234567890abcdef"):
    return capsule.build_plan(
        run_id=run_id,
        source_commit="1" * 40,
        source_tree="2" * 40,
        ssh_host_port=40222,
        **_inputs(tmp_path),
    )


def test_plan_is_canonical_deterministic_digest_bound_and_nonmutating(tmp_path: Path) -> None:
    first = _plan(tmp_path)
    second = _plan(tmp_path)

    assert first == second
    assert capsule.verify_plan(first) == first
    assert first["qualificationPosture"] == "source-validated/unqualified"
    assert first["mutating"] is False
    assert first["acceptanceStages"] == list(capsule.ACCEPTANCE_STAGES)
    unsigned = dict(first)
    unsigned.pop("planDigest")
    assert first["planDigest"] == _digest(capsule.canonical_json_bytes(unsigned))
    assert not (capsule.CAPSULE_PARENT / f"k1s-{first['runId']}").exists()


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("limits", "memoryMiB", 4096),
        ("network", "egress", "allowed"),
        ("artifacts", "wheel", {"path": "/private/.env"}),
    ],
)
def test_plan_verification_rejects_redigested_semantic_drift(
    tmp_path: Path,
    section: str,
    key: str,
    value: object,
) -> None:
    plan = _plan(tmp_path)
    nested = dict(plan[section])
    if section == "artifacts":
        identity = dict(nested[key])
        identity.update(value)
        nested[key] = identity
    else:
        nested[key] = value
    plan[section] = nested
    unsigned = dict(plan)
    unsigned.pop("planDigest")
    plan["planDigest"] = _digest(capsule.canonical_json_bytes(unsigned))
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.verify_plan(plan)


def test_qemu_argv_is_one_restricted_user_network_vm_without_host_mutation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    rendered = " ".join(plan["qemuArgv"])

    assert "q35,accel=kvm" in rendered
    assert "user,id=q1net,restrict=on,hostfwd=tcp:127.0.0.1:40222-:22" in rendered
    assert "-sandbox" in plan["qemuArgv"]
    assert "-daemonize" in plan["qemuArgv"]
    for forbidden in (
        "tap",
        "bridge",
        "iptables",
        "nft",
        "virtfs",
        "9p",
        "security_model",
        str(Path.home()),
        str(capsule.Path.cwd()),
    ):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    "run_id",
    [
        "q1-short",
        "Q1-1234567890abcdef1234567890abcdef",
        "q1-1234567890abcdef1234567890abcdeg",
        "../q1-1234567890abcdef1234567890abcdef",
    ],
)
def test_run_id_rejects_noncanonical_values(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(capsule.CapsulePolicyError, match="failed closed"):
        _plan(tmp_path, run_id=run_id)


def test_inputs_reject_symlink_hardlink_world_writable_and_digest_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"source")
    source.chmod(0o600)
    symlink = tmp_path / "symlink"
    symlink.symlink_to(source)
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.file_identity(str(symlink), expected_digest=_digest(b"source"))

    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.file_identity(str(source), expected_digest=_digest(b"source"))
    hardlink.unlink()

    source.chmod(0o666)
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.file_identity(str(source), expected_digest=_digest(b"source"))
    source.chmod(0o600)
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.file_identity(str(source), expected_digest="sha256:" + "0" * 64)


def test_guest_manifest_rejects_mutable_or_incomplete_identity(tmp_path: Path) -> None:
    identities = _inputs(tmp_path)
    manifest = json.loads(Path(identities["guest_manifest_identity"].path).read_text())
    manifest["images"][0]["manifestDigest"] = "nats:2.10"

    with pytest.raises(capsule.CapsulePolicyError, match="failed closed"):
        capsule.validate_guest_manifest(
            manifest,
            image_digest=identities["base_image"].sha256,
            helper_digest=identities["helper"].sha256,
            wheelhouse_manifest_digest=identities["wheelhouse_manifest"].sha256,
        )


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd, timeout, environment):
        assert Path(cwd).is_dir()
        assert timeout == capsule.PER_COMMAND_SECONDS
        assert environment == capsule._clean_environment()
        self.calls.append(tuple(argv))
        return capsule.CommandResult(0, b"", b"")


def test_run_boundary_orders_commands_and_never_uses_host_network_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "q1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    root = capsule.CAPSULE_PARENT / f"k1s-{run_id}"
    if root.exists():
        shutil.rmtree(root)
    plan = _plan(tmp_path, run_id=run_id)
    runner = _RecordingRunner()
    monkeypatch.setattr(capsule, "_validate_gate_a", lambda *_args, **_kwargs: {"valid": True})
    monkeypatch.setattr(
        capsule.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(10**12, 0, 10**12),
    )

    try:
        created = capsule.execute_plan(
            plan,
            expected_plan_digest=str(plan["planDigest"]),
            confirmation=f"{run_id}:{plan['planDigest']}",
            gate_a_path=tmp_path / "gate-a.json",
            expected_gate_a_digest="sha256:" + "9" * 64,
            runner=runner,
        )
        assert created == root
        assert [call[1] for call in runner.calls[:2]] == ["create", "-output"]
        assert "-daemonize" in runner.calls[2]
        rendered = " ".join(item for call in runner.calls for item in call)
        for forbidden in ("sudo", "ip link", "iptables", "nft", "virsh", "-netdev tap"):
            assert forbidden not in rendered
        assert sorted(item.name for item in root.iterdir()) == sorted(capsule.LAYOUT_NAMES)
        user_data = (root / "input" / "user-data").read_bytes()
        assert b"NOPASSWD" not in user_data
        assert b"sudo: false" in user_data
        assert b"q1 hard nproc 512" in user_data
        assert b"q1 hard nofile 1024" in user_data
        assert all(stat.S_IMODE(item.stat().st_mode) == 0o700 for item in root.iterdir())
    finally:
        if root.exists():
            shutil.rmtree(root)


def test_run_boundary_rejects_confirmation_collision_and_insufficient_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "q1-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    plan = _plan(tmp_path, run_id=run_id)
    monkeypatch.setattr(capsule, "_validate_gate_a", lambda *_args, **_kwargs: {"valid": True})
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.execute_plan(
            plan,
            expected_plan_digest=str(plan["planDigest"]),
            confirmation="wrong",
            gate_a_path=tmp_path / "gate-a.json",
            expected_gate_a_digest="sha256:" + "9" * 64,
            runner=_RecordingRunner(),
        )
    monkeypatch.setattr(
        capsule.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(10, 9, 1),
    )
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.execute_plan(
            plan,
            expected_plan_digest=str(plan["planDigest"]),
            confirmation=f"{run_id}:{plan['planDigest']}",
            gate_a_path=tmp_path / "gate-a.json",
            expected_gate_a_digest="sha256:" + "9" * 64,
            runner=_RecordingRunner(),
        )


def test_evidence_seal_is_deterministic_and_metadata_safe(tmp_path: Path) -> None:
    roots = [tmp_path / "a", tmp_path / "z"]
    seals = []
    for root in roots:
        evidence = root / "evidence"
        evidence.mkdir(parents=True, mode=0o700)
        (root / "cleanup").mkdir(mode=0o700)
        receipt = evidence / "001-step.json"
        receipt.write_bytes(capsule.canonical_json_bytes({"safe": True}))
        receipt.chmod(0o600)
        seals.append(
            capsule.seal_evidence(
                root,
                plan_digest="sha256:" + "1" * 64,
                source_commit="2" * 40,
                source_tree="3" * 40,
            )
        )
    assert seals[0] == seals[1]
    for relative in (
        "evidence/Q1-EVIDENCE-MANIFEST.json",
        "evidence/SHA256SUMS",
        "evidence/Q1-EVIDENCE-SEAL.json",
    ):
        assert (roots[0] / relative).read_bytes() == (roots[1] / relative).read_bytes()
    rendered = (roots[0] / "evidence/Q1-EVIDENCE-MANIFEST.json").read_text()
    assert str(tmp_path) not in rendered
    assert "credential" not in rendered.lower()


def test_teardown_requires_absent_process_and_removes_only_exact_private_trees() -> None:
    run_id = "q1-cccccccccccccccccccccccccccccccc"
    root = capsule.CAPSULE_PARENT / f"k1s-{run_id}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    for name in capsule.LAYOUT_NAMES:
        path = root / name
        path.mkdir(mode=0o700)
    (root / "vm" / "overlay.qcow2").write_bytes(b"private")
    (root / "vm" / "overlay.qcow2").chmod(0o600)
    run_receipt = capsule._bind_receipt(
        {
            "apiVersion": capsule.API_VERSION,
            "kind": "K1sQ1RunReceipt",
            "planDigest": "sha256:" + "1" * 64,
            "qualificationPosture": capsule.QUALIFICATION_POSTURE,
            "runId": run_id,
            "schemaVersion": capsule.SCHEMA_VERSION,
            "state": "launched-unqualified",
        }
    )
    run_receipt_path = root / "guest" / "run.json"
    run_receipt_path.write_bytes(capsule.canonical_json_bytes(run_receipt))
    run_receipt_path.chmod(0o600)
    run_file_digest = capsule._file_digest(run_receipt_path)
    confirmation = f"{run_id}:{run_receipt['receiptDigest']}"
    try:
        with pytest.raises(capsule.CapsulePolicyError):
            capsule.teardown_private_layout(
                root,
                expect_run_id=run_id,
                expected_run_receipt_file_digest=run_file_digest,
                confirmation=confirmation,
                process_absent=False,
            )
        cleanup_receipt = capsule.teardown_private_layout(
            root,
            expect_run_id=run_id,
            expected_run_receipt_file_digest=run_file_digest,
            confirmation=confirmation,
            process_absent=True,
        )
        capsule.verify_clean(
            root,
            expect_run_id=run_id,
            receipt_path=cleanup_receipt,
            expected_receipt_file_digest=capsule._file_digest(cleanup_receipt),
        )
        assert sorted(item.name for item in root.iterdir()) == ["cleanup", "evidence"]
    finally:
        if root.exists():
            shutil.rmtree(root)


def test_cli_help_is_static_and_does_not_create_capsule(tmp_path: Path, capsys) -> None:
    before = set(tmp_path.iterdir())
    with pytest.raises(SystemExit) as captured:
        capsule.main(["--help"])
    assert captured.value.code == 0
    assert set(tmp_path.iterdir()) == before
    assert "plan" in capsys.readouterr().out
