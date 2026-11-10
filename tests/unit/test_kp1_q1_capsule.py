from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import signal
import stat
import time
from pathlib import Path
from types import SimpleNamespace

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
    stage_runner = _write(tmp_path / "kp1_q1_guest_stages.py", b"#!/usr/bin/python3\n", mode=0o700)
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
        "guest_stage_runner": stage_runner,
        "ssh_public_key": public_key,
        "qemu": qemu,
        "qemu_img": qemu_img,
        "seed_builder": seed,
    }


def _plan(tmp_path: Path, *, run_id: str = "q1-1234567890abcdef1234567890abcdef"):
    tmp_path.chmod(0o700)
    return capsule.build_plan(
        run_id=run_id,
        capsule_parent=capsule.capsule_parent_identity(str(tmp_path)),
        source_commit="1" * 40,
        source_tree="2" * 40,
        ssh_host_port=40222,
        **_inputs(tmp_path),
    )


def _kvm(*, gid: int = 108, inode: int = 41) -> dict[str, object]:
    return {
        "device": 7,
        "gid": gid,
        "inode": inode,
        "major": 10,
        "minor": 232,
        "mode": "0660",
        "path": "/dev/kvm",
        "type": "character",
        "uid": 0,
    }


def _observation(
    *,
    checked_at: int = 1_000,
    cpu_count: int = capsule.VCPU_COUNT,
    memory_bytes: int = capsule.MIN_MEMORY_BYTES,
    free_bytes: int = capsule.MIN_FREE_START_BYTES,
    port_available: bool = True,
    gid: int = 108,
    resolved_gid: int | None = 108,
    access: bool = True,
    inode: int = 41,
) -> capsule.HostObservation:
    return capsule.HostObservation(
        checked_at_unix=checked_at,
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
        selected_filesystem_free_bytes=free_bytes,
        ssh_port_available=port_available,
        kvm=_kvm(gid=gid, inode=inode),
        expected_group_gid=resolved_gid,
        kvm_access=access,
    )


def test_plan_is_canonical_deterministic_digest_bound_and_nonmutating(tmp_path: Path) -> None:
    first = _plan(tmp_path)
    second = _plan(tmp_path)

    assert first == second
    assert capsule.verify_plan(first) == first
    assert first["qualificationPosture"] == "source-validated/unqualified"
    assert first["mutating"] is False
    assert first["acceptanceStages"] == list(capsule.ACCEPTANCE_STAGES)
    assert first["capsuleParent"]["path"] == str(tmp_path)
    assert first["layout"]["root"] == str(tmp_path / f"k1s-{first['runId']}")
    unsigned = dict(first)
    unsigned.pop("planDigest")
    assert first["planDigest"] == _digest(capsule.canonical_json_bytes(unsigned))
    assert not (tmp_path / f"k1s-{first['runId']}").exists()


def test_plan_input_revalidation_gives_only_base_image_the_image_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    original = capsule.file_identity
    observed: dict[str, int] = {}

    def recording_identity(raw_path, **kwargs):  # type: ignore[no-untyped-def]
        observed[Path(str(raw_path)).name] = kwargs.get("maximum", capsule.MAX_INPUT_BYTES)
        return original(raw_path, **kwargs)

    monkeypatch.setattr(capsule, "file_identity", recording_identity)

    capsule._revalidate_plan_inputs(plan)

    assert observed["base.qcow2"] == capsule.MAX_BASE_IMAGE_BYTES
    assert observed["guest-manifest.json"] == capsule.MAX_INPUT_BYTES
    assert observed["kp1_q1_guest_stages.py"] == capsule.MAX_INPUT_BYTES


