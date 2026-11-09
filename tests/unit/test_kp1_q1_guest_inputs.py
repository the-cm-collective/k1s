from __future__ import annotations

import copy
import hashlib
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
    return plan, parent


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
    plan, parent = _fixture(tmp_path)

    assert guest_inputs.verify_plan(plan, allowed_output_parent=parent) == plan
    assert len(plan["wheelhouse"]) == 24
    assert len(plan["oci"]) == 9
    assert plan["apt"]["snapshotId"] == guest_inputs.SNAPSHOT_ID
    assert not Path(plan["outputRoot"]).exists()


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
    plan, parent = _fixture(tmp_path)
    changed = copy.deepcopy(plan)
    mutation(changed)
    with pytest.raises(guest_inputs.GuestInputPolicyError, match="failed closed"):
        guest_inputs.verify_plan(changed, allowed_output_parent=parent)


def test_acquisition_seals_and_independently_replays_root(tmp_path: Path) -> None:
    plan, parent = _fixture(tmp_path)
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
    plan, parent = _fixture(tmp_path)
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
