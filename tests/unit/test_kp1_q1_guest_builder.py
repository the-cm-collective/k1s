from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_guest_builder as builder
from scripts.qualification import kp1_q1_guest_inventory as inventory


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _inventory_spec() -> dict[str, object]:
    release = "5.15.0-1112-kvm"
    return {
        "apiVersion": inventory.API_VERSION,
        "authority": {
            "buildPlanDigest": _digest(b"plan"),
            "helperSha256": _digest(b"helper"),
            "imageSha256": _digest(b"image"),
            "lane": "lane-a",
            "requirementsLockSha256": _digest(b"requirements"),
            "sourceCommit": "1" * 40,
            "sourceTree": "2" * 40,
            "wheelhouseManifestSha256": _digest(b"wheelhouse"),
        },
        "expected": {
            "kernelPackage": f"linux-image-{release}",
            "kernelRelease": release,
        },
        "kind": inventory.SPEC_KIND,
        "paths": {
            "cni": {
                "config": "/etc/cni/net.d/10-k1s-bridge.conflist",
                "plugins": {name: f"/opt/cni/bin/{name}" for name in inventory.CNI_NAMES},
            },
            "kernelImage": f"/boot/vmlinuz-{release}",
            "offlineEvidence": "/etc/k1s-q1/offline-evidence.json",
            "osRelease": "/etc/os-release",
            "python": "/usr/bin/python3.11",
            "runtime": {name: f"/usr/bin/{name}" for name in inventory.RUNTIME_NAMES},
        },
        "schemaVersion": inventory.SCHEMA_VERSION,
    }


def _serial_value() -> dict[str, object]:
    return {
        "commands": [
            {
                "argv": ["/usr/bin/uname", "-r"],
                "exitCode": 0,
                "stderr": "",
                "stdout": base64.b64encode(b"5.15.0-1112-kvm\n").decode(),
                "timedOut": False,
            }
        ],
        "files": [
            {
                "gid": 0,
                "linkCount": 1,
                "mode": 0o755,
                "path": "/usr/bin/containerd",
                "raw": None,
                "regular": True,
                "sha256": _digest(b"containerd"),
                "size": 48_000_000,
                "symlink": False,
                "uid": 0,
            }
        ],
    }


def test_provision_script_is_offline_block_bound_and_contains_no_host_mount() -> None:
    raw = builder.render_provision_script(
        payload_sha256=_digest(b"payload"),
        payload_size=4096,
        member_manifest_sha256=_digest(b"manifest"),
        guest_bundle=b"bundle-source\n",
        umask="0077",
    )
    text = raw.decode()

    assert "PAYLOAD_DEVICE=/dev/vdb" in text
    assert "--no-download" in text
    assert "--no-index" in text
    assert "umask 0077" in text
    assert "K1S_Q1_PROVISION_OK" in text
    assert "-net" not in text
    assert "/home/" not in text
    assert "9f64a747e1b97f131fabb6b447296c9b6f0201e79fb3c5356e6c77e89b6a806a" not in text


def test_inventory_script_contains_closed_collection_and_serial_marker() -> None:
    raw = builder.render_inventory_script(_inventory_spec())
    text = raw.decode()

    assert "/usr/bin/python3.11" in text
    assert "K1S_Q1_INVENTORY_OK" in text
    assert "K1S_Q1_INVENTORY:" in base64.b64decode(
        text.split("<<'K1S_Q1_COLLECTOR'\n", 1)[1].split("\nK1S_Q1_COLLECTOR", 1)[0]
    ).decode()


def test_serialized_inventory_adapter_preserves_bounded_digest_evidence() -> None:
    value = _serial_value()
    raw = builder._canonical_json_bytes(value)
    serial = (
        b"boot output\n"
        + builder.INVENTORY_PREFIX
        + base64.b64encode(raw)
        + b"\n"
        + builder.INVENTORY_MARKER
        + b"\n"
    )

    adapter = builder.parse_inventory_serial(serial)
    observed = adapter.read_file("/usr/bin/containerd", maximum_bytes=16 * 1024 * 1024)
    command = adapter.execute(
        ("/usr/bin/uname", "-r"),
        timeout_seconds=inventory.COMMAND_TIMEOUT_SECONDS,
        maximum_output_bytes=inventory.MAX_COMMAND_OUTPUT_BYTES,
    )

    assert observed.raw is None
    assert observed.sha256 == _digest(b"containerd")
    assert observed.size == 48_000_000
    assert command.stdout == b"5.15.0-1112-kvm\n"


@pytest.mark.parametrize("mutation", ["missing-marker", "duplicate", "noncanonical"])
def test_inventory_serial_fails_closed(mutation: str) -> None:
    raw = builder._canonical_json_bytes(_serial_value())
    line = builder.INVENTORY_PREFIX + base64.b64encode(raw) + b"\n"
    serial = line + builder.INVENTORY_MARKER + b"\n"
    if mutation == "missing-marker":
        serial = line
    elif mutation == "duplicate":
        serial = line + line + builder.INVENTORY_MARKER + b"\n"
    else:
        noncanonical = json.dumps(_serial_value(), indent=2).encode()
        serial = (
            builder.INVENTORY_PREFIX
            + base64.b64encode(noncanonical)
            + b"\n"
            + builder.INVENTORY_MARKER
        )

    with pytest.raises(builder.GuestLiveBuildError):
        builder.parse_inventory_serial(serial)


def test_qemu_argv_is_tcg_networkless_read_only_and_snapshot_capable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder, "_identity_path", lambda *_args: Path("/usr/bin/qemu"))
    image = tmp_path / "guest.qcow2"
    seed = tmp_path / "seed.iso"
    serial = tmp_path / "serial.log"
    payload = tmp_path / "payload.tar"

    argv = builder._qemu_argv(
        {},
        image=image,
        seed=seed,
        serial=serial,
        payload=payload,
        snapshot=True,
    )

    assert argv[argv.index("-machine") + 1] == builder.QEMU_MACHINE
    assert argv[argv.index("-nic") + 1] == "none"
    assert "-snapshot" in argv
    assert not any(item == "user" or "hostfwd=" in item or "tap," in item for item in argv)
    payload_drive = next(item for item in argv if str(payload) in item)
    assert "readonly=on" in payload_drive
    assert "format=raw" in payload_drive


def test_live_builder_requires_new_authority_inputs() -> None:
    assert {
        "b1AcquisitionPlan",
        "b1EvidenceSeal",
        "b2AcquisitionPlan",
        "b2EvidenceSeal",
        "guestBundle",
        "inventoryModule",
        "liveBuilder",
    } <= builder.guest_image.INPUT_NAMES
