from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_guest_inputs as guest_inputs


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _write(path: Path, raw: bytes, *, executable: bool = False) -> dict[str, str]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o700 if executable else 0o600)
    return {"expectedSha256": _digest(raw), "path": str(path)}


def _bound(value: dict[str, object], field: str) -> dict[str, object]:
    result = copy.deepcopy(value)
    result[field] = _digest(guest_inputs.canonical_json_bytes(result))
    return result


def _fixture(tmp_path: Path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700, parents=True)
    parent.chmod(0o700)
    inputs: dict[str, dict[str, str]] = {}
    for name in sorted(guest_inputs.INPUT_NAMES):
        inputs[name] = _write(
            parent / "authority" / name,
            f"{name}-authority".encode(),
            executable=name == "guestHelper",
        )
    wheelhouse = parent / "wheelhouse"
    wheelhouse.mkdir(mode=0o700)
    wheel_rows: list[dict[str, object]] = []
    for index in range(24):
        filename = f"wheel-{index:02d}-py3-none-any.whl"
        raw = f"wheel-{index}".encode()
        _write(wheelhouse / filename, raw)
        wheel_rows.append(
            {
                "filename": filename,
                "sha256": _digest(raw).removeprefix("sha256:"),
                "size": len(raw),
            }
        )
    wheelhouse_manifest = {
        "runtimeWheelhouseCount": len(wheel_rows),
        "runtimeWheels": wheel_rows,
    }
    manifest_raw = guest_inputs.canonical_json_bytes(wheelhouse_manifest)
    manifest_path = Path(inputs["wheelhouseManifest"]["path"])
    manifest_path.write_bytes(manifest_raw)
    inputs["wheelhouseManifest"]["expectedSha256"] = _digest(manifest_raw)
    base = guest_inputs.base_acquisition
    base_manifest = inputs["basePackageManifest"]
    base_artifacts = []
    for filename in sorted(base.DOWNLOAD_ORDER):
        if filename == "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.manifest":
            sha256 = base_manifest["expectedSha256"]
            size = Path(base_manifest["path"]).stat().st_size
        else:
            raw = filename.encode()
            sha256 = _digest(raw)
            size = len(raw)
        base_artifacts.append(
            {"filename": filename, "mode": "0400", "sha256": sha256, "size": size}
        )
    base_receipt = _bound(
        {
            "apiVersion": base.API_VERSION,
            "artifacts": base_artifacts,
            "completedAtUnix": 1_800_000_001,
            "image": {
                "actualSize": 1024,
                "format": "qcow2",
                "hasBackingFile": False,
                "virtualSize": base.MIN_VIRTUAL_SIZE,
            },
            "kind": base.RECEIPT_KIND,
            "planDigest": _digest(b"base-plan"),
            "qualificationPosture": base.QUALIFICATION_POSTURE,
            "schemaVersion": base.SCHEMA_VERSION,
            "signature": {
                "fingerprint": "A" * 40,
                "stderrSha256": _digest(b"stderr"),
                "stderrSize": 0,
                "stdoutSha256": _digest(b"stdout"),
                "stdoutSize": 0,
            },
            "source": {"commit": "3" * 40, "tree": "4" * 40, "version": "0.1.8a1"},
            "startedAtUnix": 1_800_000_000,
            "status": "acquired-and-authenticated",
        },
        "receiptDigest",
    )
    base_receipt_raw = guest_inputs.canonical_json_bytes(base_receipt)
    base_receipt_path = Path(inputs["baseAcquisitionReceipt"]["path"])
    base_receipt_path.write_bytes(base_receipt_raw)
    inputs["baseAcquisitionReceipt"]["expectedSha256"] = _digest(base_receipt_raw)
    accepted_artifacts = {}
    for name, input_name, filename in (
        ("sdist", "acceptedSdist", "dist/k1s_core-0.1.8a1.tar.gz"),
        ("wheel", "acceptedWheel", "dist/k1s_core-0.1.8a1-py3-none-any.whl"),
    ):
        identity = inputs[input_name]
        accepted_artifacts[name] = {
            "filename": filename,
            "memberCount": 1,
            "memberManifestSha256": hashlib.sha256(f"{name}-members".encode()).hexdigest(),
            "sha256": identity["expectedSha256"].removeprefix("sha256:"),
            "size": Path(identity["path"]).stat().st_size,
        }
    qh5a = {
        "acceptedCandidate": {
            "artifactManifestSha256": hashlib.sha256(b"artifact-manifest").hexdigest(),
            "artifacts": accepted_artifacts,
            "derivedFrom": "test lanes",
            "files": [],
            "independentVerificationReceiptSha256": hashlib.sha256(b"verify").hexdigest(),
            "independentVerificationStatus": "verified",
            "path": "accepted-candidate",
            "sha256sumsSha256": hashlib.sha256(b"sums").hexdigest(),
            "sourceManifestSha256": hashlib.sha256(b"source-manifest").hexdigest(),
        },
        "artifactAssemblyStatus": "completed",
        "boundaries": {
            "artifactPublished": False,
            "hostServiceChanged": False,
            "packageIndexUsed": False,
            "qemuOrKvmInvoked": False,
            "runtimeWorkloadStarted": False,
            "sourceCommitCreated": False,
            "sourceFilesModified": False,
            "tagOrPushPerformed": False,
        },
        "completedAt": "2026-11-09T18:00:00Z",
        "inputs": {
            "requirementsLockSha256": inputs["requirementsLock"]["expectedSha256"].removeprefix(
                "sha256:"
            ),
            "runtimeWheelhouseCount": len(wheel_rows),
            "runtimeWheelhouseIdentity": hashlib.sha256(b"wheelhouse").hexdigest(),
            "wheelhouseManifestContentSha256": hashlib.sha256(manifest_raw).hexdigest(),
            "wheelhouseManifestFileSha256": inputs["wheelhouseManifest"][
                "expectedSha256"
            ].removeprefix("sha256:"),
            "wheelhouseReverificationReceiptSha256": hashlib.sha256(b"reverify").hexdigest(),
        },
        "installedSmoke": {
            "networkIndexMode": "--no-index",
            "pipCheck": "passed",
            "status": "passed",
        },
        "liveQualificationStatus": "not-started",
        "qualificationPosture": guest_inputs.QUALIFICATION_POSTURE,
        "releasePromotionStatus": "not-started",
        "reproducibility": {"byteIdentical": True},
        "schema": "k1s-qh5a-completion/v1",
        "setupCorrections": [],
        "source": {
            "branch": "test",
            "commit": "5" * 40,
            "fullStatus": "",
            "repository": str(tmp_path),
            "trackedStatus": "",
            "tree": "6" * 40,
            "version": "0.1.8a1",
        },
        "status": "completed",
        "toolchain": {},
    }
    qh5a_raw = guest_inputs.canonical_json_bytes(qh5a)
    qh5a_path = Path(inputs["qh5aCompletion"]["path"])
    qh5a_path.write_bytes(qh5a_raw)
    inputs["qh5aCompletion"]["expectedSha256"] = _digest(qh5a_raw)
    tools: dict[str, dict[str, str]] = {}
    tool_paths: dict[str, str] = {}
    for name in guest_inputs.TOOL_NAMES:
        path = parent / "tools" / name
        tools[name] = _write(path, f"{name}-tool".encode(), executable=True)
        tool_paths[name] = str(path)
    spec = {
        "apiVersion": guest_inputs.API_VERSION,
        "inputs": inputs,
        "kind": guest_inputs.SPEC_KIND,
        "outputName": "qh5b-b2-0.1.8a1-11111111-20261109T180000Z",
        "outputParent": str(parent),
        "schemaVersion": guest_inputs.SCHEMA_VERSION,
        "source": {"commit": "1" * 40, "tree": "2" * 40, "version": "0.1.8a1"},
        "tools": tools,
        "wheelhouseRoot": str(wheelhouse),
    }
    plan = guest_inputs.build_plan(
        spec,
        allowed_output_parent=parent,
        expected_tool_paths=tool_paths,
    )
    return plan, parent, spec


