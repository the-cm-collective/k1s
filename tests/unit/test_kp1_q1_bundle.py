from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_bundle as bundle


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _source(root: Path, name: str, raw: bytes, *, mode: int = 0o644) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)
    return path


@dataclass(frozen=True)
class _Fixture:
    authority: bundle.BundleAuthority
    endpoint: dict[str, object]
    inputs: tuple[bundle.BundleInput, ...]
    manifest: dict[str, object]
    raw: bytes


def _rebind_manifest(value: dict[str, object]) -> dict[str, object]:
    manifest = copy.deepcopy(value)
    endpoint = manifest["gateB"]
    assert isinstance(endpoint, dict)
    endpoint.pop("endpointSha256", None)
    endpoint["endpointSha256"] = _digest(bundle.canonical_json_bytes(endpoint))
    manifest.pop("bundleId", None)
    manifest["bundleId"] = _digest(
        b"k1s-q1-sealed-bundle-v1\0" + bundle.canonical_json_bytes(manifest)
    )
    return manifest


def _rebind_attestation(value: dict[str, object]) -> dict[str, object]:
    attestation = copy.deepcopy(value)
    attestation.pop("attestationDigest", None)
    attestation["attestationDigest"] = _digest(
        b"k1s-q1-gate-b-v1\0" + bundle.canonical_json_bytes(attestation)
    )
    return attestation


def _fixture(root: Path) -> _Fixture:
    root.chmod(0o700)
    definitions: list[tuple[str, str, str, bytes, int]] = [
        (
            "bundle-validator",
            "payload/helpers/kp1_q1_bundle.py",
            "kp1_q1_bundle.py",
            b"# canonical bundle validator\n",
            0o644,
        ),
        (
            "guest-helper",
            "payload/helpers/kp1_q1_guest_helper.py",
            "kp1_q1_guest_helper.py",
            b"#!/usr/bin/python3.11\n# bounded helper\n",
            0o755,
        ),
        (
            "installed-validator",
            "payload/helpers/validate_installed_python_artifact.py",
            "validate_installed_python_artifact.py",
            b"#!/usr/bin/python3.11\n# installed artifact validator\n",
            0o755,
        ),
        (
            "k1s-sdist",
            "payload/k1s/k1s_core-1.0.0a1.tar.gz",
            "k1s_core-1.0.0a1.tar.gz",
            b"canonical-sdist",
            0o644,
        ),
        (
            "k1s-wheel",
            "payload/k1s/k1s_core-1.0.0a1-py3-none-any.whl",
            "k1s_core-1.0.0a1-py3-none-any.whl",
            b"canonical-wheel",
            0o644,
        ),
        (
            "requirements-lock",
            "payload/offline/requirements-release.lock",
            "requirements-release.lock",
            b"locked==1.0 --hash=sha256:fixture\n",
            0o644,
        ),
        (
            "wheelhouse-manifest",
            "payload/offline/wheelhouse-manifest.json",
            "wheelhouse-manifest.json",
            b'{"count":24}\n',
            0o644,
        ),
    ]
    for index in range(bundle.DEPENDENCY_WHEEL_COUNT):
        filename = f"dependency{index:02d}-1.0-py3-none-any.whl"
        definitions.append(
            (
                "dependency-wheel",
                f"payload/offline/wheels/{filename}",
                f"wheels/{filename}",
                f"wheel-{index:02d}".encode(),
                0o644,
            )
        )
    inputs: list[bundle.BundleInput] = []
    for role, bundle_path, source_name, raw, mode in definitions:
        source = _source(root, source_name, raw, mode=mode)
        inputs.append(
            bundle.bundle_input(
                source,
                bundle_path=bundle_path,
                role=role,
                expected_sha256=_digest(raw),
            )
        )
    validator = next(item for item in inputs if item.role == "bundle-validator")
    helper = next(item for item in inputs if item.role == "guest-helper")
    endpoint = bundle.build_gate_b_endpoint(
        bundle_validator_sha256=validator.sha256,
        helper_sha256=helper.sha256,
        python_sha256="sha256:" + "1" * 64,
        setfacl_sha256="sha256:" + "2" * 64,
        getfacl_sha256="sha256:" + "3" * 64,
    )
    manifest = bundle.build_manifest(
        run_id="q1-0123456789abcdef0123456789abcdef",
        plan_digest="sha256:" + "4" * 64,
        source_commit="5" * 40,
        source_tree="6" * 40,
        members=inputs,
        gate_b_endpoint=endpoint,
    )
    raw = bundle.build_bundle_bytes(manifest, inputs)
    authority = bundle.validate_bundle_bytes(
        raw,
        expected_run_id=str(manifest["runId"]),
        expected_plan_digest=str(manifest["planDigest"]),
        expected_bundle_id=str(manifest["bundleId"]),
    )
    return _Fixture(authority, endpoint, tuple(inputs), manifest, raw)


