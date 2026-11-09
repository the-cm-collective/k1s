from __future__ import annotations

import copy
import hashlib

import pytest
from scripts.qualification import kp1_q1_guest_filesystem as filesystem


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _manifest() -> dict[str, object]:
    return {
        "apiVersion": filesystem.API_VERSION,
        "entries": [
            {
                "gid": 0,
                "mode": "0755",
                "path": "/opt/k1s-q1",
                "type": "directory",
                "uid": 0,
                "xattrs": [],
            },
            {
                "gid": 0,
                "linkCount": 1,
                "mode": "0644",
                "path": "/opt/k1s-q1/release.json",
                "sha256": _digest(b"release\n"),
                "size": len(b"release\n"),
                "type": "regular",
                "uid": 0,
                "xattrs": [{"name": "user.k1s", "sha256": _digest(b"bound"), "size": 5}],
            },
            {
                "gid": 0,
                "mode": "0777",
                "path": "/usr/local/bin/k1s",
                "target": "/opt/k1s-q1/venv/bin/ae",
                "type": "symlink",
                "uid": 0,
                "xattrs": [],
            },
        ],
        "entryCount": 3,
        "kind": filesystem.KIND,
        "policy": filesystem.POLICY,
        "policySha256": filesystem.policy_digest(),
        "regularBytes": len(b"release\n"),
        "schemaVersion": filesystem.SCHEMA_VERSION,
    }


def test_complete_filesystem_manifest_is_canonical_and_policy_bound() -> None:
    manifest = _manifest()
    assert filesystem.validate_manifest(manifest) == manifest
    assert filesystem.manifest_digest(manifest) == _digest(
        filesystem.canonical_json_bytes(manifest)
    )
    assert filesystem.policy_digest() == _digest(filesystem.canonical_json_bytes(filesystem.POLICY))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(policySha256=_digest(b"other")),
        lambda value: value.update(entryCount=2),
        lambda value: value.update(regularBytes=0),
        lambda value: value["entries"].reverse(),
        lambda value: value["entries"][1].update(mode="0600"),
        lambda value: value["entries"][1].update(sha256="bad"),
    ],
)
def test_complete_filesystem_manifest_rejects_contract_drift(mutation: object) -> None:
    manifest = copy.deepcopy(_manifest())
    mutation(manifest)
    if manifest["entries"][1].get("mode") == "0600":
        # A mode change is valid evidence and therefore changes the digest; it
        # must not be silently normalized by the validator.
        assert filesystem.validate_manifest(manifest) == manifest
        assert filesystem.manifest_digest(manifest) != filesystem.manifest_digest(_manifest())
        return
    with pytest.raises(filesystem.GuestFilesystemPolicyError, match="failed closed"):
        filesystem.validate_manifest(manifest)


def test_shared_artifacts_ignore_lane_image_identity_but_bind_semantics() -> None:
    common = {
        "filesystemManifestSha256": filesystem.manifest_digest(_manifest()),
        "filesystemPolicySha256": filesystem.policy_digest(),
        "guestSemanticSha256": _digest(b"guest-semantic"),
    }
    lane_a = {**common, "imageSha256": _digest(b"image-a")}
    lane_b = {**common, "imageSha256": _digest(b"image-b")}
    assert filesystem.shared_artifacts(lane_a) == filesystem.shared_artifacts(lane_b)

    lane_b["guestSemanticSha256"] = _digest(b"different")
    assert filesystem.shared_artifacts(lane_a) != filesystem.shared_artifacts(lane_b)


def test_policy_exclusions_remain_narrow_and_explicit() -> None:
    policy = filesystem.POLICY
    excluded = set(policy["excludedMountTrees"]) | set(policy["ephemeralTrees"])
    exact = set(policy["ephemeralExactPaths"])
    assert not excluded & {"/etc", "/opt", "/usr", "/var"}
    assert "/var/lib/cloud" in excluded
    assert "/var/lib/snapd/device" in excluded
    assert "/var/lib/snapd/state.json" in exact
    assert "/var/snap/lxd/common/lxd/unix.socket" in exact
    assert "/etc/ssh/ssh_host_ed25519_key" in exact
    assert not exact & {"/etc/ssh", "/var/snap/lxd"}
    assert policy["includedPersistentMountTrees"] == ["/boot/efi"]