def _stage_file(root: Path, relative: str, raw: bytes, *, executable: bool = False) -> str:
    path = root / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o700 if executable else 0o600)
    return _digest(raw)


class _Adapter:
    def stage(self, _plan, staging_root: Path) -> guest_inputs.StageResult:
        roles: dict[str, str] = {}
        singleton = {
            "q1/accepted/source.tar.gz": "accepted-sdist",
            "q1/accepted/source.whl": "accepted-wheel",
            "apt/package-ledger.json": "apt-package-ledger",
            "q1/base/B1-ACQUISITION-RECEIPT.json": "base-acquisition-receipt",
            "q1/base/base.manifest": "base-package-manifest",
            "crictl/crictl.tar.gz": "crictl-archive",
            "crictl/crictl.sha256": "crictl-checksum",
            "apt/metadata/deadsnakes.gpg": "deadsnakes-keyring",
            "q1/accepted/QH5-A-COMPLETION.json": "qh5a-completion",
            "oci/resolution-ledger.json": "oci-resolution-ledger",
            "q1/offline-evidence.json": "offline-evidence",
            "apt/metadata/ubuntu.gpg": "ubuntu-keyring",
        }
        for path, role in singleton.items():
            _stage_file(staging_root, path, role.encode())
            roles[path] = role
        helper_sha = _stage_file(
            staging_root,
            "q1/helper/helper.py",
            b"#!/usr/bin/python3\n",
            executable=True,
        )
        roles["q1/helper/helper.py"] = "guest-helper"
        requirements_sha = _stage_file(
            staging_root,
            "q1/wheelhouse/requirements-release.lock",
            b"locked\n",
        )
        roles["q1/wheelhouse/requirements-release.lock"] = "requirements-lock"
        manifest_sha = _stage_file(
            staging_root,
            "q1/wheelhouse/wheelhouse-manifest.json",
            b'{"count":1}\n',
        )
        roles["q1/wheelhouse/wheelhouse-manifest.json"] = "wheelhouse-manifest"
        _stage_file(staging_root, "q1/wheelhouse/wheels/source.whl", b"wheel")
        roles["q1/wheelhouse/wheels/source.whl"] = "wheel"
        cni_raw = guest_inputs.canonical_json_bytes(
            {
                "cniVersion": "1.0.0",
                "name": "q1",
                "plugins": [
                    {"type": "bridge"},
                    {"type": "portmap"},
                    {"type": "firewall"},
                    {"type": "tuning"},
                ],
            }
        )
        cni_sha = _stage_file(staging_root, "cni/10-q1.conflist", cni_raw)
        roles["cni/10-q1.conflist"] = "cni-config"
        for index in range(8):
            path = f"apt/metadata/index-{index}"
            _stage_file(staging_root, path, f"metadata-{index}".encode())
            roles[path] = "apt-metadata"
        _stage_file(staging_root, "apt/packages/runtime.deb", b"deb")
        roles["apt/packages/runtime.deb"] = "apt-package"
        busybox_sha = _stage_file(staging_root, "oci/busybox.oci.tar", b"busybox")
        registry_sha = _stage_file(staging_root, "oci/registry.oci.tar", b"registry")
        roles["oci/busybox.oci.tar"] = "oci-image"
        roles["oci/registry.oci.tar"] = "oci-image"

        def component(name: str) -> dict[str, str]:
            return {"sha256": _digest(name.encode()), "version": f"{name} 1"}

        guest = {
            "cni": {
                "configTemplateSha256": cni_sha,
                "plugins": [
                    {"name": name, **component(name)}
                    for name in ("bridge", "portmap", "firewall", "tuning", "loopback")
                ],
                "version": "1.0.0",
            },
            "helper": {"sha256": helper_sha},
            "images": [
                {
                    "archiveSha256": busybox_sha,
                    "manifestDigest": _digest(b"busybox-manifest"),
                    "name": "busybox",
                }
            ],
            "kernel": {
                "imageSha256": _digest(b"kernel"),
                "package": "linux-image-5.15.0-q1",
                "release": "5.15.0-q1",
            },
            "os": {"id": "ubuntu", "releaseSha256": _digest(b"os"), "version": "22.04"},
            "python": {"executableSha256": _digest(b"python"), "version": "3.11.18"},
            "registry": {
                "archiveSha256": registry_sha,
                "imageDigest": _digest(b"registry-manifest"),
            },
            "runtime": {
                name: component(name)
                for name in ("containerd", "crictl", "docker", "podman", "runc")
            },
            "wheelhouse": {
                "manifestSha256": manifest_sha,
                "requirementsLockSha256": requirements_sha,
            },
        }
        offline = {
            "apiVersion": "k1s.dev/q1-guest-offline-evidence/v1",
            "helper": guest["helper"],
            "images": guest["images"],
            "kind": "K1sQ1GuestOfflineEvidence",
            "registry": guest["registry"],
            "schemaVersion": 1,
            "wheelhouse": guest["wheelhouse"],
        }
        (staging_root / "q1/offline-evidence.json").write_bytes(
            guest_inputs.canonical_json_bytes(offline)
        )
        return guest_inputs.StageResult(
            guest=guest,
            member_roles=dict(sorted(roles.items())),
            evidence={"adapter": "synthetic"},
        )


