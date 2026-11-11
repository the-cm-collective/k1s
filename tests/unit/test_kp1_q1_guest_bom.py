from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_guest_bom as guest_bom


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _write(root: Path, relative: str, raw: bytes, *, executable: bool = False) -> str:
    path = root / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o700 if executable else 0o600)
    return _digest(raw)


def _populate(root: Path) -> tuple[dict[str, str], dict[str, object]]:
    roles: dict[str, str] = {}
    singleton = {
        "q1/accepted/source.tar.gz": "accepted-sdist",
        "q1/accepted/source.whl": "accepted-wheel",
        "apt/package-ledger.json": "apt-package-ledger",
        "q1/base/B1-ACQUISITION-RECEIPT.json": "base-acquisition-receipt",
        "q1/base/base.manifest": "base-package-manifest",
        "crictl/crictl.tar.gz": "crictl-archive",
        "crictl/crictl.tar.gz.sha256": "crictl-checksum",
        "apt/metadata/deadsnakes.gpg": "deadsnakes-keyring",
        "q1/accepted/QH5-A-COMPLETION.json": "qh5a-completion",
        "oci/resolution-ledger.json": "oci-resolution-ledger",
        "q1/offline-evidence.json": "offline-evidence",
        "apt/metadata/ubuntu.gpg": "ubuntu-keyring",
    }
    for index, (path, role) in enumerate(singleton.items()):
        _write(root, path, f"{role}-{index}".encode())
        roles[path] = role
    helper_raw = b"#!/usr/bin/python3\n"
    helper_sha = _write(root, "q1/helper/helper.py", helper_raw, executable=True)
    roles["q1/helper/helper.py"] = "guest-helper"
    requirements_raw = b"source==1 --hash=sha256:abc\n"
    requirements_sha = _write(
        root,
        "q1/wheelhouse/requirements-release.lock",
        requirements_raw,
    )
    roles["q1/wheelhouse/requirements-release.lock"] = "requirements-lock"
    wheelhouse_raw = b'{"runtimeWheelhouseCount":1}\n'
    wheelhouse_sha = _write(
        root,
        "q1/wheelhouse/wheelhouse-manifest.json",
        wheelhouse_raw,
    )
    roles["q1/wheelhouse/wheelhouse-manifest.json"] = "wheelhouse-manifest"
    wheel_path = (
        "q1/wheelhouse/wheels/"
        "charset_normalizer-3.5.2-cp311-cp311-manylinux2014_x86_64."
        "manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    )
    _write(root, wheel_path, b"wheel")
    roles[wheel_path] = "wheel"
    cni_raw = guest_bom.canonical_json_bytes(
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
    cni_sha = _write(root, "cni/10-q1.conflist", cni_raw)
    roles["cni/10-q1.conflist"] = "cni-config"
    for index in range(8):
        path = f"apt/metadata/index-{index}"
        _write(root, path, f"metadata-{index}".encode())
        roles[path] = "apt-metadata"
    _write(root, "apt/packages/runtime.deb", b"deb")
    roles["apt/packages/runtime.deb"] = "apt-package"
    busybox_raw = b"busybox-oci"
    registry_raw = b"registry-oci"
    busybox_sha = _write(root, "oci/busybox.oci.tar", busybox_raw)
    registry_sha = _write(root, "oci/registry.oci.tar", registry_raw)
    roles["oci/busybox.oci.tar"] = "oci-image"
    roles["oci/registry.oci.tar"] = "oci-image"

    def component(name: str) -> dict[str, str]:
        return {"sha256": _digest(name.encode()), "version": f"{name} 1.0"}

    guest: dict[str, object] = {
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
            name: component(name) for name in ("containerd", "crictl", "docker", "podman", "runc")
        },
        "wheelhouse": {
            "manifestSha256": wheelhouse_sha,
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
    (root / "q1/offline-evidence.json").write_bytes(guest_bom.canonical_json_bytes(offline))
    return dict(sorted(roles.items())), guest


def _source() -> dict[str, object]:
    return {
        "commit": "1" * 40,
        "sourceDateEpoch": guest_bom.SOURCE_DATE_EPOCH,
        "tree": "2" * 40,
        "version": "1.0.0rc2",
    }


def _build(parent: Path, name: str = "payload.tar"):
    parent.mkdir(mode=0o700, parents=True)
    parent.chmod(0o700)
    stage = parent / "stage"
    stage.mkdir(mode=0o700)
    roles, guest = _populate(stage)
    destination = parent / name
    manifest, bom = guest_bom.write_payload(
        staging_root=stage,
        destination=destination,
        source=_source(),
        guest=guest,
        member_roles=roles,
    )
    return stage, destination, manifest, bom


def test_payload_is_deterministic_and_replays_to_same_bom(tmp_path: Path) -> None:
    _stage_a, payload_a, manifest_a, bom_a = _build(tmp_path / "a")
    _stage_b, payload_b, manifest_b, bom_b = _build(tmp_path / "b")

    assert payload_a.read_bytes() == payload_b.read_bytes()
    assert manifest_a == manifest_b
    assert bom_a == bom_b
    result = guest_bom.verify_two_validator_gate(
        bom_raw=guest_bom.canonical_json_bytes(bom_a),
        payload_path=payload_a,
        expected_bom_sha256=guest_bom.digest_bytes(guest_bom.canonical_json_bytes(bom_a)),
        expected_payload_sha256=bom_a["payload"]["sha256"],
    )
    assert result["status"] == "two-validator-match"
    assert result["schemaValidatorSha256"] == result["replayValidatorSha256"]
    with tarfile.open(payload_a, "r:") as archive:
        wheel = next(info for info in archive.getmembers() if "charset_normalizer" in info.name)
        assert wheel.pax_headers == {"path": wheel.name}


def test_replay_rejects_payload_tamper(tmp_path: Path) -> None:
    _stage, payload, _manifest, bom = _build(tmp_path / "root")
    raw = bytearray(payload.read_bytes())
    raw[1024] ^= 1
    payload.chmod(0o600)
    payload.write_bytes(raw)

    with pytest.raises(guest_bom.GuestBOMPolicyError, match="failed closed"):
        guest_bom.replay_payload(
            payload,
            expected_payload_sha256=bom["payload"]["sha256"],
        )


def test_payload_rejects_guest_to_member_drift(tmp_path: Path) -> None:
    parent = tmp_path / "root"
    parent.mkdir(mode=0o700)
    stage = parent / "stage"
    stage.mkdir(mode=0o700)
    roles, guest = _populate(stage)
    guest["helper"]["sha256"] = _digest(b"different")

    with pytest.raises(guest_bom.GuestBOMPolicyError):
        guest_bom.write_payload(
            staging_root=stage,
            destination=parent / "payload.tar",
            source=_source(),
            guest=guest,
            member_roles=roles,
        )


def test_payload_rejects_protected_or_linked_member(tmp_path: Path) -> None:
    parent = tmp_path / "root"
    parent.mkdir(mode=0o700)
    stage = parent / "stage"
    stage.mkdir(mode=0o700)
    roles, guest = _populate(stage)
    target = stage / "q1/helper/helper.py"
    target.unlink()
    target.symlink_to(stage / "q1/accepted/source.whl")

    with pytest.raises(guest_bom.GuestBOMPolicyError):
        guest_bom.write_payload(
            staging_root=stage,
            destination=parent / "payload.tar",
            source=_source(),
            guest=guest,
            member_roles=roles,
        )


def test_cli_verify_emits_closed_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _stage, payload, _manifest, bom = _build(tmp_path / "root")
    bom_path = tmp_path / "root" / "bom.json"
    bom_path.write_bytes(guest_bom.canonical_json_bytes(bom))
    bom_path.chmod(0o600)

    assert guest_bom.main(["verify", "--bom", str(bom_path), "--payload", str(payload)]) == 0
    assert '"status":"two-validator-match"' in capsys.readouterr().out
