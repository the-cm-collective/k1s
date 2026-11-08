from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_guest_helper as guest_helper

from ae.kctl.profile_host_plan import StrictCRIActionMaterial, canonical_json_bytes


@pytest.fixture(autouse=True)
def _sandbox_system_temp_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model the guest's root-owned /tmp in this sandbox's remapped /tmp."""

    monkeypatch.setattr(
        guest_helper,
        "SYSTEM_TEMP_OWNER_UID",
        os.lstat(guest_helper.SYSTEM_TEMP).st_uid,
    )


def _digest_value(value: object) -> str:
    return guest_helper._digest_bytes(canonical_json_bytes(value))


def _root(tmp_path: Path, transaction_id: str) -> tuple[Path, int]:
    root = tmp_path / "transaction-root"
    backup = root / "transactions" / transaction_id / "backups"
    backup.mkdir(parents=True, mode=0o700)
    for path in (root, root / "transactions", backup.parent, backup):
        path.chmod(0o700)
    return root, os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _absent(path: Path) -> dict[str, object]:
    parent = path.parent.stat()
    return {
        "parent": {
            "device": parent.st_dev,
            "gid": parent.st_gid,
            "inode": parent.st_ino,
            "mode": f"0{stat.S_IMODE(parent.st_mode):03o}",
            "nlink": parent.st_nlink,
            "path": str(path.parent),
            "type": "directory",
            "uid": parent.st_uid,
        },
        "path": str(path),
        "state": "absent",
    }


def _present(path: Path) -> dict[str, object]:
    return guest_helper.observe_exact(path, expected_uid=os.getuid())


def _synthetic_socket_fact(path: Path) -> dict[str, object]:
    path.write_bytes(b"synthetic-socket-identity")
    path.chmod(0o600)
    value = _present(path)
    return {**value, "sha256": None, "size": 0, "type": "socket"}


class _AclRunner:
    def __init__(self) -> None:
        self.acl = ("user::rw-", "group::rw-", "mask::rw-", "other::---")
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, input_bytes=None):
        assert input_bytes is None
        call = tuple(argv)
        self.calls.append(call)
        if "getfacl" in Path(argv[0]).name:
            return guest_helper.ToolResult(0, ("\n".join(self.acl) + "\n").encode(), b"")
        if argv[1] == "-m":
            uid = argv[2].split(":", 2)[1]
            self.acl = (
                "user::rw-",
                f"user:{uid}:rw-",
                "group::rw-",
                "mask::rw-",
                "other::---",
            )
            return guest_helper.ToolResult(0, b"", b"")
        if argv[1].startswith("--set="):
            self.acl = tuple(argv[1].removeprefix("--set=").split(","))
            return guest_helper.ToolResult(0, b"", b"")
        raise AssertionError(call)


def _identity(name: str) -> guest_helper.ExecutableIdentity:
    return guest_helper.ExecutableIdentity(
        path=Path(f"/usr/bin/{name}"),
        sha256="sha256:" + "a" * 64,
        device=1,
        inode=1,
    )


def _boundary(runner: _AclRunner | None = None) -> guest_helper.QualificationHostActionBoundary:
    return guest_helper.QualificationHostActionBoundary(
        access_uid=os.getuid(),
        access_gid=os.getgid(),
        setfacl=_identity("setfacl"),
        getfacl=_identity("getfacl"),
        runner=runner or _AclRunner(),
        effective_uid=os.geteuid(),
        effective_gid=os.getegid(),
    )


def _write_material(path: Path, content: bytes) -> StrictCRIActionMaterial:
    before = _absent(path)
    projection = {
        "gid": os.getegid(),
        "mode": "0644",
        "path": str(path),
        "sha256": guest_helper._digest_bytes(content),
        "type": "regular",
        "uid": os.geteuid(),
    }
    return StrictCRIActionMaterial(
        index=0,
        action_id="cni-config-10-k1s.conflist",
        kind="write-canonical",
        target=str(path),
        expected_before=before,
        expected_before_digest=_digest_value(before),
        desired_after_digest=_digest_value(projection),
        rollback="remove-created",
        content=content,
    )


def test_clean_environment_is_exact_and_rejects_ambient_values() -> None:
    guest_helper.validate_clean_environment(dict(guest_helper.ALLOWED_ENVIRONMENT))
    with pytest.raises(guest_helper.QualificationHelperError, match="failed closed"):
        guest_helper.validate_clean_environment(
            {**guest_helper.ALLOWED_ENVIRONMENT, "SUDO_COMMAND": "private"}
        )
    with pytest.raises(guest_helper.QualificationHelperError, match="failed closed"):
        guest_helper.validate_clean_environment(
            {**guest_helper.ALLOWED_ENVIRONMENT, "PATH": "/unsafe:/usr/bin"}
        )


def test_snapshot_apply_replay_and_receipt_bound_rollback(tmp_path: Path) -> None:
    transaction_id = "ht1-" + "1" * 32
    root, root_fd = _root(tmp_path, transaction_id)
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    target = target_root / "10-k1s.conflist"
    material = _write_material(target, b'{"cniVersion":"1.0.0"}\n')
    boundary = _boundary()
    try:
        assert (
            boundary.execute(
                operation="snapshot",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
            == material.expected_before_digest
        )
        assert (
            boundary.execute(
                operation="apply",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
            == material.desired_after_digest
        )
        assert target.read_bytes() == material.content
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        # Exact replay performs no divergent rewrite.
        identity = (target.stat().st_ino, target.stat().st_mtime_ns)
        assert (
            boundary.execute(
                operation="apply",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
            == material.desired_after_digest
        )
        assert (target.stat().st_ino, target.stat().st_mtime_ns) == identity
        with pytest.raises(guest_helper.QualificationHelperError):
            boundary.execute(
                operation="rollback",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
        assert (
            boundary.execute(
                operation="rollback",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest="sha256:" + "9" * 64,
            )
            == material.expected_before_digest
        )
        assert not target.exists()
        assert (root / "transactions" / transaction_id / "backups").is_dir()
    finally:
        os.close(root_fd)


def test_rollback_rejects_post_apply_file_drift_without_mutation(tmp_path: Path) -> None:
    transaction_id = "ht1-" + "6" * 32
    _root_path, root_fd = _root(tmp_path, transaction_id)
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    target = target_root / "10-k1s.conflist"
    material = _write_material(target, b"approved\n")
    boundary = _boundary()
    try:
        boundary.execute(
            operation="snapshot",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        boundary.execute(
            operation="apply",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        target.write_bytes(b"foreign-after-apply\n")
        drifted = _present(target)

        with pytest.raises(guest_helper.QualificationHelperError, match="failed closed"):
            boundary.execute(
                operation="rollback",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest="sha256:" + "7" * 64,
            )

        assert target.read_bytes() == b"foreign-after-apply\n"
        assert _present(target) == drifted
    finally:
        os.close(root_fd)


def test_crash_after_snapshot_recovers_from_exact_private_backup(tmp_path: Path) -> None:
    transaction_id = "ht1-" + "2" * 32
    _root_path, root_fd = _root(tmp_path, transaction_id)
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    target = target_root / "99-loopback.conf"
    material = _write_material(target, b'{"type":"loopback"}\n')
    try:
        first = _boundary()
        first.execute(
            operation="snapshot",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        # A new boundary instance represents a resumed helper after the crash.
        resumed = _boundary()
        assert (
            resumed.execute(
                operation="apply",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
            == material.desired_after_digest
        )
    finally:
        os.close(root_fd)


def test_apply_rejects_target_drift_and_temporary_collision(tmp_path: Path) -> None:
    transaction_id = "ht1-" + "3" * 32
    _root_path, root_fd = _root(tmp_path, transaction_id)
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    target = target_root / "10-k1s.conflist"
    material = _write_material(target, b"expected\n")
    boundary = _boundary()
    try:
        boundary.execute(
            operation="snapshot",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        target.write_bytes(b"foreign\n")
        target.chmod(0o600)
        with pytest.raises(guest_helper.QualificationHelperError, match="failed closed"):
            boundary.execute(
                operation="apply",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
        target.unlink()
        collision = target.parent / f".{target.name}.k1s-q1-{material.action_id}"
        collision.write_bytes(b"foreign")
        with pytest.raises(guest_helper.QualificationHelperError, match="failed closed"):
            boundary.execute(
                operation="apply",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
    finally:
        os.close(root_fd)


def test_target_symlink_and_hardlink_are_rejected(tmp_path: Path) -> None:
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    real = target_root / "real"
    real.write_bytes(b"value")
    real.chmod(0o600)
    link = target_root / "link"
    link.symlink_to(real)
    with pytest.raises(guest_helper.QualificationHelperError):
        guest_helper.observe_exact(link, expected_uid=os.getuid())

    hardlink = target_root / "hardlink"
    os.link(real, hardlink)
    observed = _present(real)
    assert observed["nlink"] == 2
    expected = dict(observed)
    expected["nlink"] = 1
    assert _digest_value(observed) != _digest_value(expected)


def test_acl_action_binds_uid_gid_and_restores_private_prestate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "ht1-" + "4" * 32
    _root_path, root_fd = _root(tmp_path, transaction_id)
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    socket_path = target_root / "containerd.sock"
    before = _synthetic_socket_fact(socket_path)
    monkeypatch.setattr(guest_helper, "observe_exact", lambda *_args, **_kwargs: before)
    desired = _digest_value(
        {
            "gid": os.getgid(),
            "path": str(socket_path),
            "permissions": "rw",
            "uid": os.getuid(),
        }
    )
    material = StrictCRIActionMaterial(
        index=9,
        action_id="cri-socket-access",
        kind="grant-exact-acl",
        target=str(socket_path),
        expected_before=before,
        expected_before_digest=_digest_value(before),
        desired_after_digest=desired,
        rollback="restore-exact-before",
    )
    runner = _AclRunner()
    boundary = _boundary(runner)
    try:
        boundary.execute(
            operation="snapshot",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        boundary.execute(
            operation="apply",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        assert f"user:{os.getuid()}:rw-" in runner.acl
        boundary.execute(
            operation="rollback",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest="sha256:" + "8" * 64,
        )
        assert runner.acl == ("user::rw-", "group::rw-", "mask::rw-", "other::---")
        call_count = len(runner.calls)
        assert (
            boundary.execute(
                operation="rollback",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest="sha256:" + "8" * 64,
            )
            == material.expected_before_digest
        )
        assert len(runner.calls) == call_count + 1
    finally:
        os.close(root_fd)


def test_acl_replay_and_rollback_reject_replaced_socket_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "ht1-" + "7" * 32
    _root_path, root_fd = _root(tmp_path, transaction_id)
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    socket_path = target_root / "containerd.sock"
    before = _synthetic_socket_fact(socket_path)
    observed = [before]
    monkeypatch.setattr(
        guest_helper,
        "observe_exact",
        lambda *_args, **_kwargs: observed[0],
    )
    desired = _digest_value(
        {
            "gid": os.getgid(),
            "path": str(socket_path),
            "permissions": "rw",
            "uid": os.getuid(),
        }
    )
    material = StrictCRIActionMaterial(
        index=9,
        action_id="cri-socket-access",
        kind="grant-exact-acl",
        target=str(socket_path),
        expected_before=before,
        expected_before_digest=_digest_value(before),
        desired_after_digest=desired,
        rollback="restore-exact-before",
    )
    runner = _AclRunner()
    boundary = _boundary(runner)
    try:
        boundary.execute(
            operation="snapshot",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        boundary.execute(
            operation="apply",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        desired_acl = runner.acl
        observed[0] = {**before, "inode": int(before["inode"]) + 1}

        with pytest.raises(guest_helper.QualificationHelperError, match="failed closed"):
            boundary.execute(
                operation="apply",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
        with pytest.raises(guest_helper.QualificationHelperError, match="failed closed"):
            boundary.execute(
                operation="rollback",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest="sha256:" + "6" * 64,
            )

        assert runner.acl == desired_acl
    finally:
        os.close(root_fd)


def test_acl_action_rejects_unapproved_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    socket_path = target_root / "containerd.sock"
    before = _synthetic_socket_fact(socket_path)
    monkeypatch.setattr(guest_helper, "observe_exact", lambda *_args, **_kwargs: before)
    material = StrictCRIActionMaterial(
        index=0,
        action_id="cri-socket-access",
        kind="grant-exact-acl",
        target=str(socket_path),
        expected_before=before,
        expected_before_digest=_digest_value(before),
        desired_after_digest="sha256:" + "0" * 64,
        rollback="restore-exact-before",
    )
    transaction_id = "ht1-" + "5" * 32
    _root_path, root_fd = _root(tmp_path, transaction_id)
    boundary = _boundary()
    try:
        boundary.execute(
            operation="snapshot",
            transaction_id=transaction_id,
            transaction_root_fd=root_fd,
            material=material,
            rollback_authorization_digest=None,
        )
        with pytest.raises(guest_helper.QualificationHelperError):
            boundary.execute(
                operation="apply",
                transaction_id=transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=None,
            )
    finally:
        os.close(root_fd)


def test_public_errors_and_repr_do_not_disclose_private_paths(tmp_path: Path) -> None:
    private = tmp_path / "credential-token"
    try:
        guest_helper.executable_identity(private, "sha256:" + "0" * 64)
    except guest_helper.QualificationHelperError as exc:
        rendered = str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("unsafe path unexpectedly accepted")
    assert str(private) not in rendered
    assert "credential-token" not in rendered


def test_helper_script_identity_is_digest_bound_without_requiring_exec_mode(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_bytes(b"print('bounded')\n")
    helper.chmod(0o600)
    digest = guest_helper._digest_bytes(helper.read_bytes())
    accepted = guest_helper.executable_identity(
        helper,
        digest,
        require_executable=False,
    )
    assert accepted.sha256 == digest
    with pytest.raises(guest_helper.QualificationHelperError):
        guest_helper.executable_identity(helper, digest)


def test_main_rejects_dirty_environment_before_opening_fds(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        guest_helper.os,
        "environ",
        {**guest_helper.ALLOWED_ENVIRONMENT, "HOME": "/private"},
    )
    result = guest_helper.main(
        [
            "--request-fd",
            "3",
            "--response-fd",
            "4",
            "--gate-b-attestation",
            "/private/gate.json",
            "--expect-gate-b-sha256",
            "sha256:" + "0" * 64,
            "--setfacl",
            "/usr/bin/setfacl",
            "--getfacl",
            "/usr/bin/getfacl",
        ]
    )
    assert result == 2
    error = capsys.readouterr().err
    assert error == "K1s Q1 guest helper failed closed\n"
    assert "/private" not in error