def test_plan_binds_closed_sources_tools_and_authorities(tmp_path: Path) -> None:
    plan, parent, _spec = _fixture(tmp_path)

    assert guest_inputs.verify_plan(plan, allowed_output_parent=parent) == plan
    assert plan["apt"]["packages"][0] == {"name": "acl", "version": None}
    assert len(plan["wheelhouse"]) == 24
    assert len(plan["oci"]) == 9
    assert plan["apt"]["snapshotId"] == guest_inputs.SNAPSHOT_ID
    assert not Path(plan["outputRoot"]).exists()


def test_deb_fields_uses_value_only_show_format(tmp_path: Path, monkeypatch) -> None:
    deb = tmp_path / "package.deb"
    deb.write_bytes(b"deb")

    def run(command, *, cwd, **_kwargs):
        assert command == (
            "/usr/bin/dpkg-deb",
            "--show",
            "--showformat=${Package}\n${Version}\n${Architecture}\n",
            str(deb),
        )
        assert cwd == tmp_path
        return subprocess.CompletedProcess(command, 0, b"package\n1.2.3\namd64\n", b"")

    monkeypatch.setattr(guest_inputs, "_run", run)

    assert guest_inputs._deb_fields("/usr/bin/dpkg-deb", deb, cwd=tmp_path) == (
        "package",
        "1.2.3",
        "amd64",
    )


