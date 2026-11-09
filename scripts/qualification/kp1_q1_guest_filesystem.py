#!/usr/bin/env python3
"""Validate complete semantic filesystem manifests for Q1 guest images.

The manifest intentionally omits physical filesystem layout and time fields.
It binds every persistent path by type, ownership, mode, content or link/device
identity, and extended attributes.  A narrow, versioned policy identifies
runtime mounts and first-boot state that are outside the reusable image
contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import cast

API_VERSION = "k1s.dev/q1-guest-filesystem-manifest/v1"
SCHEMA_VERSION = 1
KIND = "K1sQ1GuestFilesystemManifest"
MAX_ENTRIES = 200_000
MAX_REGULAR_BYTES = 16 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 48 * 1024 * 1024

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
MODE_RE = re.compile(r"0[0-7]{3}")
XATTR_RE = re.compile(r"[A-Za-z0-9_.-]{1,255}")

# All exclusions are exact paths or explicit subtrees.  Broad classes such as
# /etc, /opt, /usr, or /var are deliberately forbidden.  The policy itself is
# embedded in, and digest-bound by, every filesystem manifest.
POLICY: dict[str, object] = {
    "includedRoot": "/",
    "excludedMountTrees": ["/dev", "/proc", "/run", "/snap", "/sys"],
    "ephemeralExactPaths": [
        "/etc/machine-id",
        "/var/lib/dbus/machine-id",
        "/var/lib/snapd/state.json",
        "/var/lib/systemd/random-seed",
        "/var/lib/update-notifier/updates-available",
    ],
    "ephemeralTrees": [
        "/tmp",  # noqa: S108 - guest policy path, not a host temporary file
        "/var/cache",
        "/var/lib/cloud",
        "/var/lib/snapd/cgroup",
        "/var/lib/snapd/cookie",
        "/var/lib/snapd/device",
        "/var/lib/snapd/inhibit",
        "/var/log",
        "/var/tmp",  # noqa: S108 - guest policy path, not a host temporary file
    ],
    "ignoredMetadata": [
        "atime",
        "ctime",
        "filesystem-block-layout",
        "inode",
        "mtime",
    ],
}


class GuestFilesystemPolicyError(RuntimeError):
    """The complete guest filesystem evidence failed its closed contract."""


def _fail() -> GuestFilesystemPolicyError:
    return GuestFilesystemPolicyError("K1s Q1 guest filesystem manifest failed closed")


def canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    return (rendered + "\n").encode("utf-8")


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def policy_digest() -> str:
    return _digest_bytes(canonical_json_bytes(POLICY))


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    return cast(dict[str, object], value)


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _fail()
    return value


def _path(value: object) -> str:
    if type(value) is not str or not value.startswith("/") or value in {"/", "//"}:
        raise _fail()
    # Linux permits a literal backslash in a filename; JSON canonicalization
    # escapes it without changing the path identity.
    if value.endswith("/") or "\x00" in value:
        raise _fail()
    pure = PurePosixPath(value)
    if pure.as_posix() != value or any(part in {"", ".", ".."} for part in pure.parts):
        raise _fail()
    return value


def _xattrs(value: object) -> list[dict[str, object]]:
    if type(value) is not list or len(value) > 256:
        raise _fail()
    rows: list[dict[str, object]] = []
    names: list[str] = []
    for raw in value:
        row = _closed(raw, {"name", "sha256", "size"})
        name = row["name"]
        if type(name) is not str or XATTR_RE.fullmatch(name) is None:
            raise _fail()
        _digest(row["sha256"])
        size = _integer(row["size"])
        if size > 1024 * 1024:
            raise _fail()
        names.append(name)
        rows.append({"name": name, "sha256": row["sha256"], "size": size})
    if names != sorted(set(names), key=lambda item: item.encode("utf-8")):
        raise _fail()
    return rows


def _entry(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _fail()
    row = cast(dict[str, object], value)
    common = {"gid", "mode", "path", "type", "uid", "xattrs"}
    kind = row.get("type")
    extra: set[str]
    if kind == "directory":
        extra = set()
    elif kind == "regular":
        extra = {"linkCount", "sha256", "size"}
    elif kind == "symlink":
        extra = {"target"}
    elif kind in {"block", "character"}:
        extra = {"major", "minor"}
    else:
        raise _fail()
    item = _closed(row, common | extra)
    path = _path(item["path"])
    mode = item["mode"]
    if type(mode) is not str or MODE_RE.fullmatch(mode) is None:
        raise _fail()
    normalized: dict[str, object] = {
        "gid": _integer(item["gid"]),
        "mode": mode,
        "path": path,
        "type": kind,
        "uid": _integer(item["uid"]),
        "xattrs": _xattrs(item["xattrs"]),
    }
    if kind == "regular":
        size = _integer(item["size"])
        links = _integer(item["linkCount"])
        if links < 1 or size > MAX_REGULAR_BYTES:
            raise _fail()
        normalized.update({"linkCount": links, "sha256": _digest(item["sha256"]), "size": size})
    elif kind == "symlink":
        target = item["target"]
        if type(target) is not str or not target or "\x00" in target or len(target) > 4096:
            raise _fail()
        normalized["target"] = target
    elif kind in {"block", "character"}:
        normalized.update({"major": _integer(item["major"]), "minor": _integer(item["minor"])})
    return normalized


def validate_manifest(value: object) -> dict[str, object]:
    manifest = _closed(
        value,
        {
            "apiVersion",
            "entries",
            "entryCount",
            "kind",
            "policy",
            "policySha256",
            "regularBytes",
            "schemaVersion",
        },
    )
    if (
        manifest["apiVersion"] != API_VERSION
        or manifest["kind"] != KIND
        or manifest["schemaVersion"] != SCHEMA_VERSION
        or manifest["policy"] != POLICY
        or manifest["policySha256"] != policy_digest()
    ):
        raise _fail()
    entries = manifest["entries"]
    if type(entries) is not list or not 1 <= len(entries) <= MAX_ENTRIES:
        raise _fail()
    normalized = [_entry(raw) for raw in entries]
    paths = [cast(str, row["path"]) for row in normalized]
    if paths != sorted(set(paths), key=lambda item: item.encode("utf-8")):
        raise _fail()
    regular_bytes = sum(cast(int, row["size"]) for row in normalized if row["type"] == "regular")
    if regular_bytes > MAX_REGULAR_BYTES:
        raise _fail()
    if manifest["entryCount"] != len(normalized) or manifest["regularBytes"] != regular_bytes:
        raise _fail()
    result = {**manifest, "entries": normalized}
    if len(canonical_json_bytes(result)) > MAX_MANIFEST_BYTES:
        raise _fail()
    return result


def manifest_digest(value: object) -> str:
    return _digest_bytes(canonical_json_bytes(validate_manifest(value)))


def shared_artifacts(value: Mapping[str, object]) -> dict[str, object]:
    """Return the exact fields that must agree between independent lanes."""

    required = {
        "filesystemManifestSha256",
        "filesystemPolicySha256",
        "guestSemanticSha256",
    }
    if not required <= set(value):
        raise _fail()
    result = {key: _digest(value[key]) for key in sorted(required)}
    if result["filesystemPolicySha256"] != policy_digest():
        raise _fail()
    return result
