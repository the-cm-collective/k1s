from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_guest_filesystem as guest_filesystem
from scripts.qualification import kp1_q1_guest_image as guest_image


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _write(path: Path, raw: bytes, mode: int = 0o600) -> dict[str, str]:
    path.write_bytes(raw)
    path.chmod(mode)
    return {"expectedSha256": _digest(raw), "path": str(path)}


def _component(name: str) -> dict[str, str]:
    return {"sha256": _digest(name.encode()), "version": f"{name}-1.0.0"}


def _guest_template(digests: dict[str, str]) -> dict[str, object]:
    return {
        "cni": {
            "configTemplateSha256": _digest(b"cni-config"),
            "plugins": [
                {"name": name, **_component(name)}
                for name in ("bridge", "portmap", "firewall", "tuning", "loopback")
            ],
            "version": "1.0.0",
        },
        "helper": {"sha256": digests["guestHelper"]},
        "images": [
            {
                "archiveSha256": _digest(b"pause-archive"),
                "manifestDigest": _digest(b"pause-manifest"),
                "name": "pause",
            }
        ],
        "kernel": {
            "imageSha256": _digest(b"kernel"),
            "package": "linux-image-generic",
            "release": "5.15.0-100-generic",
        },
        "os": {
            "id": "ubuntu",
            "releaseSha256": _digest(b"os-release"),
            "version": "22.04",
        },
        "python": {"executableSha256": _digest(b"python"), "version": "3.11.18"},
        "registry": {
            "archiveSha256": _digest(b"registry-archive"),
            "imageDigest": _digest(b"registry-manifest"),
        },
        "runtime": {
            name: _component(name) for name in ("containerd", "crictl", "docker", "podman", "runc")
        },
        "wheelhouse": {
            "manifestSha256": digests["wheelhouseManifest"],
            "requirementsLockSha256": digests["requirementsLock"],
        },
    }


def _fixture(tmp_path: Path) -> tuple[dict[str, object], Path]:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    inputs: dict[str, dict[str, str]] = {}
    raw_inputs = {
        "acceptedSdist": b"sdist",
        "acceptedWheel": b"wheel",
        "b1AcquisitionPlan": b"b1-plan",
        "b1EvidenceSeal": b"b1-seal",
        "b2AcquisitionPlan": b"b2-plan",
        "b2EvidenceSeal": b"b2-seal",
        "baseAcquisitionModule": b"base-acquisition-module",
        "baseImage": b"base-image",
        "guestBomModule": b"guest-bom-module",
        "guestBundle": b"guest-bundle",
        "guestCapsuleModule": b"guest-capsule-module",
        "guestHelper": b"guest-helper",
        "guestImageModule": b"guest-image-module",
        "guestInputsModule": b"guest-inputs-module",
        "filesystemManifestModule": b"filesystem-manifest-module",
        "inventoryModule": b"inventory-module",
        "liveBuilder": b"live-builder",
        "offlinePayload": b"offline-payload",
        "packageManifest": b"package-manifest",
        "requirementsLock": b"requirements-lock",
        "signedChecksums": b"signed-checksums",
        "signedChecksumsSignature": b"signed-checksums-signature",
        "wheelhouseManifest": b"wheelhouse-manifest",
    }
    for name, raw in raw_inputs.items():
        inputs[name] = _write(root / name, raw)
    digests = {name: value["expectedSha256"] for name, value in inputs.items()}
    bom = {
        "apiVersion": guest_image.BOM_API_VERSION,
        "guest": _guest_template(digests),
        "kind": guest_image.BOM_KIND,
        "payload": {
            "memberCount": 10,
            "sha256": digests["offlinePayload"],
            "size": len(raw_inputs["offlinePayload"]),
        },
        "schemaVersion": guest_image.BOM_SCHEMA_VERSION,
        "sourceCommit": "1" * 40,
        "sourceDateEpoch": guest_image.SOURCE_DATE_EPOCH,
        "sourceTree": "2" * 40,
    }
    inputs["offlineBom"] = _write(
        root / "offline-bom.json",
        guest_image.canonical_json_bytes(bom),
    )
    tools = {
        name: _write(root / name, f"{name}-tool".encode(), 0o700)
        for name in ("gpgv", "python", "qemu", "qemuImg", "seedBuilder")
    }
    serial = "20261101"
    release = f"https://cloud-images.ubuntu.com/releases/jammy/release-{serial}"
    image_filename = "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img"
    manifest_filename = "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.manifest"
    spec: dict[str, object] = {
        "apiVersion": guest_image.API_VERSION,
        "base": {
            "architecture": "amd64",
            "imageFilename": image_filename,
            "imageSha256": digests["baseImage"],
            "imageUrl": f"{release}/{image_filename}",
            "packageManifestFilename": manifest_filename,
            "packageManifestSha256": digests["packageManifest"],
            "packageManifestUrl": f"{release}/{manifest_filename}",
            "serial": serial,
            "sha256sumsUrl": f"{release}/SHA256SUMS",
            "signatureUrl": f"{release}/SHA256SUMS.gpg",
            "signerFingerprint": "A" * 40,
        },
        "inputs": inputs,
        "kind": guest_image.SPEC_KIND,
        "outputParent": str(root),
        "schemaVersion": guest_image.SCHEMA_VERSION,
        "source": {
            "commit": "1" * 40,
            "tree": "2" * 40,
            "version": guest_image.CANDIDATE_VERSION,
        },
        "sourceDateEpoch": guest_image.SOURCE_DATE_EPOCH,
        "tools": tools,
    }
    return spec, root