def test_runtime_binary_paths_pin_canonical_runc_location(tmp_path: Path) -> None:
    canonical = tmp_path / "usr/bin/runc"
    duplicate = tmp_path / "usr/sbin/runc"
    _write(canonical, b"runc", executable=True)
    _write(duplicate, b"runc", executable=True)

    assert guest_inputs.RUNTIME_BINARY_PATHS["runc"] == ("usr/bin/runc",)
    assert (
        guest_inputs.LiveAcquisitionAdapter._inspect_file(
            tmp_path,
            guest_inputs.RUNTIME_BINARY_PATHS["runc"],
            executable=True,
        )
        == canonical
    )


def test_apt_payload_filename_is_portable_and_digest_bound() -> None:
    digest = _digest(b"package")

    assert guest_inputs._apt_payload_filename("libaudit1", digest) == (
        f"libaudit1-{digest.removeprefix('sha256:')[:32]}.deb"
    )
    assert "%" not in guest_inputs._apt_payload_filename("libaudit1", digest)
    assert "~" not in guest_inputs._apt_payload_filename("libaudit1", digest)
    with pytest.raises(guest_inputs.GuestInputPolicyError):
        guest_inputs._apt_payload_filename("invalid/package", digest)


def test_safe_stage_path_makes_private_intermediate_directories(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    root.mkdir(mode=0o700)

    target = guest_inputs._safe_stage_path(root, "apt/packages/package.deb")

    assert target == root / "apt/packages/package.deb"
    assert (root / "apt").stat().st_mode & 0o7777 == 0o700
    assert (root / "apt/packages").stat().st_mode & 0o7777 == 0o700


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value["source"].update(version="1.0.0"),
        lambda value: value["oci"][0].update(expectedManifestDigest=_digest(b"wrong")),
        lambda value: value["apt"].update(snapshotId="latest"),
    ],
)
def test_plan_rejects_drift(tmp_path: Path, mutation) -> None:
    plan, parent, _spec = _fixture(tmp_path)
    changed = copy.deepcopy(plan)
    mutation(changed)
    with pytest.raises(guest_inputs.GuestInputPolicyError, match="failed closed"):
        guest_inputs.verify_plan(changed, allowed_output_parent=parent)