def test_plan_payload_allows_large_base_image_but_not_large_other_inputs(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    large_base = copy.deepcopy(plan)
    large_base["artifacts"]["baseImage"]["size"] = capsule.MAX_INPUT_BYTES + 1
    large_base.pop("planDigest")
    large_base["planDigest"] = _digest(capsule.canonical_json_bytes(large_base))

    assert capsule.verify_plan(large_base) == large_base

    large_wheel = copy.deepcopy(plan)
    large_wheel["artifacts"]["wheel"]["size"] = capsule.MAX_INPUT_BYTES + 1
    large_wheel.pop("planDigest")
    large_wheel["planDigest"] = _digest(capsule.canonical_json_bytes(large_wheel))
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.verify_plan(large_wheel)


def test_parent_is_explicit_private_local_and_symlink_free(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    identity = capsule.capsule_parent_identity(str(private), filesystem_type="tmpfs")
    assert identity.path == str(private)
    assert identity.mode == "0700"
    assert identity.filesystem_type == "tmpfs"

    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.capsule_parent_identity(str(link), filesystem_type="tmpfs")

    private.chmod(0o750)
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.capsule_parent_identity(str(private), filesystem_type="tmpfs")
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.capsule_parent_identity(str(Path("/") / "tmp"), filesystem_type="tmpfs")


@pytest.mark.parametrize("identity_field", ["device", "inode"])
def test_parent_owner_and_plan_identity_drift_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_field: str,
) -> None:
    tmp_path.chmod(0o700)
    actual_uid = os.getuid()
    monkeypatch.setattr(capsule.os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.capsule_parent_identity(str(tmp_path), filesystem_type="tmpfs")
    monkeypatch.setattr(capsule.os, "getuid", lambda: actual_uid)

    plan = _plan(tmp_path)
    parent = dict(plan["capsuleParent"])
    parent[identity_field] += 1
    plan["capsuleParent"] = parent
    unsigned = dict(plan)
    unsigned.pop("planDigest")
    plan["planDigest"] = _digest(capsule.canonical_json_bytes(unsigned))
    assert capsule.verify_plan(plan) == plan
    with pytest.raises(capsule.CapsulePolicyError):
        capsule._revalidate_capsule_parent(plan)


def test_tmp_parent_is_supported_only_when_explicit_private_and_digest_bound(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    parent = capsule.capsule_parent_identity(str(tmp_path))
    assert plan["capsuleParent"] == parent.to_payload()
    assert plan["layout"]["root"] == str(tmp_path / f"k1s-{plan['runId']}")
    assert str(plan["planDigest"]).startswith("sha256:")


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
    assert "virtio-scsi-pci,id=q1scsi" in plan["qemuArgv"]
    assert "scsi-cd,drive=q1seed,bus=q1scsi.0" in plan["qemuArgv"]
    assert any("seed.iso,if=none,id=q1seed,readonly=on" in arg for arg in plan["qemuArgv"])
    assert "-sandbox" in plan["qemuArgv"]
    assert "-daemonize" not in plan["qemuArgv"]
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


def test_host_preflight_binds_capacity_tools_port_parent_and_kvm(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    receipt = capsule.build_host_preflight(plan, _observation())

    assert receipt["status"] == "pass"
    assert receipt["mutating"] is False
    assert receipt["capsuleParent"] == plan["capsuleParent"]
    assert receipt["tools"] == plan["tools"]
    assert receipt["sshPort"] == {
        "address": "127.0.0.1",
        "availableAtCheck": True,
        "port": 40222,
        "reservationClaimed": False,
    }
    assert receipt["expectedGroup"] == {"gid": 108, "name": "kvm"}
    assert capsule.verify_host_preflight(receipt, plan, now_unix=1_001) == receipt


@pytest.mark.parametrize(
    ("observation", "failure"),
    [
        (_observation(cpu_count=capsule.VCPU_COUNT - 1), "insufficient-vcpus"),
        (
            _observation(memory_bytes=capsule.MIN_MEMORY_BYTES - 1),
            "insufficient-memory",
        ),
        (
            _observation(free_bytes=capsule.MIN_FREE_START_BYTES - 1),
            "insufficient-selected-filesystem-space",
        ),
        (_observation(port_available=False), "ssh-port-unavailable"),
        (_observation(gid=42_427, resolved_gid=108), "kvm-group-mismatch"),
        (_observation(access=False), "kvm-access-denied"),
    ],
)
def test_host_preflight_reports_exact_fail_closed_reasons(
    tmp_path: Path,
    observation: capsule.HostObservation,
    failure: str,
) -> None:
    receipt = capsule.build_host_preflight(_plan(tmp_path), observation)
    assert receipt["status"] == "fail"
    assert failure in receipt["failures"]


def test_port_probe_reports_occupied_without_claiming_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OccupiedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def setsockopt(self, *_args):
            return None

        def bind(self, _address):
            raise OSError("occupied")

    monkeypatch.setattr(capsule.socket, "socket", lambda *_args: _OccupiedSocket())
    assert capsule._ssh_port_available(40222) is False


def test_gate_a_is_fresh_plan_parent_group_and_kvm_identity_bound(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    preflight = capsule.build_host_preflight(plan, _observation())
    attestation = capsule.build_gate_a_attestation(
        plan,
        preflight,
        _observation(),
        issued_at_unix=1_000,
        expires_at_unix=1_000 + capsule.GATE_A_TTL_SECONDS,
    )
    assert attestation["expectedGroupName"] == "kvm"
    assert attestation["expectedGroupGid"] == 108
    assert attestation["capsuleParent"] == plan["capsuleParent"]
    assert (
        capsule.validate_gate_a_attestation(
            attestation,
            plan,
            preflight,
            _observation(checked_at=1_001),
            now_unix=1_001,
        )
        == attestation
    )

    with pytest.raises(capsule.CapsulePolicyError):
        capsule.validate_gate_a_attestation(
            attestation,
            plan,
            preflight,
            _observation(checked_at=2_000),
            now_unix=2_000,
        )

    tampered = dict(attestation)
    tampered["expectedGroupGid"] = 42_427
    unsigned = dict(tampered)
    unsigned.pop("attestationDigest")
    tampered["attestationDigest"] = _digest(capsule.canonical_json_bytes(unsigned))
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.validate_gate_a_attestation(
            tampered,
            plan,
            preflight,
            _observation(checked_at=1_001),
            now_unix=1_001,
        )


def test_gate_a_allows_separate_access_grant_only_after_exact_reobservation(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    denied = capsule.build_host_preflight(plan, _observation(access=False))
    assert denied["failures"] == ["kvm-access-denied"]
    granted = _observation(access=True)
    attestation = capsule.build_gate_a_attestation(
        plan,
        denied,
        granted,
        issued_at_unix=1_000,
        expires_at_unix=1_000 + capsule.GATE_A_TTL_SECONDS,
    )
    assert attestation["accessStatus"] == "read-write"

    with pytest.raises(capsule.CapsulePolicyError):
        capsule.build_gate_a_attestation(
            plan,
            denied,
            _observation(access=True, inode=99),
            issued_at_unix=1_000,
            expires_at_unix=1_000 + capsule.GATE_A_TTL_SECONDS,
        )


def test_gate_a_rejects_cross_run_replay_and_symbolic_group_mismatch(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    preflight = capsule.build_host_preflight(plan, _observation())
    attestation = capsule.build_gate_a_attestation(
        plan,
        preflight,
        _observation(),
        issued_at_unix=1_000,
        expires_at_unix=1_000 + capsule.GATE_A_TTL_SECONDS,
    )
    other_root = tmp_path / "other"
    other_root.mkdir(mode=0o700)
    other = _plan(other_root, run_id="q1-ffffffffffffffffffffffffffffffff")
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.validate_gate_a_attestation(
            attestation,
            other,
            preflight,
            _observation(checked_at=1_001),
            now_unix=1_001,
        )
    mismatch = capsule.build_host_preflight(plan, _observation(gid=42_427, resolved_gid=108))
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.build_gate_a_attestation(
            plan,
            mismatch,
            _observation(gid=42_427, resolved_gid=108),
            issued_at_unix=1_000,
            expires_at_unix=1_000 + capsule.GATE_A_TTL_SECONDS,
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


class _FailingQemuLauncher(_RecordingRunner):
    def run(self, argv, *, cwd, timeout, environment):  # type: ignore[no-untyped-def]
        super().run(argv, cwd=cwd, timeout=timeout, environment=environment)
        return capsule.CommandResult(1, b"", b"launch failed")


def test_qemu_launcher_returns_after_exact_foreground_child_is_ready(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    helper = tmp_path / "fake-qemu"
    helper.write_text(
        "#!/usr/bin/python3\n"
        "import os, pathlib, sys, time\n"
        "pidfile = pathlib.Path(sys.argv[sys.argv.index('-pidfile') + 1])\n"
        "qmp = pathlib.Path(sys.argv[sys.argv.index('-qmp') + 1][5:].split(',', 1)[0])\n"
        "pidfile.write_text(str(os.getpid()), encoding='ascii')\n"
        "qmp.touch()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    pidfile = tmp_path / "qemu.pid"
    qmp = tmp_path / "qmp.sock"
    launcher = capsule.SubprocessQemuLauncher()

    result = launcher.run(
        (
            str(helper),
            "-pidfile",
            str(pidfile),
            "-qmp",
            f"unix:{qmp},server=on,wait=off",
        ),
        cwd=tmp_path,
        timeout=10,
        environment=capsule._clean_environment(),
    )

    pid = int(pidfile.read_text(encoding="ascii"))
    try:
        assert result == capsule.CommandResult(0, b"", b"")
        assert Path(f"/proc/{pid}").exists()
        assert qmp.exists()
    finally:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.01)
    assert not Path(f"/proc/{pid}").exists()


def test_failed_qemu_launch_removes_only_the_private_partial_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "q1-dddddddddddddddddddddddddddddddd"
    plan = _plan(tmp_path, run_id=run_id)
    runner = _RecordingRunner()
    launcher = _FailingQemuLauncher()
    bundle_arguments = _bundle_arguments(tmp_path, monkeypatch, plan)
    monkeypatch.setattr(capsule, "_validate_gate_a", lambda *_args, **_kwargs: {"valid": True})
    monkeypatch.setattr(
        capsule.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(10**12, 0, 10**12),
    )

    with pytest.raises(capsule.CapsulePolicyError):
        capsule.execute_plan(
            plan,
            expected_plan_digest=str(plan["planDigest"]),
            confirmation=f"{run_id}:{plan['planDigest']}",
            host_preflight_path=tmp_path / "host-preflight.json",
            expected_host_preflight_digest="sha256:" + "8" * 64,
            gate_a_path=tmp_path / "gate-a.json",
            expected_gate_a_digest="sha256:" + "9" * 64,
            runner=runner,
            qemu_launcher=launcher,
            port_checker=lambda _port: True,
            **bundle_arguments,
        )

    assert not (tmp_path / f"k1s-{run_id}").exists()
    assert len(runner.calls) == 2
    assert len(launcher.calls) == 1


def _bundle_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: dict[str, object],
) -> dict[str, object]:
    path = tmp_path / "sealed-input.bundle"
    raw = b"sealed-q1-input-bundle"
    path.write_bytes(raw)
    path.chmod(0o600)
    authority = SimpleNamespace(
        bundle_id="sha256:" + "7" * 64,
        plan_digest=plan["planDigest"],
        raw_sha256=_digest(raw),
        run_id=plan["runId"],
    )
    monkeypatch.setattr(
        capsule.q1_bundle,
        "validate_bundle_file",
        lambda *_args, **_kwargs: authority,
    )
    return {
        "input_bundle_path": path,
        "expected_input_bundle_digest": _digest(raw),
        "expected_bundle_id": authority.bundle_id,
    }


def test_run_boundary_orders_commands_and_never_uses_host_network_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "q1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    root = tmp_path / f"k1s-{run_id}"
    plan = _plan(tmp_path, run_id=run_id)
    runner = _RecordingRunner()
    bundle_arguments = _bundle_arguments(tmp_path, monkeypatch, plan)
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
            host_preflight_path=tmp_path / "host-preflight.json",
            expected_host_preflight_digest="sha256:" + "8" * 64,
            gate_a_path=tmp_path / "gate-a.json",
            expected_gate_a_digest="sha256:" + "9" * 64,
            runner=runner,
            port_checker=lambda _port: True,
            **bundle_arguments,
        )
        assert created == root
        assert [call[1] for call in runner.calls[:2]] == ["create", "-output"]
        assert "-daemonize" not in runner.calls[2]
        rendered = " ".join(item for call in runner.calls for item in call)
        for forbidden in ("sudo", "ip link", "iptables", "nft", "virsh", "-netdev tap"):
            assert forbidden not in rendered
        assert sorted(item.name for item in root.iterdir()) == sorted(capsule.LAYOUT_NAMES)
        user_data = (root / "input" / "user-data").read_bytes()
        assert b"NOPASSWD" not in user_data
        assert b"sudo: false" in user_data
        assert b"kp1_q1_guest_stages.py" in user_data
        assert b"encoding: b64" in user_data
        assert b"q1 hard nproc 512" in user_data
        assert b"q1 hard nofile 1024" in user_data
        assert (root / "input" / "network-config").read_bytes() == (
            b"version: 2\nethernets:\n  q1:\n    match:\n      name: \"en*\"\n"
            b"    dhcp4: true\n    dhcp6: false\n    optional: true\n"
        )
        assert (root / "input" / "k1s-q1-input.bundle").read_bytes() == (b"sealed-q1-input-bundle")
        assert str(root / "input" / "network-config") in runner.calls[1]
        assert str(root / "input" / "k1s-q1-input.bundle") in runner.calls[1]
        assert all(stat.S_IMODE(item.stat().st_mode) == 0o700 for item in root.iterdir())
    finally:
        if root.exists():
            shutil.rmtree(root)


def test_run_boundary_rejects_confirmation_collision_and_insufficient_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "q1-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    plan = _plan(tmp_path, run_id=run_id)
    bundle_arguments = _bundle_arguments(tmp_path, monkeypatch, plan)
    monkeypatch.setattr(capsule, "_validate_gate_a", lambda *_args, **_kwargs: {"valid": True})
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.execute_plan(
            plan,
            expected_plan_digest=str(plan["planDigest"]),
            confirmation="wrong",
            host_preflight_path=tmp_path / "host-preflight.json",
            expected_host_preflight_digest="sha256:" + "8" * 64,
            gate_a_path=tmp_path / "gate-a.json",
            expected_gate_a_digest="sha256:" + "9" * 64,
            runner=_RecordingRunner(),
            **bundle_arguments,
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
            host_preflight_path=tmp_path / "host-preflight.json",
            expected_host_preflight_digest="sha256:" + "8" * 64,
            gate_a_path=tmp_path / "gate-a.json",
            expected_gate_a_digest="sha256:" + "9" * 64,
            runner=_RecordingRunner(),
            **bundle_arguments,
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


def test_teardown_requires_absent_process_and_removes_only_exact_private_trees(
    tmp_path: Path,
) -> None:
    run_id = "q1-cccccccccccccccccccccccccccccccc"
    tmp_path.chmod(0o700)
    root = tmp_path / f"k1s-{run_id}"
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
        wrong_parent = tmp_path / "wrong"
        wrong_parent.mkdir(mode=0o700)
        with pytest.raises(capsule.CapsulePolicyError):
            capsule.teardown_private_layout(
                wrong_parent,
                expect_run_id=run_id,
                expected_run_receipt_file_digest=run_file_digest,
                confirmation=confirmation,
                process_absent=True,
            )
        with pytest.raises(capsule.CapsulePolicyError):
            capsule.teardown_private_layout(
                tmp_path,
                expect_run_id=run_id,
                expected_run_receipt_file_digest=run_file_digest,
                confirmation=confirmation,
                process_absent=False,
            )
        cleanup_receipt = capsule.teardown_private_layout(
            tmp_path,
            expect_run_id=run_id,
            expected_run_receipt_file_digest=run_file_digest,
            confirmation=confirmation,
            process_absent=True,
        )
        capsule.verify_clean(
            tmp_path,
            expect_run_id=run_id,
            receipt_path=cleanup_receipt,
            expected_receipt_file_digest=capsule._file_digest(cleanup_receipt),
        )
        assert sorted(item.name for item in root.iterdir()) == ["cleanup", "evidence"]
    finally:
        if root.exists():
            shutil.rmtree(root)


def test_teardown_and_clean_reject_run_root_device_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "q1-dddddddddddddddddddddddddddddddd"
    tmp_path.chmod(0o700)
    root = tmp_path / f"k1s-{run_id}"
    root.mkdir(mode=0o700)
    marker = root / "foreign-content"
    marker.write_bytes(b"preserve")
    marker.chmod(0o600)
    real_lstat = os.lstat
    actual = real_lstat(root)

    def drifted_lstat(path: os.PathLike[str] | str):
        observed = real_lstat(path)
        if Path(path) == root:
            return SimpleNamespace(
                st_dev=actual.st_dev + 1,
                st_ino=actual.st_ino,
                st_mode=actual.st_mode,
                st_uid=actual.st_uid,
            )
        return observed

    monkeypatch.setattr(capsule.os, "lstat", drifted_lstat)
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.teardown_private_layout(
            tmp_path,
            expect_run_id=run_id,
            expected_run_receipt_file_digest="sha256:" + "1" * 64,
            confirmation="not-authority",
            process_absent=True,
        )
    with pytest.raises(capsule.CapsulePolicyError):
        capsule.verify_clean(
            tmp_path,
            expect_run_id=run_id,
            receipt_path=root / "cleanup" / "cleanup.json",
            expected_receipt_file_digest="sha256:" + "2" * 64,
        )
    assert marker.read_bytes() == b"preserve"


def test_cli_help_is_static_and_does_not_create_capsule(tmp_path: Path, capsys) -> None:
    before = set(tmp_path.iterdir())
    with pytest.raises(SystemExit) as captured:
        capsule.main(["--help"])
    assert captured.value.code == 0
    assert set(tmp_path.iterdir()) == before
    assert "plan" in capsys.readouterr().out