class _Clock:
    def __init__(self) -> None:
        self.tick = 0.0
        self.wall = 1_800_000_000

    def monotonic(self) -> float:
        self.tick += 0.01
        return self.tick

    def wall_time(self) -> int:
        self.wall += 1
        return self.wall


class _Adapter:
    def __init__(self, *, image_mismatch: bool = False, semantic_mismatch: bool = False) -> None:
        self.calls: list[guest_image.StageRequest] = []
        self.image_mismatch = image_mismatch
        self.semantic_mismatch = semantic_mismatch

    def execute(self, request: guest_image.StageRequest) -> guest_image.StageObservation:
        self.calls.append(request)
        artifacts = None
        if request.stage == guest_image.STAGES[-1]:
            image_raw = b"lane-b" if self.image_mismatch and request.lane == "lane-b" else b"image"
            filesystem_raw = (
                b"lane-b-filesystem"
                if self.semantic_mismatch and request.lane == "lane-b"
                else b"filesystem"
            )
            artifacts = {
                "filesystemManifestSha256": _digest(filesystem_raw),
                "filesystemPolicySha256": guest_filesystem.policy_digest(),
                "guestManifestSha256": _digest(b"manifest"),
                "guestSemanticSha256": _digest(b"semantic"),
                "imageSha256": _digest(image_raw),
                "imageSize": 1024,
                "inventorySha256": _digest(b"inventory"),
            }
        return guest_image.StageObservation(
            identity=dict(request.authority),
            exit_code=0,
            timed_out=False,
            stdout=f"{request.lane}:{request.stage}".encode(),
            artifacts=artifacts,
        )


def test_build_plan_is_closed_deterministic_and_two_lane(tmp_path: Path) -> None:
    spec, root = _fixture(tmp_path)
    plan = guest_image.build_plan(spec)

    assert guest_image.verify_plan(plan) == plan
    assert plan["qualificationPosture"] == "source-validated/unqualified"
    assert [lane["name"] for lane in plan["lanes"]] == ["lane-a", "lane-b"]
    assert [lane["umask"] for lane in plan["lanes"]] == ["0022", "0077"]
    assert all(lane["stages"] == list(guest_image.STAGES) for lane in plan["lanes"])
    assert guest_image.build_plan(spec)["planDigest"] == plan["planDigest"]
    assert not (root / "lane-a").exists()


@pytest.mark.parametrize(
    ("mutation",),
    [
        (lambda spec: spec.update(extra=True),),
        (
            lambda spec: spec["base"].update(
                imageUrl="https://cloud-images.ubuntu.com/releases/jammy/release/image"
            ),
        ),
        (lambda spec: spec["base"].update(imageSha256=_digest(b"wrong")),),
        (lambda spec: spec["source"].update(commit="f" * 40),),
    ],
)
def test_build_plan_rejects_contract_drift(tmp_path: Path, mutation: object) -> None:
    spec, _ = _fixture(tmp_path)
    mutation(spec)
    with pytest.raises(guest_image.GuestImagePolicyError, match="failed closed"):
        guest_image.build_plan(spec)


def test_build_plan_rejects_existing_lane_and_symlink(tmp_path: Path) -> None:
    spec, root = _fixture(tmp_path)
    (root / "lane-a").mkdir()
    with pytest.raises(guest_image.GuestImagePolicyError):
        guest_image.build_plan(spec)

    (root / "lane-a").rmdir()
    original = Path(spec["inputs"]["guestHelper"]["path"])
    replacement = root / "guest-helper-link"
    replacement.symlink_to(original)
    spec["inputs"]["guestHelper"]["path"] = str(replacement)
    with pytest.raises(guest_image.GuestImagePolicyError):
        guest_image.build_plan(spec)