@pytest.mark.parametrize(
    ("input_name", "mutate"),
    [
        (
            "baseAcquisitionReceipt",
            lambda value: next(
                row
                for row in value["artifacts"]
                if row["filename"] == "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.manifest"
            ).update(sha256=_digest(b"unrelated-base-manifest")),
        ),
        (
            "qh5aCompletion",
            lambda value: value["acceptedCandidate"]["artifacts"]["wheel"].update(
                sha256=hashlib.sha256(b"unrelated-wheel").hexdigest()
            ),
        ),
    ],
)
def test_plan_rejects_semantically_unrelated_authority(
    tmp_path: Path, input_name: str, mutate
) -> None:
    _plan, parent, spec = _fixture(tmp_path)
    path = Path(spec["inputs"][input_name]["path"])
    value = json.loads(path.read_bytes())
    mutate(value)
    if input_name == "baseAcquisitionReceipt":
        value.pop("receiptDigest")
        value = _bound(value, "receiptDigest")
    raw = guest_inputs.canonical_json_bytes(value)
    path.write_bytes(raw)
    spec["inputs"][input_name]["expectedSha256"] = _digest(raw)

    with pytest.raises(guest_inputs.GuestInputPolicyError, match="failed closed"):
        guest_inputs.build_plan(spec, allowed_output_parent=parent)


def test_acquisition_seals_and_independently_replays_root(tmp_path: Path) -> None:
    plan, parent, _spec = _fixture(tmp_path)
    receipt, seal = guest_inputs.execute_acquisition(
        plan,
        confirmation=f"ACQUIRE-Q1-BOM:{plan['planDigest']}",
        adapter=_Adapter(),
        clock=lambda: 1_800_000_000,
        allowed_output_parent=parent,
    )
    root = Path(plan["outputRoot"])
    result = guest_inputs.verify_retained_root(
        plan,
        expected_seal_file_sha256=_digest((root / "B2-EVIDENCE-SEAL.json").read_bytes()),
        allowed_output_parent=parent,
    )

    assert receipt["status"] == "bom-closed"
    assert seal["status"] == "retained-b2-evidence"
    assert result["status"] == "retained-b2-evidence-verified"
    assert result["bomSha256"] == receipt["bomSha256"]
    assert (root / "B2-OFFLINE-PAYLOAD.tar").stat().st_size == receipt["payloadSize"]


def test_acquisition_rejects_confirmation_and_retained_tamper(tmp_path: Path) -> None:
    plan, parent, _spec = _fixture(tmp_path)
    with pytest.raises(guest_inputs.GuestInputPolicyError):
        guest_inputs.execute_acquisition(
            plan,
            confirmation="ACQUIRE-Q1-BOM:sha256:" + "0" * 64,
            adapter=_Adapter(),
            allowed_output_parent=parent,
        )
    receipt, _seal = guest_inputs.execute_acquisition(
        plan,
        confirmation=f"ACQUIRE-Q1-BOM:{plan['planDigest']}",
        adapter=_Adapter(),
        allowed_output_parent=parent,
    )
    root = Path(receipt["outputRoot"])
    target = root / "payload" / "apt/packages/runtime.deb"
    target.write_bytes(b"drift")

    with pytest.raises(guest_inputs.GuestInputPolicyError):
        guest_inputs.verify_retained_root(plan, allowed_output_parent=parent)
