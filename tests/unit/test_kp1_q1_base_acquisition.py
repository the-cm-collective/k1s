from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_base_acquisition as acquisition


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _write(path: Path, raw: bytes, mode: int) -> dict[str, str]:
    path.write_bytes(raw)
    path.chmod(mode)
    return {"expectedSha256": _digest(raw), "path": str(path)}


def _fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], Path, dict[str, bytes], dict[str, str]]:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700, parents=True)
    parent.chmod(0o700)
    image = b"synthetic-qcow2-image"
    package_manifest = b"linux-image-generic 5.15.0\n"
    sums = (
        f"{_digest(image).removeprefix('sha256:')} *{acquisition.IMAGE_FILENAME}\n"
        f"{_digest(package_manifest).removeprefix('sha256:')} *{acquisition.MANIFEST_FILENAME}\n"
    ).encode()
    downloads = {
        "SHA256SUMS": sums,
        "SHA256SUMS.gpg": b"synthetic-signature",
        acquisition.MANIFEST_FILENAME: package_manifest,
        acquisition.IMAGE_FILENAME: image,
    }
    keyring_raw = b"synthetic-keyring"
    keyring = parent / "ubuntu-cloudimage-keyring.gpg"
    keyring.write_bytes(keyring_raw)
    keyring.chmod(0o644)
    serial = "20261106"
    release = f"https://cloud-images.ubuntu.com/releases/jammy/release-{serial}"
    fingerprint = "D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81"
    transaction = {
        "canonicalBaseCandidate": {
            "architecture": "amd64",
            "filename": acquisition.IMAGE_FILENAME,
            "format": "qcow2",
            "headObservedAt": "2026-11-09T15:02:03Z",
            "headStatus": 200,
            "imageContentLength": len(image),
            "imageSha256": _digest(image).removeprefix("sha256:"),
            "imageUrl": f"{release}/{acquisition.IMAGE_FILENAME}",
            "immutableDirectory": f"{release}/",
            "packageManifestSha256": _digest(package_manifest).removeprefix("sha256:"),
            "packageManifestUrl": f"{release}/{acquisition.MANIFEST_FILENAME}",
            "serial": serial,
            "sha256sumsUrl": f"{release}/SHA256SUMS",
            "signatureUrl": f"{release}/SHA256SUMS.gpg",
            "status": "provisional-until-signed-checksum-set-is-acquired-and-verified",
        },
        "canonicalSigner": {
            "fingerprint": fingerprint,
            "keyring": str(keyring),
            "keyringSha256": _digest(keyring_raw).removeprefix("sha256:"),
            "locallyPresentFingerprints": [fingerprint],
        },
        "schema": acquisition.TRANSACTION_SCHEMA,
    }
    transaction_path = parent / "transaction.json"
    transaction_raw = json.dumps(transaction, separators=(",", ":"), sort_keys=True).encode()
    transaction_path.write_bytes(transaction_raw)
    transaction_path.chmod(0o600)
    tool_paths: dict[str, str] = {}
    tools: dict[str, dict[str, str]] = {}
    for name in acquisition.TOOL_NAMES:
        path = parent / name
        tools[name] = _write(path, f"{name}-tool".encode(), 0o700)
        tool_paths[name] = str(path)
    spec: dict[str, object] = {
        "apiVersion": acquisition.API_VERSION,
        "kind": acquisition.SPEC_KIND,
        "outputName": "qh5b-b1-1.0.0a3-11111111-20261109T162500Z",
        "outputParent": str(parent),
        "schemaVersion": acquisition.SCHEMA_VERSION,
        "source": {"commit": "1" * 40, "tree": "2" * 40, "version": "1.0.0a3"},
        "tools": tools,
        "transaction": {
            "expectedSha256": _digest(transaction_raw),
            "path": str(transaction_path),
        },
    }
    return spec, parent, downloads, tool_paths


class _Clock:
    def __init__(self) -> None:
        self.value = 1_800_000_000

    def time(self) -> int:
        self.value += 1
        return self.value


