#!/usr/bin/env python3
"""Independently reconstruct a K1s Q1 guest manifest from bounded evidence.

The extractor has no image-builder dependency and never invokes a shell.  A
future guest adapter must provide exact regular-file observations and bounded
argv results.  B0 tests use a synthetic adapter; the CLI verifies canonical
manifests and extraction receipts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, cast

try:
    from scripts.qualification import kp1_q1_capsule as q1_capsule
except ModuleNotFoundError:  # Direct execution from scripts/qualification.
    import kp1_q1_capsule as q1_capsule  # type: ignore[import-not-found,no-redef]

API_VERSION = "k1s.dev/q1-guest-inventory/v1"
OFFLINE_API_VERSION = "k1s.dev/q1-guest-offline-evidence/v1"
SCHEMA_VERSION = 1
SPEC_KIND = "K1sQ1GuestInventorySpec"
RECEIPT_KIND = "K1sQ1GuestInventoryReceipt"
OFFLINE_KIND = "K1sQ1GuestOfflineEvidence"

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
COMMAND_TIMEOUT_SECONDS = 15

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
OBJECT_RE = re.compile(r"[0-9a-f]{40}")
LANE_RE = re.compile(r"lane-[ab]")
PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,126}")
RELEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,126}")
VERSION_RE = re.compile(r"[^\x00\r\n]{1,128}")
RUNTIME_NAMES = ("containerd", "crictl", "docker", "podman", "runc")
CNI_NAMES = ("bridge", "portmap", "firewall", "tuning", "loopback")
PROTECTED_NAMES = {
    ".env",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    ".envrc",
    "ae-ha-core.env",
    "ae.env",
}


class GuestInventoryPolicyError(RuntimeError):
    """Guest inventory evidence was unsafe, ambiguous, or incomplete."""


def _fail() -> GuestInventoryPolicyError:
    return GuestInventoryPolicyError("K1s Q1 guest inventory failed closed")


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


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _canonical_json(raw: bytes) -> dict[str, object]:
    if not 1 <= len(raw) <= MAX_FILE_BYTES:
        raise _fail()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return cast(dict[str, object], value)


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _object_id(value: object) -> str:
    if type(value) is not str or OBJECT_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    return cast(dict[str, object], value)


def _bind(value: Mapping[str, object]) -> dict[str, object]:
    if "receiptDigest" in value:
        raise _fail()
    result = dict(value)
    result["receiptDigest"] = _digest_bytes(canonical_json_bytes(result))
    return result


def _safe_path(value: object, *, roots: tuple[str, ...]) -> str:
    if type(value) is not str or not value.startswith("/") or value in {"/", "//"}:
        raise _fail()
    if value.endswith("/") or "\\" in value or "\x00" in value:
        raise _fail()
    pure = PurePosixPath(value)
    if pure.as_posix() != value or any(part in {"", ".", ".."} for part in pure.parts):
        raise _fail()
    if pure.name in PROTECTED_NAMES or not any(value.startswith(root) for root in roots):
        raise _fail()
    return value


def _version(value: object) -> str:
    if type(value) is not str or VERSION_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _authority(value: object) -> dict[str, object]:
    authority = _closed(
        value,
        {
            "buildPlanDigest",
            "helperSha256",
            "imageSha256",
            "lane",
            "requirementsLockSha256",
            "sourceCommit",
            "sourceTree",
            "wheelhouseManifestSha256",
        },
    )
    for key in (
        "buildPlanDigest",
        "helperSha256",
        "imageSha256",
        "requirementsLockSha256",
        "wheelhouseManifestSha256",
    ):
        _digest(authority[key])
    _object_id(authority["sourceCommit"])
    _object_id(authority["sourceTree"])
    lane = authority["lane"]
    if type(lane) is not str or LANE_RE.fullmatch(lane) is None:
        raise _fail()
    return authority


@dataclass(frozen=True, slots=True)
class FileObservation:
    path: str
    raw: bytes
    mode: int
    uid: int
    gid: int
    regular: bool = True
    symlink: bool = False
    link_count: int = 1


@dataclass(frozen=True, slots=True)
class CommandObservation:
    argv: tuple[str, ...]
    exit_code: int
    timed_out: bool
    stdout: bytes = b""
    stderr: bytes = b""


class InventoryAdapter(Protocol):
    def read_file(self, path: str, *, maximum_bytes: int) -> FileObservation: ...

    def execute(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> CommandObservation: ...


def validate_spec(value: object) -> dict[str, object]:
    spec = _closed(
        value,
        {
            "apiVersion",
            "authority",
            "expected",
            "kind",
            "paths",
            "schemaVersion",
        },
    )
    if (
        spec["apiVersion"] != API_VERSION
        or spec["kind"] != SPEC_KIND
        or spec["schemaVersion"] != SCHEMA_VERSION
    ):
        raise _fail()
    authority = _authority(spec["authority"])
    expected = _closed(spec["expected"], {"kernelPackage", "kernelRelease"})
    if (
        type(expected["kernelPackage"]) is not str
        or PACKAGE_RE.fullmatch(expected["kernelPackage"]) is None
        or type(expected["kernelRelease"]) is not str
        or RELEASE_RE.fullmatch(expected["kernelRelease"]) is None
    ):
        raise _fail()
    paths = _closed(
        spec["paths"],
        {"cni", "kernelImage", "offlineEvidence", "osRelease", "python", "runtime"},
    )
    kernel_path = f"/boot/vmlinuz-{expected['kernelRelease']}"
    if paths["kernelImage"] != kernel_path or paths["osRelease"] != "/etc/os-release":
        raise _fail()
    paths["python"] = _safe_path(
        paths["python"],
        roots=("/usr/bin/", "/usr/local/bin/", "/opt/k1s-q1/"),
    )
    paths["kernelImage"] = _safe_path(paths["kernelImage"], roots=("/boot/",))
    paths["osRelease"] = _safe_path(paths["osRelease"], roots=("/etc/",))
    paths["offlineEvidence"] = _safe_path(
        paths["offlineEvidence"],
        roots=("/etc/k1s-q1/",),
    )
    runtime = _closed(paths["runtime"], set(RUNTIME_NAMES))
    for name in RUNTIME_NAMES:
        runtime[name] = _safe_path(
            runtime[name],
            roots=("/usr/bin/", "/usr/local/bin/", "/usr/sbin/", "/usr/local/sbin/"),
        )
    cni = _closed(paths["cni"], {"config", "plugins"})
    cni["config"] = _safe_path(cni["config"], roots=("/etc/cni/net.d/",))
    plugins = _closed(cni["plugins"], set(CNI_NAMES))
    for name in CNI_NAMES:
        path = _safe_path(plugins[name], roots=("/opt/cni/bin/",))
        if PurePosixPath(path).name != name:
            raise _fail()
        plugins[name] = path
    return {
        **spec,
        "authority": authority,
        "expected": expected,
        "paths": {**paths, "cni": {**cni, "plugins": plugins}, "runtime": runtime},
    }


def _file(
    adapter: InventoryAdapter,
    path: str,
    *,
    executable: bool = False,
) -> tuple[bytes, dict[str, object]]:
    try:
        observed = adapter.read_file(path, maximum_bytes=MAX_FILE_BYTES)
    except Exception as exc:
        raise _fail() from exc
    if (
        type(observed) is not FileObservation
        or observed.path != path
        or type(observed.raw) is not bytes
        or type(observed.regular) is not bool
        or not observed.regular
        or type(observed.symlink) is not bool
        or observed.symlink
        or type(observed.link_count) is not int
        or observed.link_count != 1
        or not 1 <= len(observed.raw) <= MAX_FILE_BYTES
        or type(observed.mode) is not int
        or not 0 <= observed.mode <= 0o777
        or observed.mode & 0o022
        or type(observed.uid) is not int
        or observed.uid < 0
        or type(observed.gid) is not int
        or observed.gid < 0
    ):
        raise _fail()
    if executable and not observed.mode & 0o100:
        raise _fail()
    if not executable and observed.mode & 0o111:
        raise _fail()
    evidence = {
        "mode": f"{observed.mode:04o}",
        "path": path,
        "sha256": _digest_bytes(observed.raw),
        "size": len(observed.raw),
    }
    return observed.raw, evidence


def _command(
    adapter: InventoryAdapter,
    argv: tuple[str, ...],
) -> tuple[str, dict[str, object]]:
    if not argv or any(not item or "\x00" in item for item in argv):
        raise _fail()
    try:
        observed = adapter.execute(
            argv,
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
            maximum_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
        )
    except Exception as exc:
        raise _fail() from exc
    if (
        type(observed) is not CommandObservation
        or type(observed.argv) is not tuple
        or observed.argv != argv
        or type(observed.exit_code) is not int
        or observed.exit_code != 0
        or type(observed.timed_out) is not bool
        or observed.timed_out
        or type(observed.stdout) is not bytes
        or type(observed.stderr) is not bytes
        or len(observed.stdout) + len(observed.stderr) > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise _fail()
    selected = observed.stdout.strip() or observed.stderr.strip()
    try:
        text = selected.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail() from exc
    if "\x00" in text:
        raise _fail()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise _fail()
    version = _version(lines[0])
    evidence = {
        "argv": list(argv),
        "stderrSha256": _digest_bytes(observed.stderr),
        "stderrSize": len(observed.stderr),
        "stdoutSha256": _digest_bytes(observed.stdout),
        "stdoutSize": len(observed.stdout),
    }
    return version, evidence


def _os_release(raw: bytes) -> tuple[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail() from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if not separator or key in values or not re.fullmatch(r"[A-Z0-9_]+", key):
            raise _fail()
        try:
            parsed = shlex.split(raw_value, posix=True)
        except ValueError as exc:
            raise _fail() from exc
        if len(parsed) != 1:
            raise _fail()
        values[key] = parsed[0]
    if values.get("ID") != "ubuntu" or values.get("VERSION_ID") != "22.04":
        raise _fail()
    return "ubuntu", "22.04"


def _offline_evidence(value: object, authority: Mapping[str, object]) -> dict[str, object]:
    item = _closed(
        value,
        {"apiVersion", "helper", "images", "kind", "registry", "schemaVersion", "wheelhouse"},
    )
    if (
        item["apiVersion"] != OFFLINE_API_VERSION
        or item["kind"] != OFFLINE_KIND
        or item["schemaVersion"] != SCHEMA_VERSION
    ):
        raise _fail()
    helper = _closed(item["helper"], {"sha256"})
    if _digest(helper["sha256"]) != authority["helperSha256"]:
        raise _fail()
    wheelhouse = _closed(item["wheelhouse"], {"manifestSha256", "requirementsLockSha256"})
    if (
        _digest(wheelhouse["manifestSha256"]) != authority["wheelhouseManifestSha256"]
        or _digest(wheelhouse["requirementsLockSha256"]) != authority["requirementsLockSha256"]
    ):
        raise _fail()
    registry = _closed(item["registry"], {"archiveSha256", "imageDigest"})
    _digest(registry["archiveSha256"])
    _digest(registry["imageDigest"])
    images = item["images"]
    if type(images) is not list or not 1 <= len(images) <= 64:
        raise _fail()
    normalized: list[dict[str, str]] = []
    for raw in images:
        image = _closed(raw, {"archiveSha256", "manifestDigest", "name"})
        name = image["name"]
        if (
            type(name) is not str
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?", name) is None
        ):
            raise _fail()
        normalized.append(
            {
                "archiveSha256": _digest(image["archiveSha256"]),
                "manifestDigest": _digest(image["manifestDigest"]),
                "name": name,
            }
        )
    if [row["name"] for row in normalized] != sorted(
        {row["name"] for row in normalized}, key=lambda name: name.encode("utf-8")
    ):
        raise _fail()
    return {**item, "images": normalized}


def _validate_cni_config(raw: bytes) -> None:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or value.get("cniVersion") != "1.0.0":
        raise _fail()
    plugins = value.get("plugins")
    if type(plugins) is not list or len(plugins) != 4:
        raise _fail()
    types: list[str] = []
    for plugin in plugins:
        if type(plugin) is not dict or type(plugin.get("type")) is not str:
            raise _fail()
        types.append(cast(str, plugin["type"]))
    if types != list(CNI_NAMES[:-1]):
        raise _fail()


def extract_inventory(
    spec_value: object,
    *,
    adapter: InventoryAdapter,
) -> tuple[dict[str, object], dict[str, object]]:
    spec = validate_spec(spec_value)
    authority = cast(dict[str, object], spec["authority"])
    expected = cast(dict[str, object], spec["expected"])
    paths = cast(dict[str, object], spec["paths"])
    files: list[dict[str, object]] = []
    commands: list[dict[str, object]] = []

    os_raw, evidence = _file(adapter, cast(str, paths["osRelease"]))
    files.append(evidence)
    os_id, os_version = _os_release(os_raw)
    os_release_sha = _digest_bytes(os_raw)

    kernel_raw, evidence = _file(adapter, cast(str, paths["kernelImage"]))
    files.append(evidence)
    kernel_release, evidence = _command(adapter, ("/usr/bin/uname", "-r"))
    commands.append(evidence)
    if kernel_release != expected["kernelRelease"]:
        raise _fail()
    package_status, evidence = _command(
        adapter,
        (
            "/usr/bin/dpkg-query",
            "-W",
            "-f=${db:Status-Abbrev}\\t${binary:Package}\\t${Version}",
            cast(str, expected["kernelPackage"]),
        ),
    )
    commands.append(evidence)
    status_parts = package_status.split("\t")
    if (
        len(status_parts) != 3
        or status_parts[0] != "ii "
        or status_parts[1] != expected["kernelPackage"]
        or not status_parts[2]
    ):
        raise _fail()

    python_path = cast(str, paths["python"])
    python_raw, evidence = _file(adapter, python_path, executable=True)
    files.append(evidence)
    python_version, evidence = _command(adapter, (python_path, "--version"))
    commands.append(evidence)
    match = re.fullmatch(r"Python (3\.11\.[0-9]+)", python_version)
    if match is None:
        raise _fail()

    runtime_paths = cast(dict[str, object], paths["runtime"])
    runtime: dict[str, dict[str, str]] = {}
    for name in RUNTIME_NAMES:
        component_path = cast(str, runtime_paths[name])
        component_raw, evidence = _file(adapter, component_path, executable=True)
        files.append(evidence)
        version, evidence = _command(adapter, (component_path, "--version"))
        commands.append(evidence)
        runtime[name] = {"sha256": _digest_bytes(component_raw), "version": version}

    cni_paths = cast(dict[str, object], paths["cni"])
    cni_raw, evidence = _file(adapter, cast(str, cni_paths["config"]))
    files.append(evidence)
    _validate_cni_config(cni_raw)
    cni_plugins: list[dict[str, str]] = []
    plugin_paths = cast(dict[str, object], cni_paths["plugins"])
    for name in CNI_NAMES:
        plugin_path = cast(str, plugin_paths[name])
        plugin_raw, evidence = _file(adapter, plugin_path, executable=True)
        files.append(evidence)
        version, evidence = _command(adapter, (plugin_path, "--version"))
        commands.append(evidence)
        cni_plugins.append({"name": name, "sha256": _digest_bytes(plugin_raw), "version": version})

    offline_raw, evidence = _file(adapter, cast(str, paths["offlineEvidence"]))
    files.append(evidence)
    offline = _offline_evidence(_canonical_json(offline_raw), authority)
    manifest: dict[str, object] = {
        "apiVersion": q1_capsule.API_VERSION,
        "cni": {
            "configTemplateSha256": _digest_bytes(cni_raw),
            "plugins": cni_plugins,
            "version": "1.0.0",
        },
        "helper": offline["helper"],
        "image": {"format": "qcow2", "sha256": authority["imageSha256"]},
        "images": offline["images"],
        "kernel": {
            "imageSha256": _digest_bytes(kernel_raw),
            "package": expected["kernelPackage"],
            "release": kernel_release,
        },
        "kind": q1_capsule.GUEST_MANIFEST_KIND,
        "os": {"id": os_id, "releaseSha256": os_release_sha, "version": os_version},
        "python": {
            "executableSha256": _digest_bytes(python_raw),
            "version": match.group(1),
        },
        "registry": offline["registry"],
        "runtime": runtime,
        "schemaVersion": q1_capsule.SCHEMA_VERSION,
        "wheelhouse": offline["wheelhouse"],
    }
    try:
        normalized = q1_capsule.validate_guest_manifest(
            manifest,
            image_digest=cast(str, authority["imageSha256"]),
            helper_digest=cast(str, authority["helperSha256"]),
            wheelhouse_manifest_digest=cast(str, authority["wheelhouseManifestSha256"]),
        )
    except Exception as exc:
        raise _fail() from exc
    manifest_raw = q1_capsule.canonical_json_bytes(normalized)
    receipt: dict[str, object] = {
        "apiVersion": API_VERSION,
        "authority": authority,
        "commands": commands,
        "files": files,
        "kind": RECEIPT_KIND,
        "manifestSha256": _digest_bytes(manifest_raw),
        "manifestSize": len(manifest_raw),
        "qualificationPosture": "source-validated/unqualified",
        "schemaVersion": SCHEMA_VERSION,
        "status": "inventory-verified",
    }
    return normalized, _bind(receipt)


def verify_manifest(
    value: object,
    *,
    image_digest: object,
    helper_digest: object,
    wheelhouse_manifest_digest: object,
) -> dict[str, object]:
    try:
        return q1_capsule.validate_guest_manifest(
            value,
            image_digest=_digest(image_digest),
            helper_digest=_digest(helper_digest),
            wheelhouse_manifest_digest=_digest(wheelhouse_manifest_digest),
        )
    except Exception as exc:
        raise _fail() from exc


def verify_receipt(
    value: object,
    *,
    expected_build_plan_digest: object,
    expected_manifest_digest: object,
) -> dict[str, object]:
    keys = {
        "apiVersion",
        "authority",
        "commands",
        "files",
        "kind",
        "manifestSha256",
        "manifestSize",
        "qualificationPosture",
        "schemaVersion",
        "status",
    }
    receipt = _closed(value, {*keys, "receiptDigest"})
    unsigned = dict(receipt)
    actual = _digest(unsigned.pop("receiptDigest"))
    if actual != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != RECEIPT_KIND
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or receipt["status"] != "inventory-verified"
        or receipt["qualificationPosture"] != "source-validated/unqualified"
        or receipt["manifestSha256"] != _digest(expected_manifest_digest)
    ):
        raise _fail()
    authority = _authority(receipt["authority"])
    if authority["buildPlanDigest"] != _digest(expected_build_plan_digest):
        raise _fail()
    if (
        type(receipt["manifestSize"]) is not int
        or not 1 <= receipt["manifestSize"] <= MAX_FILE_BYTES
    ):
        raise _fail()
    files = receipt["files"]
    commands = receipt["commands"]
    if (
        type(files) is not list
        or len(files) != 15
        or type(commands) is not list
        or len(commands) != 13
    ):
        raise _fail()
    file_paths: set[str] = set()
    for raw in files:
        row = _closed(raw, {"mode", "path", "sha256", "size"})
        if type(row["mode"]) is not str or re.fullmatch(r"0[0-7]{3}", row["mode"]) is None:
            raise _fail()
        path = _safe_path(
            row["path"],
            roots=("/boot/", "/etc/", "/opt/", "/usr/"),
        )
        if path in file_paths:
            raise _fail()
        file_paths.add(path)
        _digest(row["sha256"])
        if int(row["mode"], 8) & 0o022:
            raise _fail()
        if type(row["size"]) is not int or not 1 <= row["size"] <= MAX_FILE_BYTES:
            raise _fail()
    command_argvs: set[tuple[str, ...]] = set()
    for raw in commands:
        row = _closed(raw, {"argv", "stderrSha256", "stderrSize", "stdoutSha256", "stdoutSize"})
        argv = row["argv"]
        if (
            type(argv) is not list
            or not argv
            or any(type(item) is not str or not item or "\x00" in item for item in argv)
        ):
            raise _fail()
        argv_tuple = tuple(argv)
        if argv_tuple in command_argvs:
            raise _fail()
        command_argvs.add(argv_tuple)
        _digest(row["stderrSha256"])
        _digest(row["stdoutSha256"])
        for key in ("stderrSize", "stdoutSize"):
            if (
                type(row[key]) is not int
                or not 0 <= cast(int, row[key]) <= MAX_COMMAND_OUTPUT_BYTES
            ):
                raise _fail()
    return receipt


def _read_canonical(path: str, expected_digest: object) -> dict[str, object]:
    if not os.path.isabs(path) or PurePosixPath(path).name in PROTECTED_NAMES:
        raise _fail()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        if not 1 <= opened.st_size <= MAX_FILE_BYTES:
            raise _fail()
        raw = b""
        while len(raw) < opened.st_size:
            chunk = os.read(descriptor, opened.st_size - len(raw))
            if not chunk:
                raise _fail()
            raw += chunk
        if os.read(descriptor, 1) or _digest_bytes(raw) != _digest(expected_digest):
            raise _fail()
        return _canonical_json(raw)
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    manifest = sub.add_parser("verify-manifest", help="verify a canonical Q1 guest manifest")
    manifest.add_argument("--manifest", required=True)
    manifest.add_argument("--expect-manifest-sha256", required=True)
    manifest.add_argument("--image-sha256", required=True)
    manifest.add_argument("--helper-sha256", required=True)
    manifest.add_argument("--wheelhouse-manifest-sha256", required=True)
    receipt = sub.add_parser("verify-receipt", help="verify an inventory receipt")
    receipt.add_argument("--receipt", required=True)
    receipt.add_argument("--expect-receipt-sha256", required=True)
    receipt.add_argument("--expect-build-plan-digest", required=True)
    receipt.add_argument("--expect-manifest-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-manifest":
            value = _read_canonical(args.manifest, args.expect_manifest_sha256)
            normalized = verify_manifest(
                value,
                image_digest=args.image_sha256,
                helper_digest=args.helper_sha256,
                wheelhouse_manifest_digest=args.wheelhouse_manifest_sha256,
            )
            print(
                json.dumps(
                    {
                        "manifestSha256": _digest_bytes(canonical_json_bytes(normalized)),
                        "status": "verified",
                    },
                    sort_keys=True,
                )
            )
            return 0
        value = _read_canonical(args.receipt, args.expect_receipt_sha256)
        receipt = verify_receipt(
            value,
            expected_build_plan_digest=args.expect_build_plan_digest,
            expected_manifest_digest=args.expect_manifest_digest,
        )
        print(
            json.dumps(
                {"receiptDigest": receipt["receiptDigest"], "status": "verified"},
                sort_keys=True,
            )
        )
        return 0
    except GuestInventoryPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