def test_plan_tamper_and_input_identity_drift_fail_closed(tmp_path: Path) -> None:
    spec, _ = _fixture(tmp_path)
    plan = guest_image.build_plan(spec)
    tampered = copy.deepcopy(plan)
    tampered["mutating"] = False
    with pytest.raises(guest_image.GuestImagePolicyError):
        guest_image.verify_plan(tampered)

    helper = Path(spec["inputs"]["guestHelper"]["path"])
    helper.write_bytes(b"changed")
    with pytest.raises(guest_image.GuestImagePolicyError):
        guest_image.revalidate_plan_files(plan)


def test_synthetic_build_runs_exact_stage_order_and_verifies(tmp_path: Path) -> None:
    spec, _ = _fixture(tmp_path)
    plan = guest_image.build_plan(spec)
    adapter = _Adapter()
    receipt = guest_image.run_build(
        plan,
        adapter=adapter,
        clock=_Clock(),
        revalidate_files=False,
    )

    assert len(adapter.calls) == len(guest_image.LANES) * len(guest_image.STAGES)
    assert all(call.plan == plan for call in adapter.calls)
    assert [(call.lane, call.stage) for call in adapter.calls] == [
        (lane, stage) for lane, _ in guest_image.LANES for stage in guest_image.STAGES
    ]
    assert (
        guest_image.verify_receipt(
            receipt,
            expected_plan_digest=plan["planDigest"],
        )
        == receipt
    )


def test_synthetic_build_accepts_lane_specific_image_identity(tmp_path: Path) -> None:
    spec, _ = _fixture(tmp_path)
    plan = guest_image.build_plan(spec)
    receipt = guest_image.run_build(
        plan,
        adapter=_Adapter(image_mismatch=True),
        clock=_Clock(),
        revalidate_files=False,
    )
    lanes = receipt["lanes"]
    assert lanes[0]["artifacts"]["imageSha256"] != lanes[1]["artifacts"]["imageSha256"]
    assert guest_image.verify_receipt(receipt, expected_plan_digest=plan["planDigest"])


def test_synthetic_build_rejects_semantic_lane_divergence(tmp_path: Path) -> None:
    spec, _ = _fixture(tmp_path)
    plan = guest_image.build_plan(spec)
    with pytest.raises(guest_image.GuestImagePolicyError):
        guest_image.run_build(
            plan,
            adapter=_Adapter(semantic_mismatch=True),
            clock=_Clock(),
            revalidate_files=False,
        )


@pytest.mark.parametrize(
    "failure",
    ["identity", "exit", "timeout", "output", "early-artifact", "plan-mutation"],
)
def test_synthetic_build_rejects_stage_failure(tmp_path: Path, failure: str) -> None:
    spec, _ = _fixture(tmp_path)
    plan = guest_image.build_plan(spec)

    class BrokenAdapter(_Adapter):
        def execute(self, request: guest_image.StageRequest) -> guest_image.StageObservation:
            observation = super().execute(request)
            if len(self.calls) != 1:
                return observation
            if failure == "plan-mutation":
                assert isinstance(request.plan, dict)
                request.plan["mutating"] = False
                return observation
            if failure == "identity":
                return guest_image.StageObservation({}, 0, False)
            if failure == "exit":
                return guest_image.StageObservation(request.authority, 1, False)
            if failure == "timeout":
                return guest_image.StageObservation(request.authority, 0, True)
            if failure == "output":
                return guest_image.StageObservation(
                    request.authority,
                    0,
                    False,
                    stdout=b"x" * (guest_image.MAX_STAGE_OUTPUT_BYTES + 1),
                )
            return guest_image.StageObservation(
                request.authority,
                0,
                False,
                artifacts={
                    "filesystemManifestSha256": _digest(b"filesystem"),
                    "filesystemPolicySha256": guest_filesystem.policy_digest(),
                    "guestManifestSha256": _digest(b"manifest"),
                    "guestSemanticSha256": _digest(b"semantic"),
                    "imageSha256": _digest(b"image"),
                    "imageSize": 1024,
                    "inventorySha256": _digest(b"inventory"),
                },
            )

    with pytest.raises(guest_image.GuestImagePolicyError):
        guest_image.run_build(
            plan,
            adapter=BrokenAdapter(),
            clock=_Clock(),
            revalidate_files=False,
        )


def test_plan_cli_writes_private_canonical_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec, root = _fixture(tmp_path)
    spec_path = root / "spec.json"
    spec_raw = guest_image.canonical_json_bytes(spec)
    spec_path.write_bytes(spec_raw)
    spec_path.chmod(0o600)
    output = root / "plan.json"

    assert (
        guest_image.main(
            [
                "plan",
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
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "planned"
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.read_bytes().endswith(b"\n")
    assert (
        guest_image.main(
            [
                "verify-plan",
                "--plan",
                str(output),
                "--expect-plan-sha256",
                _digest(output.read_bytes()),
            ]
        )
        == 0
    )