class _Adapter:
    def __init__(
        self,
        downloads: dict[str, bytes],
        *,
        fingerprint: str = "D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81",
        backing: str | None = None,
        fail: str | None = None,
    ) -> None:
        self.downloads = downloads
        self.fingerprint = fingerprint
        self.backing = backing
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> acquisition.CommandObservation:
        self.calls.append(argv)
        assert Path(cwd).is_dir()
        assert timeout_seconds > 0
        assert maximum_output_bytes == acquisition.MAX_COMMAND_OUTPUT_BYTES
        if self.fail == "command" and len(self.calls) == 1:
            return acquisition.CommandObservation(argv, 1, False, stderr=b"synthetic")
        executable = Path(argv[0]).name
        if executable == "curl":
            destination = Path(argv[argv.index("--output") + 1])
            filename = argv[-1].rsplit("/", 1)[-1]
            destination.write_bytes(self.downloads[filename])
            return acquisition.CommandObservation(argv, 0, False)
        if executable == "gpgv":
            status = f"[GNUPG:] VALIDSIG {self.fingerprint} 0 0 0 0 0 0 0 0 0\n".encode()
            return acquisition.CommandObservation(argv, 0, False, stdout=status)
        if executable == "qemuImg":
            payload: dict[str, object] = {
                "actual-size": len(self.downloads[acquisition.IMAGE_FILENAME]),
                "filename": argv[-1],
                "format": "qcow2",
                "virtual-size": 2 * 1024 * 1024 * 1024,
            }
            if self.backing is not None:
                payload["backing-filename"] = self.backing
            return acquisition.CommandObservation(
                argv, 0, False, stdout=json.dumps(payload).encode()
            )
        raise AssertionError(argv)


def _plan(
    tmp_path: Path,
) -> tuple[dict[str, object], Path, dict[str, bytes]]:
    spec, parent, downloads, tool_paths = _fixture(tmp_path)
    plan = acquisition.build_plan(
        spec,
        allowed_output_parent=parent,
        expected_tool_paths=tool_paths,
    )
    return plan, parent, downloads


def test_plan_binds_transaction_tools_candidate_and_private_root(tmp_path: Path) -> None:
    plan, parent, _ = _plan(tmp_path)

    assert acquisition.verify_plan(plan, allowed_output_parent=parent) == plan
    assert plan["qualificationPosture"] == acquisition.QUALIFICATION_POSTURE
    assert [row["filename"] for row in plan["downloads"]] == list(acquisition.DOWNLOAD_ORDER)
    assert not Path(plan["outputRoot"]).exists()
    assert plan["candidate"]["serial"] == "20261106"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda spec: spec.update(extra=True),
        lambda spec: spec.update(outputName="qh5b-b1-1.0.0a3-22222222-20261109T162500Z"),
        lambda spec: spec["source"].update(version="1.0.0"),
        lambda spec: spec["transaction"].update(expectedSha256=_digest(b"wrong")),
    ],
)
def test_plan_rejects_authority_drift(tmp_path: Path, mutation: object) -> None:
    spec, parent, _, tool_paths = _fixture(tmp_path)
    mutation(spec)
    with pytest.raises(acquisition.BaseAcquisitionPolicyError, match="failed closed"):
        acquisition.build_plan(
            spec,
            allowed_output_parent=parent,
            expected_tool_paths=tool_paths,
        )


def test_plan_rejects_mutable_url_and_existing_root(tmp_path: Path) -> None:
    spec, parent, _, tool_paths = _fixture(tmp_path)
    transaction_path = Path(spec["transaction"]["path"])
    transaction = json.loads(transaction_path.read_text())
    transaction["canonicalBaseCandidate"]["imageUrl"] = (
        f"https://cloud-images.ubuntu.com/releases/jammy/release/{acquisition.IMAGE_FILENAME}"
    )
    raw = json.dumps(transaction, separators=(",", ":"), sort_keys=True).encode()
    transaction_path.write_bytes(raw)
    spec["transaction"]["expectedSha256"] = _digest(raw)
    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition.build_plan(
            spec,
            allowed_output_parent=parent,
            expected_tool_paths=tool_paths,
        )

    spec, parent, _, tool_paths = _fixture(tmp_path / "second")
    (parent / spec["outputName"]).mkdir()
    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition.build_plan(
            spec,
            allowed_output_parent=parent,
            expected_tool_paths=tool_paths,
        )


def test_acquisition_authenticates_downloads_inspects_image_and_seals(tmp_path: Path) -> None:
    plan, parent, downloads = _plan(tmp_path)
    adapter = _Adapter(downloads)
    result = acquisition.run_acquisition(
        plan,
        adapter=adapter,
        clock=_Clock(),
        allowed_output_parent=parent,
    )
    root = Path(plan["outputRoot"])

    assert root.stat().st_mode & 0o777 == 0o700
    assert sorted(path.name for path in root.iterdir()) == sorted(
        [
            *acquisition.DOWNLOAD_ORDER,
            "B1-ACQUISITION-RECEIPT.json",
            "B1-EVIDENCE-SEAL.json",
            "B1-LOCAL-SHA256SUMS",
        ]
    )
    assert all(path.stat().st_mode & 0o777 == 0o400 for path in root.iterdir())
    receipt = result["receipt"]
    assert (
        acquisition.verify_receipt(
            receipt,
            expected_plan_digest=plan["planDigest"],
        )
        == receipt
    )
    assert receipt["image"] == {
        "actualSize": len(downloads[acquisition.IMAGE_FILENAME]),
        "format": "qcow2",
        "hasBackingFile": False,
        "virtualSize": 2 * 1024 * 1024 * 1024,
    }
    assert len(adapter.calls) == 6
    seal_path = root / "B1-EVIDENCE-SEAL.json"
    replay = acquisition.verify_acquisition_root(
        plan,
        expected_seal_file_digest=_digest(seal_path.read_bytes()),
        allowed_output_parent=parent,
    )
    assert replay["receipt"] == receipt
    assert replay["seal"] == result["seal"]


