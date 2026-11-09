from __future__ import annotations

import copy
import hashlib
import json

import pytest
from scripts.qualification import kp1_q1_capsule as capsule
from scripts.qualification import kp1_q1_guest_inventory as inventory


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _spec() -> dict[str, object]:
    release = "5.15.0-100-generic"
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
            "kernelPackage": "linux-image-5.15.0-100-generic",
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
            "osRelease": "/usr/lib/os-release",
            "python": "/usr/bin/python3.11",
            "runtime": {name: f"/usr/bin/{name}" for name in inventory.RUNTIME_NAMES},
        },
        "schemaVersion": inventory.SCHEMA_VERSION,
    }


class _Adapter:
    def __init__(self, spec: dict[str, object]) -> None:
        authority = spec["authority"]
        paths = spec["paths"]
        cni = paths["cni"]
        self.files: dict[str, inventory.FileObservation] = {}
        self.commands: dict[tuple[str, ...], inventory.CommandObservation] = {}

        def add_file(path: str, raw: bytes, mode: int = 0o644) -> None:
            self.files[path] = inventory.FileObservation(path, raw, mode, 0, 0)

        add_file("/usr/lib/os-release", b'ID=ubuntu\nVERSION_ID="22.04"\n')
        add_file(paths["kernelImage"], b"kernel-image")
        add_file(paths["python"], b"python-binary", 0o755)
        for name, path in paths["runtime"].items():
            add_file(path, f"{name}-binary".encode(), 0o755)
        config = {
            "cniVersion": "1.0.0",
            "name": "k1s",
            "plugins": [{"type": name} for name in inventory.CNI_NAMES[:-1]],
        }
        add_file(cni["config"], json.dumps(config, sort_keys=True).encode())
        for name, path in cni["plugins"].items():
            add_file(path, f"{name}-binary".encode(), 0o755)
        offline = {
            "apiVersion": inventory.OFFLINE_API_VERSION,
            "helper": {"sha256": authority["helperSha256"]},
            "images": [
                {
                    "archiveSha256": _digest(b"pause-archive"),
                    "manifestDigest": _digest(b"pause-manifest"),
                    "name": "pause",
                }
            ],
            "kind": inventory.OFFLINE_KIND,
            "registry": {
                "archiveSha256": _digest(b"registry-archive"),
                "imageDigest": _digest(b"registry-manifest"),
            },
            "schemaVersion": inventory.SCHEMA_VERSION,
            "wheelhouse": {
                "manifestSha256": authority["wheelhouseManifestSha256"],
                "requirementsLockSha256": authority["requirementsLockSha256"],
            },
        }
        add_file(paths["offlineEvidence"], inventory.canonical_json_bytes(offline))

        def add_command(argv: tuple[str, ...], stdout: bytes) -> None:
            self.commands[argv] = inventory.CommandObservation(argv, 0, False, stdout)

        release = spec["expected"]["kernelRelease"]
        package = spec["expected"]["kernelPackage"]
        add_command(("/usr/bin/uname", "-r"), f"{release}\n".encode())
        add_command(
            (
                "/usr/bin/dpkg-query",
                "-W",
                "-f=${db:Status-Abbrev}\\t${binary:Package}\\t${Version}",
                package,
            ),
            f"ii \t{package}\t5.15.0-100.110\n".encode(),
        )
        add_command((paths["python"], "--version"), b"Python 3.11.18\n")
        for name, path in paths["runtime"].items():
            output = f"{name} 1.0.0\n".encode()
            if name == "runc":
                output += b"commit: synthetic\nspec: 1.0.2\n"
            add_command((path, "--version"), output)
        for name, path in cni["plugins"].items():
            add_command((path, "--version"), f"{name} 1.0.0\n".encode())

    def read_file(self, path: str, *, maximum_bytes: int) -> inventory.FileObservation:
        value = self.files[path]
        if value.raw is not None:
            assert len(value.raw) <= maximum_bytes
        return value

    def execute(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> inventory.CommandObservation:
        assert timeout_seconds == inventory.COMMAND_TIMEOUT_SECONDS
        value = self.commands[argv]
        assert len(value.stdout) + len(value.stderr) <= maximum_output_bytes
        return value


def test_extract_inventory_reconstructs_exact_guest_manifest() -> None:
    spec = _spec()
    manifest, receipt = inventory.extract_inventory(spec, adapter=_Adapter(spec))
    authority = spec["authority"]

    assert manifest["image"]["sha256"] == authority["imageSha256"]
    assert manifest["python"]["version"] == "3.11.18"
    assert manifest["runtime"]["runc"]["version"] == "runc 1.0.0"
    assert [row["name"] for row in manifest["cni"]["plugins"]] == list(inventory.CNI_NAMES)
    normalized = capsule.validate_guest_manifest(
        manifest,
        image_digest=authority["imageSha256"],
        helper_digest=authority["helperSha256"],
        wheelhouse_manifest_digest=authority["wheelhouseManifestSha256"],
    )
    manifest_digest = _digest(capsule.canonical_json_bytes(normalized))
    assert (
        inventory.verify_receipt(
            receipt,
            expected_build_plan_digest=authority["buildPlanDigest"],
            expected_manifest_digest=manifest_digest,
        )
        == receipt
    )
    assert len(receipt["files"]) == 15
    assert len(receipt["commands"]) == 13


@pytest.mark.parametrize(
    "field,value",
    [
        ("lane", "lane-c"),
        ("sourceCommit", "short"),
        ("imageSha256", "sha256:bad"),
    ],
)
def test_validate_spec_rejects_bad_authority(field: str, value: str) -> None:
    spec = _spec()
    spec["authority"][field] = value
    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.validate_spec(spec)


def test_inventory_rejects_os_kernel_python_and_offline_drift() -> None:
    cases = []
    for mutate in (
        lambda adapter, _spec_value: adapter.files.__setitem__(
            "/usr/lib/os-release",
            inventory.FileObservation(
                "/usr/lib/os-release", b'ID=debian\nVERSION_ID="12"\n', 0o644, 0, 0
            ),
        ),
        lambda adapter, _spec_value: adapter.commands.__setitem__(
            ("/usr/bin/uname", "-r"),
            inventory.CommandObservation(("/usr/bin/uname", "-r"), 0, False, b"wrong-release\n"),
        ),
        lambda adapter, spec: adapter.commands.__setitem__(
            (spec["paths"]["python"], "--version"),
            inventory.CommandObservation(
                (spec["paths"]["python"], "--version"),
                0,
                False,
                b"Python 3.12.1\n",
            ),
        ),
        lambda adapter, spec: adapter.files.__setitem__(
            spec["paths"]["offlineEvidence"],
            inventory.FileObservation(spec["paths"]["offlineEvidence"], b"{}\n", 0o644, 0, 0),
        ),
    ):
        spec = _spec()
        adapter = _Adapter(spec)
        mutate(adapter, spec)
        cases.append((spec, adapter))

    for spec, adapter in cases:
        with pytest.raises(inventory.GuestInventoryPolicyError, match="failed closed"):
            inventory.extract_inventory(spec, adapter=adapter)


@pytest.mark.parametrize("failure", ["symlink", "writable", "nonexec", "oversize"])
def test_inventory_rejects_unsafe_file_observation(failure: str) -> None:
    spec = _spec()
    adapter = _Adapter(spec)
    path = spec["paths"]["python"]
    original = adapter.files[path]
    if failure == "symlink":
        adapter.files[path] = inventory.FileObservation(
            path, original.raw, original.mode, 0, 0, symlink=True
        )
    elif failure == "writable":
        adapter.files[path] = inventory.FileObservation(path, original.raw, 0o777, 0, 0)
    elif failure == "nonexec":
        adapter.files[path] = inventory.FileObservation(path, original.raw, 0o644, 0, 0)
    else:
        adapter.files[path] = inventory.FileObservation(
            path, b"x" * (inventory.MAX_FILE_BYTES + 1), 0o755, 0, 0
        )
    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.extract_inventory(spec, adapter=adapter)


def test_validate_spec_requires_canonical_regular_os_release_target() -> None:
    spec = _spec()
    spec["paths"]["osRelease"] = "/etc/os-release"

    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.validate_spec(spec)


def test_inventory_accepts_digest_only_large_executable_observations() -> None:
    spec = _spec()
    expected_manifest, _ = inventory.extract_inventory(spec, adapter=_Adapter(spec))
    adapter = _Adapter(spec)
    executable_paths = [
        spec["paths"]["kernelImage"],
        spec["paths"]["python"],
        *spec["paths"]["runtime"].values(),
        *spec["paths"]["cni"]["plugins"].values(),
    ]
    for path in executable_paths:
        original = adapter.files[path]
        assert original.raw is not None
        adapter.files[path] = inventory.FileObservation(
            path=path,
            raw=None,
            mode=original.mode,
            uid=original.uid,
            gid=original.gid,
            sha256=_digest(original.raw),
            size=inventory.MAX_FILE_BYTES + 1,
        )

    manifest, receipt = inventory.extract_inventory(spec, adapter=adapter)

    assert manifest == expected_manifest
    assert any(
        row["path"] == spec["paths"]["runtime"]["containerd"]
        and row["size"] == inventory.MAX_FILE_BYTES + 1
        for row in receipt["files"]
    )


@pytest.mark.parametrize(
    "path_selector",
    [
        lambda spec: spec["paths"]["osRelease"],
        lambda spec: spec["paths"]["cni"]["config"],
        lambda spec: spec["paths"]["offlineEvidence"],
    ],
)
def test_inventory_rejects_digest_only_content_required_observations(path_selector) -> None:
    spec = _spec()
    adapter = _Adapter(spec)
    path = path_selector(spec)
    original = adapter.files[path]
    assert original.raw is not None
    adapter.files[path] = inventory.FileObservation(
        path=path,
        raw=None,
        mode=original.mode,
        uid=original.uid,
        gid=original.gid,
        sha256=_digest(original.raw),
        size=len(original.raw),
    )

    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.extract_inventory(spec, adapter=adapter)


@pytest.mark.parametrize("field", ["sha256", "size"])
def test_inventory_rejects_raw_digest_metadata_mismatch(field: str) -> None:
    spec = _spec()
    adapter = _Adapter(spec)
    path = spec["paths"]["python"]
    original = adapter.files[path]
    assert original.raw is not None
    values = {
        "sha256": _digest(original.raw),
        "size": len(original.raw),
    }
    values[field] = _digest(b"wrong") if field == "sha256" else len(original.raw) + 1
    adapter.files[path] = inventory.FileObservation(
        path=path,
        raw=original.raw,
        mode=original.mode,
        uid=original.uid,
        gid=original.gid,
        sha256=values["sha256"],
        size=values["size"],
    )

    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.extract_inventory(spec, adapter=adapter)


@pytest.mark.parametrize("size", [0, inventory.MAX_DIGEST_FILE_BYTES + 1])
def test_inventory_rejects_digest_only_observation_outside_size_bound(size: int) -> None:
    spec = _spec()
    adapter = _Adapter(spec)
    path = spec["paths"]["python"]
    original = adapter.files[path]
    assert original.raw is not None
    adapter.files[path] = inventory.FileObservation(
        path=path,
        raw=None,
        mode=original.mode,
        uid=original.uid,
        gid=original.gid,
        sha256=_digest(original.raw),
        size=size,
    )

    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.extract_inventory(spec, adapter=adapter)


@pytest.mark.parametrize("failure", ["exit", "timeout", "identity", "oversize"])
def test_inventory_rejects_unsafe_command_observation(failure: str) -> None:
    spec = _spec()
    adapter = _Adapter(spec)
    argv = ("/usr/bin/uname", "-r")
    if failure == "exit":
        adapter.commands[argv] = inventory.CommandObservation(argv, 1, False, b"bad")
    elif failure == "timeout":
        adapter.commands[argv] = inventory.CommandObservation(argv, 0, True, b"bad")
    elif failure == "identity":
        adapter.commands[argv] = inventory.CommandObservation(("/bin/uname",), 0, False, b"bad")
    else:
        adapter.commands[argv] = inventory.CommandObservation(
            argv,
            0,
            False,
            b"x" * (inventory.MAX_COMMAND_OUTPUT_BYTES + 1),
        )
    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.extract_inventory(spec, adapter=adapter)


def test_receipt_tampering_and_manifest_mismatch_fail_closed() -> None:
    spec = _spec()
    manifest, receipt = inventory.extract_inventory(spec, adapter=_Adapter(spec))
    manifest_digest = _digest(capsule.canonical_json_bytes(manifest))
    tampered = copy.deepcopy(receipt)
    tampered["files"][0]["path"] = "/etc/other"
    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.verify_receipt(
            tampered,
            expected_build_plan_digest=spec["authority"]["buildPlanDigest"],
            expected_manifest_digest=manifest_digest,
        )
    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.verify_receipt(
            receipt,
            expected_build_plan_digest=_digest(b"other-plan"),
            expected_manifest_digest=manifest_digest,
        )


def test_verify_manifest_rejects_wrong_authority() -> None:
    spec = _spec()
    manifest, _ = inventory.extract_inventory(spec, adapter=_Adapter(spec))
    with pytest.raises(inventory.GuestInventoryPolicyError):
        inventory.verify_manifest(
            manifest,
            image_digest=_digest(b"wrong"),
            helper_digest=spec["authority"]["helperSha256"],
            wheelhouse_manifest_digest=spec["authority"]["wheelhouseManifestSha256"],
        )