def test_bundle_is_canonical_deterministic_and_exact_018a1(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir(mode=0o700)
    second_root.mkdir(mode=0o700)
    first = _fixture(first_root)
    second = _fixture(second_root)

    assert first.raw == second.raw
    assert first.authority == second.authority
    assert first.manifest["distribution"] == {"name": "k1s-core", "version": "1.0.0a1"}
    assert first.manifest["offlineInstall"] == {
        "indexAccess": "denied",
        "network": "denied",
        "pipConfigFile": "/dev/null",
        "pipNoIndex": True,
        "requireHashes": True,
        "wheelCount": 24,
    }
    assert len(first.manifest["members"]) == 31
    assert first.endpoint["service"] == {
        "gid": 0,
        "mode": "single-transaction-exec",
        "name": "k1s-q1-gate-b",
        "persistent": False,
        "uid": 0,
    }
    assert first.raw == bundle._tar_bytes(
        first.manifest, {item.bundle_path: item.source_path.read_bytes() for item in first.inputs}
    )


def test_bundle_round_trips_valid_manylinux_wheel_name_beyond_ustar_limit(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    original = next(item for item in fixture.inputs if item.role == "dependency-wheel")
    filename = (
        "charset_normalizer-3.5.2-cp311-cp311-manylinux2014_x86_64."
        "manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    )
    replacement = bundle.BundleInput(
        source_path=original.source_path,
        bundle_path=f"payload/offline/wheels/{filename}",
        role=original.role,
        sha256=original.sha256,
        size=original.size,
        mode=original.mode,
        uid=original.uid,
        gid=original.gid,
        device=original.device,
        inode=original.inode,
    )
    inputs = tuple(replacement if item is original else item for item in fixture.inputs)
    manifest = bundle.build_manifest(
        run_id="q1-0123456789abcdef0123456789abcdef",
        plan_digest="sha256:" + "4" * 64,
        source_commit="5" * 40,
        source_tree="6" * 40,
        members=inputs,
        gate_b_endpoint=fixture.endpoint,
    )

    raw = bundle.build_bundle_bytes(manifest, inputs)
    authority = bundle.validate_bundle_bytes(
        raw,
        expected_run_id=str(manifest["runId"]),
        expected_plan_digest=str(manifest["planDigest"]),
        expected_bundle_id=str(manifest["bundleId"]),
    )

    assert authority.manifest == manifest
    assert raw == bundle.build_bundle_bytes(manifest, inputs)


def test_bundle_source_links_modes_and_content_drift_fail_closed(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    source = _source(tmp_path, "source.whl", b"original")
    hardlink = tmp_path / "hardlink.whl"
    os.link(source, hardlink)
    with pytest.raises(bundle.Q1BundleError):
        bundle.bundle_input(
            source,
            bundle_path="payload/offline/wheels/source-1-py3-none-any.whl",
            role="dependency-wheel",
            expected_sha256=_digest(b"original"),
        )
    hardlink.unlink()
    link = tmp_path / "link.whl"
    link.symlink_to(source)
    with pytest.raises(bundle.Q1BundleError):
        bundle.bundle_input(
            link,
            bundle_path="payload/offline/wheels/source-1-py3-none-any.whl",
            role="dependency-wheel",
            expected_sha256=_digest(b"original"),
        )
    source.chmod(0o666)
    with pytest.raises(bundle.Q1BundleError):
        bundle.bundle_input(
            source,
            bundle_path="payload/offline/wheels/source-1-py3-none-any.whl",
            role="dependency-wheel",
            expected_sha256=_digest(b"original"),
        )

    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir(mode=0o700)
    fixture = _fixture(fixture_root)
    fixture.inputs[0].source_path.write_bytes(b"drift")
    with pytest.raises(bundle.Q1BundleError):
        bundle.build_bundle_bytes(fixture.manifest, fixture.inputs)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schemaVersion", 2),
        ("sourceDateEpoch", 0),
        ("runId", 1),
        ("distribution", {"name": "k1s-runtime", "version": "1.0.0a1"}),
        ("offlineInstall", {"network": "allowed"}),
    ],
)
def test_manifest_unknown_version_and_policy_drift_fail_closed(
    tmp_path: Path, field: str, replacement: object
) -> None:
    fixture = _fixture(tmp_path)
    changed = copy.deepcopy(fixture.manifest)
    changed[field] = replacement
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_manifest(changed)
    extra = copy.deepcopy(fixture.manifest)
    extra["unsupported"] = True
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_manifest(extra)
    bad_row = copy.deepcopy(fixture.manifest)
    bad_row["members"][0]["role"] = []
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_manifest(bad_row)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schemaVersion", True),
        ("uid", False),
        ("gid", False),
        ("transactions", True),
        ("maxRequests", True),
        ("requestFd", True),
        ("shell", 0),
        ("ambientSudo", 0),
    ],
)
def test_endpoint_boolean_integer_substitution_fails_closed(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    fixture = _fixture(tmp_path)
    changed = copy.deepcopy(fixture.manifest)
    endpoint = changed["gateB"]
    assert isinstance(endpoint, dict)
    endpoint[field] = replacement
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_manifest(_rebind_manifest(changed))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("uid", False), ("gid", False), ("persistent", 0)],
)
def test_nested_service_boolean_integer_substitution_fails_closed(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    fixture = _fixture(tmp_path)
    changed = copy.deepcopy(fixture.manifest)
    endpoint = changed["gateB"]
    assert isinstance(endpoint, dict)
    service = endpoint["service"]
    assert isinstance(service, dict)
    service[field] = replacement
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_manifest(_rebind_manifest(changed))


@pytest.mark.parametrize(
    ("container", "field", "replacement"),
    [
        ("manifest", "schemaVersion", True),
        ("manifest", "sourceDateEpoch", True),
        ("offline", "wheelCount", True),
        ("offline", "pipNoIndex", 1),
        ("offline", "requireHashes", 1),
    ],
)
def test_manifest_and_offline_boolean_integer_substitution_fails_closed(
    tmp_path: Path,
    container: str,
    field: str,
    replacement: object,
) -> None:
    fixture = _fixture(tmp_path)
    changed = copy.deepcopy(fixture.manifest)
    target = changed if container == "manifest" else changed["offlineInstall"]
    assert isinstance(target, dict)
    target[field] = replacement
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_manifest(_rebind_manifest(changed))


def _malicious_archive(manifest: dict[str, object], member: tarfile.TarInfo) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        manifest_raw = bundle.canonical_json_bytes(manifest)
        first = tarfile.TarInfo("manifest.json")
        first.size = len(manifest_raw)
        first.mode = 0o644
        first.mtime = bundle.SOURCE_DATE_EPOCH
        archive.addfile(first, io.BytesIO(manifest_raw))
        archive.addfile(member, io.BytesIO(b"x") if member.isreg() else None)
    return output.getvalue()


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_archive_links_and_special_members_fail_closed(tmp_path: Path, member_type: bytes) -> None:
    fixture = _fixture(tmp_path)
    member = tarfile.TarInfo("payload/unsafe")
    member.type = member_type
    member.linkname = "payload/k1s/k1s_core-1.0.0a1.tar.gz"
    member.mtime = bundle.SOURCE_DATE_EPOCH
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_bundle_bytes(
            _malicious_archive(fixture.manifest, member),
            expected_run_id=str(fixture.manifest["runId"]),
            expected_plan_digest=str(fixture.manifest["planDigest"]),
        )


def test_archive_traversal_duplicate_digest_and_replay_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    member = tarfile.TarInfo("../escape")
    member.size = 1
    member.mode = 0o644
    member.mtime = bundle.SOURCE_DATE_EPOCH
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_bundle_bytes(
            _malicious_archive(fixture.manifest, member),
            expected_run_id=str(fixture.manifest["runId"]),
            expected_plan_digest=str(fixture.manifest["planDigest"]),
        )

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        manifest_raw = bundle.canonical_json_bytes(fixture.manifest)
        first = tarfile.TarInfo("manifest.json")
        first.size = len(manifest_raw)
        first.mode = 0o644
        first.mtime = bundle.SOURCE_DATE_EPOCH
        archive.addfile(first, io.BytesIO(manifest_raw))
        for _index in range(2):
            duplicate = tarfile.TarInfo("payload/duplicate")
            duplicate.size = 1
            duplicate.mode = 0o644
            duplicate.mtime = bundle.SOURCE_DATE_EPOCH
            archive.addfile(duplicate, io.BytesIO(b"x"))
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_bundle_bytes(
            output.getvalue(),
            expected_run_id=str(fixture.manifest["runId"]),
            expected_plan_digest=str(fixture.manifest["planDigest"]),
        )

    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_bundle_bytes(
            fixture.raw,
            expected_run_id=str(fixture.manifest["runId"]),
            expected_plan_digest=str(fixture.manifest["planDigest"]),
            consumed_bundle_ids=frozenset({fixture.authority.bundle_id}),
        )


def test_manifest_duplicate_json_keys_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    duplicated = b'{"apiVersion":"k1s.dev/q1-bundle/v1","apiVersion":"k1s.dev/q1-bundle/v1"}\n'
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        first = tarfile.TarInfo("manifest.json")
        first.size = len(duplicated)
        first.mode = 0o644
        first.mtime = bundle.SOURCE_DATE_EPOCH
        archive.addfile(first, io.BytesIO(duplicated))
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_bundle_bytes(
            output.getvalue(),
            expected_run_id=fixture.authority.run_id,
            expected_plan_digest=fixture.authority.plan_digest,
        )
    changed = bytearray(fixture.raw)
    changed[-1] = 1
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_bundle_bytes(
            bytes(changed),
            expected_run_id=str(fixture.manifest["runId"]),
            expected_plan_digest=str(fixture.manifest["planDigest"]),
        )


def test_gate_b_attestation_is_digest_expiry_and_rollback_bound(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    attestation = bundle.build_gate_b_attestation(
        fixture.authority,
        access_uid=1000,
        access_gid=108,
        transaction_id="ht1-0123456789abcdef0123456789abcdef",
        target_id="strict-cri",
        request_count=4,
        rollback_receipt_digest="sha256:" + "7" * 64,
        issued_at_unix=1_000,
        expires_at_unix=1_000 + bundle.GATE_B_TTL_SECONDS,
    )
    assert (
        bundle.validate_gate_b_attestation(
            attestation,
            fixture.authority,
            now_unix=1_001,
        )
        == attestation
    )
    for field in ("bundleId", "rollbackReceiptSha256", "targetId"):
        changed = dict(attestation)
        changed[field] = "sha256:" + "8" * 64 if field != "targetId" else "other"
        with pytest.raises(bundle.Q1BundleError):
            bundle.validate_gate_b_attestation(
                changed,
                fixture.authority,
                now_unix=1_001,
            )
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_gate_b_attestation(
            attestation,
            fixture.authority,
            now_unix=2_000,
        )
    for field in (
        "accessGid",
        "accessUid",
        "expiresAtUnix",
        "issuedAtUnix",
        "requestCount",
        "schemaVersion",
    ):
        changed = dict(attestation)
        changed[field] = True
        with pytest.raises(bundle.Q1BundleError):
            bundle.validate_gate_b_attestation(
                _rebind_attestation(changed),
                fixture.authority,
                now_unix=1_001,
            )


def test_gate_b_endpoint_context_is_exact_root_pipe_and_clean_environment() -> None:
    pipe_mode = stat.S_IFIFO | 0o600
    accepted = {
        "euid": 0,
        "egid": 0,
        "cwd": "/",
        "umask": 0o077,
        "request_fd_mode": pipe_mode,
        "response_fd_mode": pipe_mode,
        "request_fd_device": 1,
        "request_fd_inode": 10,
        "response_fd_device": 1,
        "response_fd_inode": 11,
        "request_fd": 3,
        "response_fd": 4,
        "environment": dict(bundle.ALLOWED_ENVIRONMENT),
    }
    bundle.validate_endpoint_context(**accepted)
    for key, value in (
        ("euid", 1000),
        ("cwd", "/unsafe"),
        ("umask", 0o022),
        ("request_fd", 5),
        ("request_fd_mode", stat.S_IFREG | 0o600),
        ("environment", {**bundle.ALLOWED_ENVIRONMENT, "HOME": "/root"}),
    ):
        changed = dict(accepted)
        changed[key] = value
        with pytest.raises(bundle.Q1BundleError):
            bundle.validate_endpoint_context(**changed)


def test_gate_b_endpoint_context_binds_two_distinct_real_pipe_objects() -> None:
    saved: dict[int, int | None] = {}
    for target in (3, 4):
        try:
            saved[target] = os.dup(target)
        except OSError:
            saved[target] = None
    request_read, request_write = os.pipe()
    response_read, response_write = os.pipe()
    pipe_fds = {request_read, request_write, response_read, response_write}
    try:
        os.dup2(request_read, 3)
        os.dup2(response_write, 4)
        request_info = os.fstat(3)
        response_info = os.fstat(4)
        accepted = {
            "euid": 0,
            "egid": 0,
            "cwd": "/",
            "umask": 0o077,
            "request_fd_mode": request_info.st_mode,
            "response_fd_mode": response_info.st_mode,
            "request_fd_device": request_info.st_dev,
            "request_fd_inode": request_info.st_ino,
            "response_fd_device": response_info.st_dev,
            "response_fd_inode": response_info.st_ino,
            "request_fd": 3,
            "response_fd": 4,
            "environment": dict(bundle.ALLOWED_ENVIRONMENT),
        }
        bundle.validate_endpoint_context(**accepted)

        os.dup2(3, 4)
        duplicated = os.fstat(4)
        rejected = {
            **accepted,
            "response_fd_device": duplicated.st_dev,
            "response_fd_inode": duplicated.st_ino,
            "response_fd_mode": duplicated.st_mode,
        }
        with pytest.raises(bundle.Q1BundleError):
            bundle.validate_endpoint_context(**rejected)
    finally:
        for descriptor in pipe_fds - {3, 4}:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        for target in (3, 4):
            original = saved[target]
            if original is None:
                with contextlib.suppress(OSError):
                    os.close(target)
            else:
                os.dup2(original, target)
                os.close(original)


def test_bundle_file_requires_private_single_link_and_exact_digest(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir(mode=0o700)
    fixture = _fixture(fixture_root)
    sealed = tmp_path / "input.bundle"
    sealed.write_bytes(fixture.raw)
    sealed.chmod(0o600)
    accepted = bundle.validate_bundle_file(
        sealed,
        expected_sha256=_digest(fixture.raw),
        expected_run_id=fixture.authority.run_id,
        expected_plan_digest=fixture.authority.plan_digest,
        expected_bundle_id=fixture.authority.bundle_id,
    )
    assert accepted == fixture.authority
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_bundle_file(
            sealed,
            expected_sha256="sha256:" + "0" * 64,
            expected_run_id=fixture.authority.run_id,
            expected_plan_digest=fixture.authority.plan_digest,
        )
    sealed.chmod(0o644)
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_bundle_file(
            sealed,
            expected_sha256=_digest(fixture.raw),
            expected_run_id=fixture.authority.run_id,
            expected_plan_digest=fixture.authority.plan_digest,
        )


def test_acceptance_receipt_is_single_transaction_and_identity_bound(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt = bundle.build_acceptance_receipt(
        fixture.authority,
        transaction_id="ht1-fedcba9876543210fedcba9876543210",
    )
    assert receipt["state"] == "accepted-once"
    assert receipt["bundleId"] == fixture.authority.bundle_id
    unsigned = dict(receipt)
    digest = unsigned.pop("receiptDigest")
    assert digest == _digest(bundle.canonical_json_bytes(unsigned))
    malformed = dict(receipt)
    malformed["schemaVersion"] = True
    malformed.pop("receiptDigest")
    malformed["receiptDigest"] = _digest(bundle.canonical_json_bytes(malformed))
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_acceptance_receipt(
            malformed,
            fixture.authority,
            transaction_id="ht1-fedcba9876543210fedcba9876543210",
        )
    assert (
        bundle.expected_acceptance_receipt_path(fixture.authority)
        == Path("/run/k1s-q1/private/acceptance")
        / f"{fixture.authority.bundle_id.removeprefix('sha256:')}.json"
    )
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_acceptance_receipt_output_path(
            Path("/run/k1s-q1/private/acceptance/wrong.json"),
            fixture.authority,
        )


def test_acceptance_receipt_is_created_once_without_overwrite(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir(mode=0o700)
    fixture = _fixture(fixture_root)
    receipt_root = tmp_path / "acceptance"
    receipt_root.mkdir(mode=0o700)
    transaction_id = "ht1-fedcba9876543210fedcba9876543210"
    receipt_path = receipt_root / "receipt.json"
    kwargs = {
        "transaction_id": transaction_id,
        "_expected_uid": os.getuid(),
        "_expected_gid": os.getgid(),
    }

    created = bundle.create_acceptance_receipt(
        receipt_path,
        fixture.authority,
        **kwargs,
    )
    assert created["state"] == "accepted-once"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert receipt_path.stat().st_nlink == 1
    assert (
        bundle.validate_acceptance_receipt_file(
            receipt_path,
            fixture.authority,
            **kwargs,
        )
        == created
    )
    original = receipt_path.read_bytes()
    with pytest.raises(bundle.Q1BundleError):
        bundle.create_acceptance_receipt(
            receipt_path,
            fixture.authority,
            **kwargs,
        )
    assert receipt_path.read_bytes() == original
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_acceptance_receipt_file(
            receipt_path,
            fixture.authority,
            transaction_id="ht1-" + "9" * 32,
            _expected_uid=os.getuid(),
            _expected_gid=os.getgid(),
        )


@pytest.mark.parametrize("case", ["existing", "mode", "owner", "symlink", "drift"])
def test_acceptance_receipt_unsafe_existing_state_fails_closed(
    tmp_path: Path,
    case: str,
) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir(mode=0o700)
    fixture = _fixture(fixture_root)
    receipt_root = tmp_path / "acceptance"
    receipt_root.mkdir(mode=0o700)
    receipt_path = receipt_root / "receipt.json"
    transaction_id = "ht1-" + "8" * 32
    kwargs = {
        "transaction_id": transaction_id,
        "_expected_uid": os.getuid(),
        "_expected_gid": os.getgid(),
    }
    if case == "existing":
        receipt_path.write_bytes(b"not-a-receipt")
        receipt_path.chmod(0o600)
        with pytest.raises(bundle.Q1BundleError):
            bundle.create_acceptance_receipt(receipt_path, fixture.authority, **kwargs)
        with pytest.raises(bundle.Q1BundleError):
            bundle.validate_acceptance_receipt_file(
                receipt_path,
                fixture.authority,
                **kwargs,
            )
        return
    if case == "symlink":
        target = receipt_root / "target.json"
        target.write_bytes(b"not-a-receipt")
        target.chmod(0o600)
        receipt_path.symlink_to(target)
        with pytest.raises(bundle.Q1BundleError):
            bundle.create_acceptance_receipt(receipt_path, fixture.authority, **kwargs)
        with pytest.raises(bundle.Q1BundleError):
            bundle.validate_acceptance_receipt_file(
                receipt_path,
                fixture.authority,
                **kwargs,
            )
        return

    bundle.create_acceptance_receipt(receipt_path, fixture.authority, **kwargs)
    if case == "mode":
        receipt_path.chmod(0o644)
        expected_uid = os.getuid()
    elif case == "owner":
        expected_uid = os.getuid() + 1
    else:
        receipt_path.write_bytes(b"drifted")
        expected_uid = os.getuid()
    with pytest.raises(bundle.Q1BundleError):
        bundle.validate_acceptance_receipt_file(
            receipt_path,
            fixture.authority,
            transaction_id=transaction_id,
            _expected_uid=expected_uid,
            _expected_gid=os.getgid(),
        )


def test_failed_acceptance_publication_cleans_only_its_pending_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir(mode=0o700)
    fixture = _fixture(fixture_root)
    receipt_root = tmp_path / "acceptance"
    receipt_root.mkdir(mode=0o700)
    receipt_path = receipt_root / "receipt.json"

    def fail_link(*_args, **_kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(bundle.os, "link", fail_link)
    with pytest.raises(bundle.Q1BundleError):
        bundle.create_acceptance_receipt(
            receipt_path,
            fixture.authority,
            transaction_id="ht1-" + "7" * 32,
            _expected_uid=os.getuid(),
            _expected_gid=os.getgid(),
        )
    assert not receipt_path.exists()
    assert list(receipt_root.iterdir()) == []


def test_build_and_verify_cli_use_private_absent_outputs(tmp_path: Path, capsys) -> None:
    inputs_root = tmp_path / "inputs"
    inputs_root.mkdir(mode=0o700)
    fixture = _fixture(inputs_root)
    spec = {
        "endpoint": {
            "getfaclSha256": "sha256:" + "3" * 64,
            "pythonSha256": "sha256:" + "1" * 64,
            "setfaclSha256": "sha256:" + "2" * 64,
        },
        "inputs": [
            {
                "bundlePath": item.bundle_path,
                "expectedSha256": item.sha256,
                "role": item.role,
                "sourcePath": str(item.source_path),
            }
            for item in fixture.inputs
        ],
        "planDigest": fixture.authority.plan_digest,
        "runId": fixture.authority.run_id,
        "sourceCommit": "5" * 40,
        "sourceTree": "6" * 40,
    }
    spec_path = tmp_path / "bundle-spec.json"
    spec_raw = bundle.canonical_json_bytes(spec)
    spec_path.write_bytes(spec_raw)
    spec_path.chmod(0o600)
    output = tmp_path / "candidate.bundle"

    assert (
        bundle.main(
            [
                "build",
                "--spec",
                str(spec_path),
                "--expect-spec-sha256",
                _digest(spec_raw),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    build_result = json.loads(capsys.readouterr().out)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert build_result["sha256"] == _digest(output.read_bytes())
    assert (
        bundle.main(
            [
                "verify",
                "--bundle",
                str(output),
                "--expect-bundle-sha256",
                build_result["sha256"],
                "--run-id",
                fixture.authority.run_id,
                "--plan-digest",
                fixture.authority.plan_digest,
                "--bundle-id",
                build_result["bundleId"],
            ]
        )
        == 0
    )
    verify_result = json.loads(capsys.readouterr().out)
    assert verify_result["bundleId"] == build_result["bundleId"]
    assert (
        bundle.main(
            [
                "build",
                "--spec",
                str(spec_path),
                "--expect-spec-sha256",
                _digest(spec_raw),
                "--output",
                str(output),
            ]
        )
        == 2
    )

    malformed = dict(spec)
    malformed["runId"] = True
    malformed_raw = bundle.canonical_json_bytes(malformed)
    malformed_path = tmp_path / "malformed-spec.json"
    malformed_path.write_bytes(malformed_raw)
    malformed_path.chmod(0o600)
    assert (
        bundle.main(
            [
                "build",
                "--spec",
                str(malformed_path),
                "--expect-spec-sha256",
                _digest(malformed_raw),
                "--output",
                str(tmp_path / "malformed.bundle"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err == (
        "K1s Q1 sealed input bundle failed closed\nK1s Q1 sealed input bundle failed closed\n"
    )
    assert "Traceback" not in captured.err