@pytest.mark.parametrize(
    ("fingerprint", "backing", "fail"),
    [
        ("A" * 40, None, None),
        ("D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81", "base.qcow2", None),
        ("D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81", None, "command"),
    ],
)
def test_acquisition_fails_closed_on_signature_image_or_command_failure(
    tmp_path: Path,
    fingerprint: str,
    backing: str | None,
    fail: str | None,
) -> None:
    plan, parent, downloads = _plan(tmp_path)
    adapter = _Adapter(downloads, fingerprint=fingerprint, backing=backing, fail=fail)
    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition.run_acquisition(
            plan,
            adapter=adapter,
            clock=_Clock(),
            allowed_output_parent=parent,
        )
    failure = Path(plan["failurePath"])
    assert failure.is_file()
    assert json.loads(failure.read_text())["status"] == "failed-closed"
    assert not Path(plan["outputRoot"]).exists()


def test_failed_root_cleanup_refuses_unexpected_members(tmp_path: Path) -> None:
    plan, _, _ = _plan(tmp_path)
    root = Path(plan["outputRoot"])
    root.mkdir(mode=0o700)
    unexpected = root / "foreign"
    unexpected.write_bytes(b"do-not-delete")
    unexpected.chmod(0o600)

    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition._remove_failed_root(plan)

    assert unexpected.read_bytes() == b"do-not-delete"


def test_acquisition_rejects_signed_checksum_mismatch(tmp_path: Path) -> None:
    plan, parent, downloads = _plan(tmp_path)
    downloads["SHA256SUMS"] = (
        f"{'0' * 64} *{acquisition.IMAGE_FILENAME}\n"
        f"{_digest(downloads[acquisition.MANIFEST_FILENAME]).removeprefix('sha256:')} "
        f"*{acquisition.MANIFEST_FILENAME}\n"
    ).encode()
    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition.run_acquisition(
            plan,
            adapter=_Adapter(downloads),
            clock=_Clock(),
            allowed_output_parent=parent,
        )


def test_receipt_tamper_and_wrong_plan_fail_closed(tmp_path: Path) -> None:
    plan, parent, downloads = _plan(tmp_path)
    result = acquisition.run_acquisition(
        plan,
        adapter=_Adapter(downloads),
        clock=_Clock(),
        allowed_output_parent=parent,
    )
    receipt = result["receipt"]
    tampered = copy.deepcopy(receipt)
    tampered["image"]["hasBackingFile"] = True
    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition.verify_receipt(tampered, expected_plan_digest=plan["planDigest"])
    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition.verify_receipt(receipt, expected_plan_digest=_digest(b"other"))


def test_root_replay_rejects_member_tamper_and_unexpected_member(tmp_path: Path) -> None:
    plan, parent, downloads = _plan(tmp_path)
    acquisition.run_acquisition(
        plan,
        adapter=_Adapter(downloads),
        clock=_Clock(),
        allowed_output_parent=parent,
    )
    root = Path(plan["outputRoot"])
    seal_digest = _digest((root / "B1-EVIDENCE-SEAL.json").read_bytes())
    manifest = root / acquisition.MANIFEST_FILENAME
    manifest.chmod(0o600)
    manifest.write_bytes(b"tampered")
    manifest.chmod(0o400)
    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition.verify_acquisition_root(
            plan,
            expected_seal_file_digest=seal_digest,
            allowed_output_parent=parent,
        )

    plan, parent, downloads = _plan(tmp_path / "extra")
    acquisition.run_acquisition(
        plan,
        adapter=_Adapter(downloads),
        clock=_Clock(),
        allowed_output_parent=parent,
    )
    root = Path(plan["outputRoot"])
    extra = root / "unexpected"
    extra.write_text("unexpected")
    extra.chmod(0o400)
    with pytest.raises(acquisition.BaseAcquisitionPolicyError):
        acquisition.verify_acquisition_root(
            plan,
            expected_seal_file_digest=_digest((root / "B1-EVIDENCE-SEAL.json").read_bytes()),
            allowed_output_parent=parent,
        )
