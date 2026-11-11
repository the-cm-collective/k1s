#!/usr/bin/env python3
"""Build the two offline QH5-B B3 guest-image lanes through QEMU TCG.

The host adapter never invokes a shell, never attaches a network device, and
never mounts a host path in the guest.  The retained B2 payload is exposed as
one read-only raw block device; a NoCloud seed contains only bounded bootstrap
instructions.  Inventory is returned over the serial port and independently
reconstructed by :mod:`kp1_q1_guest_inventory` on the host.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

try:
    from scripts.qualification import kp1_q1_base_acquisition as base_acquisition
    from scripts.qualification import kp1_q1_capsule as guest_capsule
    from scripts.qualification import kp1_q1_guest_bom as guest_bom
    from scripts.qualification import kp1_q1_guest_filesystem as guest_filesystem
    from scripts.qualification import kp1_q1_guest_image as guest_image
    from scripts.qualification import kp1_q1_guest_inputs as guest_inputs
    from scripts.qualification import kp1_q1_guest_inventory as guest_inventory
except ModuleNotFoundError:  # Direct execution from scripts/qualification.
    import kp1_q1_base_acquisition as base_acquisition  # type: ignore[import-not-found,no-redef]
    import kp1_q1_capsule as guest_capsule  # type: ignore[import-not-found,no-redef]
    import kp1_q1_guest_bom as guest_bom  # type: ignore[import-not-found,no-redef]
    import kp1_q1_guest_filesystem as guest_filesystem  # type: ignore[import-not-found,no-redef]
    import kp1_q1_guest_image as guest_image  # type: ignore[import-not-found,no-redef]
    import kp1_q1_guest_inputs as guest_inputs  # type: ignore[import-not-found,no-redef]
    import kp1_q1_guest_inventory as guest_inventory  # type: ignore[import-not-found,no-redef]

API_VERSION = "k1s.dev/q1-guest-live-builder/v1"
SCHEMA_VERSION = 1
FAILURE_KIND = "K1sQ1GuestLiveBuildFailure"
IMAGE_VIRTUAL_SIZE = 16 * 1024 * 1024 * 1024
MAX_SERIAL_BYTES = 64 * 1024 * 1024
MAX_FAILURE_TEXT = 4096
INVENTORY_PREFIX = b"K1S_Q1_INVENTORY:"
PROVISION_MARKER = b"K1S_Q1_PROVISION_OK"
INVENTORY_MARKER = b"K1S_Q1_INVENTORY_OK"
QEMU_MACHINE = "pc-i440fx-6.2,accel=tcg"
QEMU_CPU = "qemu64,-rdrand,-rdseed"
QEMU_MEMORY_MIB = 8192
QEMU_CPUS = 4
# Keep TCG virtual time near host monotonic time.  A fixed accelerated shift
# with sleep disabled can expire systemd device deadlines before host-backed
# virtio I/O and udev have completed, even though the kernel found the device.
QEMU_ICOUNT = "shift=auto,align=off,sleep=on"
QEMU_UUID = "00000000-0000-4000-8000-000000000001"
QEMU_RTC = "2026-12-03T00:00:00"

BOUND_SOURCE_MODULES = {
    "baseAcquisitionModule": base_acquisition,
    "guestBomModule": guest_bom,
    "filesystemManifestModule": guest_filesystem,
    "guestCapsuleModule": guest_capsule,
    "guestImageModule": guest_image,
    "guestInputsModule": guest_inputs,
    "inventoryModule": guest_inventory,
}

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SAFE_ENVIRONMENT = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "SOURCE_DATE_EPOCH": str(guest_image.SOURCE_DATE_EPOCH),
    "TZ": "UTC",
}


class GuestLiveBuildError(RuntimeError):
    """The B3 live builder rejected unsafe, ambiguous, or divergent state."""


def _fail() -> GuestLiveBuildError:
    return GuestLiveBuildError("K1s Q1 live guest build failed closed")


def _canonical_json_bytes(value: object) -> bytes:
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


def _canonical_json(raw: bytes, *, maximum: int = guest_image.MAX_JSON_BYTES) -> dict[str, object]:
    if not 1 <= len(raw) <= maximum:
        raise _fail()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or _canonical_json_bytes(value) != raw:
        raise _fail()
    return cast(dict[str, object], value)


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest_path(path: Path, *, maximum: int = guest_image.MAX_INPUT_BYTES) -> tuple[str, int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= maximum
            or mode & 0o022
        ):
            raise _fail()
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 1024 * 1024))
            if not block:
                raise _fail()
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise _fail()
        return f"sha256:{digest.hexdigest()}", opened.st_size
    finally:
        os.close(descriptor)


def _identity_path(plan: Mapping[str, object], group: str, name: str) -> Path:
    values = cast(dict[str, object], plan[group])
    identity = cast(dict[str, object], values[name])
    path = Path(cast(str, identity["path"]))
    current = guest_image.file_identity(
        str(path),
        expected_digest=identity["sha256"],
        executable=group == "tools",
    )
    if current.as_dict() != identity:
        raise _fail()
    return path


def _read_identity(
    plan: Mapping[str, object],
    name: str,
    *,
    maximum: int = guest_image.MAX_JSON_BYTES,
) -> bytes:
    path = _identity_path(plan, "inputs", name)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _fail() from exc
    if not 1 <= len(raw) <= maximum:
        raise _fail()
    return raw


def verify_executing_sources(plan: Mapping[str, object]) -> None:
    inputs = cast(dict[str, object], plan["inputs"])
    sources: dict[str, Path] = {}
    for name, module in BOUND_SOURCE_MODULES.items():
        module_path = getattr(module, "__file__", None)
        if type(module_path) is not str:
            raise _fail()
        sources[name] = Path(module_path)
    sources["liveBuilder"] = Path(__file__)
    for name, source_path in sources.items():
        identity = cast(dict[str, object], inputs[name])
        actual_digest, actual_size = _digest_path(source_path)
        if actual_digest != identity["sha256"] or actual_size != identity["size"]:
            raise _fail()


def _write_new(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
        )
    except OSError as exc:
        raise _fail() from exc
    try:
        written = 0
        while written < len(raw):
            count = os.write(descriptor, raw[written:])
            if count <= 0:
                raise _fail()
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class SubprocessRunner:
    def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        if not argv or timeout <= 0 or any(not item or "\x00" in item for item in argv):
            raise _fail()
        try:
            completed = subprocess.run(  # noqa: S603 - argv paths are plan-bound
                argv,
                check=False,
                capture_output=True,
                env=SAFE_ENVIRONMENT,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _fail() from exc
        if len(completed.stdout) + len(completed.stderr) > guest_image.MAX_STAGE_OUTPUT_BYTES:
            raise _fail()
        return CommandResult(argv, completed.returncode, completed.stdout, completed.stderr)


def _command(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    deadline: float,
) -> CommandResult:
    remaining = deadline - time.monotonic()
    result = runner.run(tuple(argv), timeout=remaining)
    if result.argv != tuple(argv) or result.returncode != 0:
        raise _fail()
    return result


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def render_provision_script(
    *,
    apt_package_count: int,
    payload_sha256: str,
    payload_size: int,
    member_manifest_sha256: str,
    guest_bundle: bytes,
    umask: str,
) -> bytes:
    if (
        type(apt_package_count) is not int
        or not 1 <= apt_package_count <= 512
        or DIGEST_RE.fullmatch(payload_sha256) is None
        or DIGEST_RE.fullmatch(member_manifest_sha256) is None
        or type(payload_size) is not int
        or payload_size <= 0
        or umask not in {value for _, value in guest_image.LANES}
        or not guest_bundle
    ):
        raise _fail()
    bundle_b64 = _b64(guest_bundle)
    payload_hex = payload_sha256.removeprefix("sha256:")
    manifest_hex = member_manifest_sha256.removeprefix("sha256:")
    script = f"""#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1
umask {umask}

quiesce_mutable_services() {{
  # The cloud image starts snapd/LXD and PackageKit before cloud-final.  Stop
  # their sockets and services so they cannot create or replace files while
  # the image tree is normalized below.  Some units are optional across base
  # image revisions, so a missing/inactive unit is not a normalization error.
  systemctl stop \
    snapd.socket snapd.service snapd.seeded.service \
    packagekit.service unattended-upgrades.service \
    'snap.*.service' || true
}}

quiesce_mutable_services

PAYLOAD_DEVICE=/dev/vdb
PAYLOAD_ROOT=/opt/k1s-q1/stage
test "$(blockdev --getsize64 "$PAYLOAD_DEVICE")" = "{payload_size}"
echo "{payload_hex}  $PAYLOAD_DEVICE" | sha256sum --check --status
install -d -m 0755 "$PAYLOAD_ROOT"
tar --extract --file "$PAYLOAD_DEVICE" --directory "$PAYLOAD_ROOT" --no-same-owner
echo "{manifest_hex}  $PAYLOAD_ROOT/B2-PAYLOAD-MANIFEST.json" | sha256sum --check --status

python3 - "$PAYLOAD_ROOT" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
raw = (root / "B2-PAYLOAD-MANIFEST.json").read_bytes()
value = json.loads(raw)
canonical = (
    json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\\n"
).encode()
if canonical != raw:
    raise SystemExit(41)
rows = value.get("members")
if not isinstance(rows, list):
    raise SystemExit(42)
expected = {{"B2-PAYLOAD-MANIFEST.json"}}
for row in rows:
    path = row["path"]
    expected.add(path)
    selected = root / path
    info = os.lstat(selected)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SystemExit(43)
    if info.st_size != row["size"] or stat.S_IMODE(info.st_mode) != int(row["mode"], 8):
        raise SystemExit(44)
    digest = hashlib.sha256()
    with selected.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if "sha256:" + digest.hexdigest() != row["sha256"]:
        raise SystemExit(45)
actual = {{str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}}
if actual != expected:
    raise SystemExit(46)
PY

cat >/usr/sbin/policy-rc.d <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 0755 /usr/sbin/policy-rc.d
install -d -m 0755 /opt/k1s-q1/empty-sources.list.d
install -m 0644 /dev/null /opt/k1s-q1/empty-sources.list
mapfile -d '' packages < <(
  find "$PAYLOAD_ROOT/apt/packages" -maxdepth 1 -type f -name '*.deb' -print0 | sort -z
)
test "${{#packages[@]}}" = {apt_package_count}
for package in "${{packages[@]}}"; do
  [[ "$package" = /* ]]
done
# The guest has no NIC and no configured package sources.  Do not add
# --no-download here: apt 2.4 loses command-line local-deb paths in that mode
# and rejects their basenames as non-absolute before invoking dpkg.
apt-get \
  -o Dir::Etc::SourceList=/opt/k1s-q1/empty-sources.list \
  -o Dir::Etc::SourceParts=/opt/k1s-q1/empty-sources.list.d \
  -o APT::Get::List-Cleanup=0 \
  --no-install-recommends -y install "${{packages[@]}}"
dpkg --audit
rm -f /usr/sbin/policy-rc.d

install -d -m 0755 /usr/local/bin /opt/cni/bin /etc/cni/net.d /etc/k1s-q1 /opt/k1s-q1
tar --extract --gzip --file "$PAYLOAD_ROOT/crictl/crictl-v1.30.1-linux-amd64.tar.gz" \
  --to-stdout crictl > /usr/local/bin/crictl
chmod 0755 /usr/local/bin/crictl
for plugin in bridge portmap firewall tuning loopback; do
  install -m 0755 "/usr/lib/cni/$plugin" "/opt/cni/bin/$plugin"
done
install -m 0644 "$PAYLOAD_ROOT/cni/10-k1s-q1.conflist" \
  /etc/cni/net.d/10-k1s-bridge.conflist
install -m 0644 "$PAYLOAD_ROOT/q1/offline-evidence.json" /etc/k1s-q1/offline-evidence.json
install -m 0755 "$PAYLOAD_ROOT/q1/helper/kp1_q1_guest_helper.py" \
  /opt/k1s-q1/kp1_q1_guest_helper.py
base64 --decode >/opt/k1s-q1/kp1_q1_bundle.py <<'K1S_Q1_BUNDLE'
{bundle_b64}
K1S_Q1_BUNDLE
chmod 0644 /opt/k1s-q1/kp1_q1_bundle.py

/usr/bin/python3.11 -m venv /opt/k1s-q1/venv
/opt/k1s-q1/venv/bin/pip install \
  --disable-pip-version-check --no-cache-dir --no-compile --no-index --require-hashes \
  --find-links "$PAYLOAD_ROOT/q1/wheelhouse/wheels" \
  --requirement "$PAYLOAD_ROOT/q1/wheelhouse/requirements-release.lock"
/opt/k1s-q1/venv/bin/pip install \
  --disable-pip-version-check --no-cache-dir --no-compile --no-index --no-deps \
  "$PAYLOAD_ROOT/q1/accepted/k1s_core-1.0.0rc1-py3-none-any.whl"

mv "$PAYLOAD_ROOT" /opt/k1s-q1/offline
quiesce_mutable_services
find /opt/k1s-q1 -xdev -type d -name __pycache__ -prune -exec rm -rf {{}} +
find /opt/k1s-q1 -xdev -type f -name '*.py[co]' -delete
# Normalize all Q1-owned content independently of the adversarial lane umask.
find /opt/k1s-q1 -xdev -type d -exec chmod 0755 {{}} +
find /opt/k1s-q1 -xdev -type f -perm /0111 -exec chmod 0755 {{}} +
find /opt/k1s-q1 -xdev -type f ! -perm /0111 -exec chmod 0644 {{}} +
chmod 0755 /opt/k1s-q1/kp1_q1_guest_helper.py
cat >/etc/sudoers.d/90-cloud-init-users <<'EOF'
# Deterministic Q1 image user policy
ubuntu ALL=(ALL) NOPASSWD:ALL
EOF
chmod 0440 /etc/sudoers.d/90-cloud-init-users
rm -rf /root/.cache /var/cache/apt/archives/*.deb /var/lib/apt/lists/*
rm -f \
  /var/lib/containerd/io.containerd.grpc.v1.introspection/uuid \
  /var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db \
  /var/lib/containers/storage/overlay-layers/layers.lock \
  /var/lib/containers/storage/storage.lock \
  /var/lib/docker/buildkit/containerd-overlayfs/workerid \
  /var/lib/docker/engine-id \
  /var/lib/docker/network/files/local-kv.db
rm -rf /var/lib/snapd/cgroup /var/lib/snapd/cookie /var/lib/snapd/device \
  /var/lib/snapd/inhibit
rm -f /etc/ssh/ssh_host_* /var/lib/systemd/random-seed /var/lib/dbus/machine-id
chmod 0644 /var/lib/update-notifier/updates-available 2>/dev/null || true
cloud-init clean --logs --machine-id || true
find /tmp /var/tmp -mindepth 1 -xdev -delete
find /var/log -type f -xdev -exec truncate --size 0 {{}} +
find / -xdev -path /snap -prune -o \
  -exec touch -h --date='@{guest_image.SOURCE_DATE_EPOCH}' {{}} +
sync
echo {PROVISION_MARKER.decode("ascii")} >/dev/ttyS0
/usr/bin/systemctl poweroff --no-wall
"""
    return script.encode("utf-8")


def _inventory_collection_plan(spec: Mapping[str, object]) -> dict[str, object]:
    normalized = guest_inventory.validate_spec(spec)
    expected = cast(dict[str, object], normalized["expected"])
    paths = cast(dict[str, object], normalized["paths"])
    cni = cast(dict[str, object], paths["cni"])
    runtime = cast(dict[str, object], paths["runtime"])
    plugins = cast(dict[str, object], cni["plugins"])
    content_paths = [paths["osRelease"], cni["config"], paths["offlineEvidence"]]
    digest_paths = [
        paths["kernelImage"],
        paths["python"],
        *[runtime[name] for name in guest_inventory.RUNTIME_NAMES],
        *[plugins[name] for name in guest_inventory.CNI_NAMES],
    ]
    commands: list[list[str]] = [
        ["/usr/bin/uname", "-r"],
        [
            "/usr/bin/dpkg-query",
            "-W",
            "-f=${db:Status-Abbrev}\\t${binary:Package}\\t${Version}",
            cast(str, expected["kernelPackage"]),
        ],
        [cast(str, paths["python"]), "--version"],
    ]
    commands.extend(
        [[cast(str, runtime[name]), "--version"] for name in guest_inventory.RUNTIME_NAMES]
    )
    commands.extend([[cast(str, plugins[name]), "--version"] for name in guest_inventory.CNI_NAMES])
    return {
        "commands": commands,
        "contentPaths": content_paths,
        "digestPaths": digest_paths,
    }


def render_inventory_script(spec: Mapping[str, object]) -> bytes:
    collection_b64 = _b64(_canonical_json_bytes(_inventory_collection_plan(spec)))
    filesystem_policy_b64 = _b64(guest_filesystem.canonical_json_bytes(guest_filesystem.POLICY))
    collector = r"""import base64
import hashlib
import json
import os
import signal
import stat
import subprocess

plan = json.loads(base64.b64decode("__COLLECTION__"))
filesystem_policy = json.loads(base64.b64decode("__FILESYSTEM_POLICY__"))

FILESYSTEM_OPERATION_TIMEOUT_SECONDS = 60
FILESYSTEM_PATH_PROGRESS_INTERVAL = 100
current_filesystem_operation = ""

def progress(phase):
    print("K1S_Q1_PROGRESS:" + phase, flush=True)

def encoded_path(path):
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")

def filesystem_timeout(_signum, _frame):
    progress("filesystem-timeout-" + current_filesystem_operation)
    raise RuntimeError("filesystem operation timeout: " + current_filesystem_operation)

def bounded_filesystem_operation(operation, path, callback):
    global current_filesystem_operation
    current_filesystem_operation = operation + "-" + encoded_path(path)
    signal.setitimer(signal.ITIMER_REAL, FILESYSTEM_OPERATION_TIMEOUT_SECONDS)
    try:
        return callback()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)

signal.signal(signal.SIGALRM, filesystem_timeout)

def digest_file(path, maximum=256 * 1024 * 1024):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > maximum:
                raise RuntimeError("file bound")
            digest.update(block)
    return "sha256:" + digest.hexdigest(), size

def excluded(path):
    if path in filesystem_policy["ephemeralExactPaths"]:
        return True
    for key in ("excludedMountTrees", "ephemeralTrees"):
        for root in filesystem_policy[key]:
            if path == root or path.startswith(root + "/"):
                return True
    return False

def included_persistent_mount(path):
    for root in filesystem_policy["includedPersistentMountTrees"]:
        if path == root or path.startswith(root + "/"):
            return True
    return False

def digest_xattrs(path):
    rows = []
    names = os.listxattr(path, follow_symlinks=False)
    for name in sorted(names, key=lambda item: item.encode("utf-8")):
        raw = os.getxattr(path, name, follow_symlinks=False)
        rows.append({
            "name": name,
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        })
    return rows

root_device = os.lstat("/").st_dev
filesystem_entries = []
regular_bytes = 0

def collect_tree(parent):
    global regular_bytes
    names = bounded_filesystem_operation("listdir", parent, lambda: os.listdir(parent))
    for name in sorted(names, key=lambda item: item.encode("utf-8")):
        path = "/" + name if parent == "/" else parent + "/" + name
        if excluded(path):
            continue
        if len(filesystem_entries) % FILESYSTEM_PATH_PROGRESS_INTERVAL == 0:
            progress(
                "filesystem-path-"
                + str(len(filesystem_entries))
                + "-"
                + encoded_path(path)
            )
        info = bounded_filesystem_operation("lstat", path, lambda: os.lstat(path))
        if info.st_dev != root_device and not included_persistent_mount(path):
            raise RuntimeError("unexpected cross-device path: " + path)
        row = {
            "gid": info.st_gid,
            "mode": format(stat.S_IMODE(info.st_mode), "04o"),
            "path": path,
            "uid": info.st_uid,
            "xattrs": bounded_filesystem_operation(
                "xattrs", path, lambda: digest_xattrs(path)
            ),
        }
        if stat.S_ISDIR(info.st_mode):
            row["type"] = "directory"
            filesystem_entries.append(row)
            if len(filesystem_entries) % 10000 == 0:
                progress("filesystem-entries-" + str(len(filesystem_entries)))
            collect_tree(path)
        elif stat.S_ISREG(info.st_mode):
            digest, size = bounded_filesystem_operation(
                "digest", path, lambda: digest_file(path, 16 * 1024 * 1024 * 1024)
            )
            regular_bytes += size
            row.update({
                "linkCount": info.st_nlink,
                "sha256": digest,
                "size": size,
                "type": "regular",
            })
            filesystem_entries.append(row)
            if len(filesystem_entries) % 10000 == 0:
                progress("filesystem-entries-" + str(len(filesystem_entries)))
        elif stat.S_ISLNK(info.st_mode):
            target = bounded_filesystem_operation("readlink", path, lambda: os.readlink(path))
            row.update({"target": target, "type": "symlink"})
            filesystem_entries.append(row)
            if len(filesystem_entries) % 10000 == 0:
                progress("filesystem-entries-" + str(len(filesystem_entries)))
        elif stat.S_ISBLK(info.st_mode) or stat.S_ISCHR(info.st_mode):
            row.update({
                "major": os.major(info.st_rdev),
                "minor": os.minor(info.st_rdev),
                "type": "block" if stat.S_ISBLK(info.st_mode) else "character",
            })
            filesystem_entries.append(row)
            if len(filesystem_entries) % 10000 == 0:
                progress("filesystem-entries-" + str(len(filesystem_entries)))
        else:
            raise RuntimeError("unsupported persistent object: " + path)

progress("bounded-files-start")
files = []
for path in plan["contentPaths"] + plan["digestPaths"]:
    info = os.lstat(path)
    digest, size = digest_file(path)
    raw = None
    if path in plan["contentPaths"]:
        if size > 16 * 1024 * 1024:
            raise RuntimeError("content bound")
        raw = base64.b64encode(open(path, "rb").read()).decode("ascii")
    files.append({
        "gid": info.st_gid,
        "linkCount": info.st_nlink,
        "mode": stat.S_IMODE(info.st_mode),
        "path": path,
        "raw": raw,
        "regular": stat.S_ISREG(info.st_mode),
        "sha256": digest,
        "size": size,
        "symlink": stat.S_ISLNK(info.st_mode),
        "uid": info.st_uid,
    })
progress("bounded-files-complete")

commands = []
for index, argv in enumerate(plan["commands"]):
    progress("command-" + str(index) + "-start")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
        timed_out = False
        exit_code = process.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = -1
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate(timeout=5)
    if len(stdout) + len(stderr) > 64 * 1024:
        raise RuntimeError("command bound")
    commands.append({
        "argv": argv,
        "exitCode": exit_code,
        "stderr": base64.b64encode(stderr).decode("ascii"),
        "stdout": base64.b64encode(stdout).decode("ascii"),
        "timedOut": timed_out,
    })
    progress("command-" + str(index) + "-complete")

progress("filesystem-start")
collect_tree("/")
progress("filesystem-traversal-complete")
filesystem_entries.sort(key=lambda row: row["path"].encode("utf-8"))
filesystem_manifest = {
    "apiVersion": "__FILESYSTEM_API__",
    "entries": filesystem_entries,
    "entryCount": len(filesystem_entries),
    "kind": "__FILESYSTEM_KIND__",
    "policy": filesystem_policy,
    "policySha256": "__FILESYSTEM_POLICY_SHA256__",
    "regularBytes": regular_bytes,
    "schemaVersion": __FILESYSTEM_SCHEMA__,
}
progress("filesystem-manifest-complete")

value = {"commands": commands, "files": files, "filesystemManifest": filesystem_manifest}
raw = (
    json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\n"
).encode()
progress("serialization-complete")
print("K1S_Q1_INVENTORY:" + base64.b64encode(raw).decode("ascii"), flush=True)
"""
    collector = (
        collector.replace("__COLLECTION__", collection_b64)
        .replace("__FILESYSTEM_POLICY__", filesystem_policy_b64)
        .replace("__FILESYSTEM_API__", guest_filesystem.API_VERSION)
        .replace("__FILESYSTEM_KIND__", guest_filesystem.KIND)
        .replace("__FILESYSTEM_POLICY_SHA256__", guest_filesystem.policy_digest())
        .replace("__FILESYSTEM_SCHEMA__", str(guest_filesystem.SCHEMA_VERSION))
    )
    collector_b64 = _b64(collector.encode("utf-8"))
    shell = f"""#!/bin/bash
set -euo pipefail
/usr/bin/timeout --signal=KILL 30s /usr/bin/systemctl stop \
  apt-daily.timer apt-daily-upgrade.timer \
  apt-daily.service apt-daily-upgrade.service \
  docker.socket docker.service containerd.service podman.socket \
  snapd.socket snapd.service snapd.seeded.service \
  packagekit.service unattended-upgrades.service \
  'snap.lxd.*' 'snap.*.service' || true
/usr/bin/sync
base64 --decode >/run/k1s-q1-inventory.py <<'K1S_Q1_COLLECTOR'
{collector_b64}
K1S_Q1_COLLECTOR
/usr/bin/python3.11 /run/k1s-q1-inventory.py >/dev/ttyS0 2>&1
echo {INVENTORY_MARKER.decode("ascii")} >/dev/ttyS0
sync
/usr/bin/systemctl poweroff --no-wall
"""
    return shell.encode("utf-8")


def _decode_b64(value: object, *, maximum: int) -> bytes:
    if type(value) is not str or len(value) > maximum * 2:
        raise _fail()
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise _fail() from exc
    if len(raw) > maximum:
        raise _fail()
    return raw


class SerializedInventoryAdapter:
    def __init__(self, value: object) -> None:
        if type(value) is not dict or set(value) != {
            "commands",
            "files",
            "filesystemManifest",
        }:
            raise _fail()
        files = value["files"]
        commands = value["commands"]
        if type(files) is not list or type(commands) is not list:
            raise _fail()
        try:
            self.filesystem_manifest = guest_filesystem.validate_manifest(
                value["filesystemManifest"]
            )
        except guest_filesystem.GuestFilesystemPolicyError as exc:
            raise _fail() from exc
        self.files: dict[str, guest_inventory.FileObservation] = {}
        self.commands: dict[tuple[str, ...], guest_inventory.CommandObservation] = {}
        for raw in files:
            if type(raw) is not dict or set(raw) != {
                "gid",
                "linkCount",
                "mode",
                "path",
                "raw",
                "regular",
                "sha256",
                "size",
                "symlink",
                "uid",
            }:
                raise _fail()
            path = raw["path"]
            if type(path) is not str or path in self.files:
                raise _fail()
            content = None
            if raw["raw"] is not None:
                content = _decode_b64(raw["raw"], maximum=guest_inventory.MAX_FILE_BYTES)
            observation = guest_inventory.FileObservation(
                path=path,
                raw=content,
                mode=cast(int, raw["mode"]),
                uid=cast(int, raw["uid"]),
                gid=cast(int, raw["gid"]),
                regular=cast(bool, raw["regular"]),
                symlink=cast(bool, raw["symlink"]),
                link_count=cast(int, raw["linkCount"]),
                sha256=cast(str, raw["sha256"]),
                size=cast(int, raw["size"]),
            )
            self.files[path] = observation
        for raw in commands:
            if type(raw) is not dict or set(raw) != {
                "argv",
                "exitCode",
                "stderr",
                "stdout",
                "timedOut",
            }:
                raise _fail()
            argv_raw = raw["argv"]
            if (
                type(argv_raw) is not list
                or not argv_raw
                or any(type(item) is not str for item in argv_raw)
            ):
                raise _fail()
            argv = tuple(cast(list[str], argv_raw))
            if argv in self.commands:
                raise _fail()
            self.commands[argv] = guest_inventory.CommandObservation(
                argv=argv,
                exit_code=cast(int, raw["exitCode"]),
                timed_out=cast(bool, raw["timedOut"]),
                stdout=_decode_b64(raw["stdout"], maximum=guest_inventory.MAX_COMMAND_OUTPUT_BYTES),
                stderr=_decode_b64(raw["stderr"], maximum=guest_inventory.MAX_COMMAND_OUTPUT_BYTES),
            )

    def read_file(self, path: str, *, maximum_bytes: int) -> guest_inventory.FileObservation:
        try:
            value = self.files[path]
        except KeyError as exc:
            raise _fail() from exc
        if value.raw is not None and len(value.raw) > maximum_bytes:
            raise _fail()
        return value

    def execute(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> guest_inventory.CommandObservation:
        if timeout_seconds != guest_inventory.COMMAND_TIMEOUT_SECONDS:
            raise _fail()
        try:
            value = self.commands[argv]
        except KeyError as exc:
            raise _fail() from exc
        if len(value.stdout) + len(value.stderr) > maximum_output_bytes:
            raise _fail()
        return value


def parse_inventory_serial(raw: bytes) -> SerializedInventoryAdapter:
    if not 1 <= len(raw) <= MAX_SERIAL_BYTES or INVENTORY_MARKER not in raw:
        raise _fail()
    matches = [line for line in raw.splitlines() if line.startswith(INVENTORY_PREFIX)]
    if len(matches) != 1:
        raise _fail()
    encoded = matches[0][len(INVENTORY_PREFIX) :]
    payload = _decode_b64(encoded.decode("ascii"), maximum=MAX_SERIAL_BYTES)
    return SerializedInventoryAdapter(_canonical_json(payload, maximum=MAX_SERIAL_BYTES))


def _seed_files(root: Path, *, user_data: bytes, instance_id: str) -> tuple[Path, ...]:
    seed_root = root / f"seed-{instance_id}"
    try:
        seed_root.mkdir(mode=0o700)
    except OSError as exc:
        raise _fail() from exc
    meta = f"instance-id: {instance_id}\nlocal-hostname: k1s-q1\n".encode("ascii")
    network = b"version: 2\nethernets: {}\n"
    paths = (
        seed_root / "user-data",
        seed_root / "meta-data",
        seed_root / "network-config",
    )
    for path, content in zip(paths, (user_data, meta, network), strict=True):
        _write_new(path, content)
    return paths


def _create_seed(
    plan: Mapping[str, object],
    runner: CommandRunner,
    root: Path,
    *,
    user_data: bytes,
    instance_id: str,
    output_name: str,
    deadline: float,
) -> Path:
    seed_builder = _identity_path(plan, "tools", "seedBuilder")
    paths = _seed_files(root, user_data=user_data, instance_id=instance_id)
    output = root / output_name
    result = _command(
        runner,
        (
            str(seed_builder),
            "-quiet",
            "-output",
            str(output),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            *[str(path) for path in paths],
        ),
        deadline=deadline,
    )
    if result.stdout or result.stderr:
        pass
    os.chmod(output, 0o600)
    digest, size = _digest_path(output, maximum=64 * 1024 * 1024)
    if DIGEST_RE.fullmatch(digest) is None or size <= 0:
        raise _fail()
    return output


def _qemu_argv(
    plan: Mapping[str, object],
    *,
    image: Path,
    seed: Path,
    serial: Path,
    payload: Path | None,
    snapshot: bool,
) -> tuple[str, ...]:
    qemu = _identity_path(plan, "tools", "qemu")
    argv = [
        str(qemu),
        "-machine",
        QEMU_MACHINE,
        "-cpu",
        QEMU_CPU,
        "-smp",
        str(QEMU_CPUS),
        "-m",
        str(QEMU_MEMORY_MIB),
        "-icount",
        QEMU_ICOUNT,
        "-rtc",
        f"base={QEMU_RTC},clock=vm,driftfix=none",
        "-uuid",
        QEMU_UUID,
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        f"file:{serial}",
        "-nic",
        "none",
        "-no-reboot",
        "-boot",
        "c",
        "-drive",
        f"file={image},format=qcow2,if=virtio,cache=none,aio=threads",
        "-drive",
        f"file={seed},format=raw,if=ide,index=1,media=cdrom,readonly=on",
    ]
    if payload is not None:
        argv.extend(
            (
                "-drive",
                f"file={payload},format=raw,if=virtio,readonly=on,cache=none,aio=threads",
            )
        )
    if snapshot:
        argv.append("-snapshot")
    return tuple(argv)


class LiveBuildAdapter:
    def __init__(
        self,
        plan_value: object,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.plan = guest_image.verify_plan(plan_value)
        verify_executing_sources(self.plan)
        self.runner = runner or SubprocessRunner()
        self._b2: dict[str, object] | None = None

    def _root(self, request: guest_image.StageRequest) -> Path:
        root = Path(request.root)
        expected = Path(cast(str, cast(dict[str, object], self.plan["outputParent"])["path"]))
        if root.parent != expected or root.name != request.lane:
            raise _fail()
        return root

    def _prepare_stage(self, request: guest_image.StageRequest) -> Path:
        if request.plan != self.plan or request.stage not in guest_image.STAGES:
            raise _fail()
        root = self._root(request)
        stage_index = guest_image.STAGES.index(request.stage)
        if stage_index == 0:
            if root.exists() or root.is_symlink():
                raise _fail()
            root.mkdir(mode=0o700)
            root.chmod(0o700)
        else:
            if not root.is_dir() or root.is_symlink():
                raise _fail()
            previous = root / f"stage-{stage_index:02d}-{guest_image.STAGES[stage_index - 1]}.json"
            if not previous.is_file():
                raise _fail()
        current = root / f"stage-{stage_index + 1:02d}-{request.stage}.json"
        if current.exists() or current.is_symlink():
            raise _fail()
        return root

    def _complete_stage(
        self,
        request: guest_image.StageRequest,
        root: Path,
        detail: Mapping[str, object],
    ) -> None:
        index = guest_image.STAGES.index(request.stage) + 1
        path = root / f"stage-{index:02d}-{request.stage}.json"
        _write_new(
            path,
            _canonical_json_bytes(
                {
                    "apiVersion": API_VERSION,
                    "detail": dict(detail),
                    "identity": dict(request.authority),
                    "kind": "K1sQ1GuestLiveBuildStage",
                    "schemaVersion": SCHEMA_VERSION,
                    "status": "passed",
                }
            ),
        )

    def _failure(self, request: guest_image.StageRequest, root: Path, exc: Exception) -> None:
        if not root.is_dir():
            return
        path = root / "B3-FAILURE.json"
        if path.exists() or path.is_symlink():
            return
        text = str(exc).replace("\x00", "")[:MAX_FAILURE_TEXT]
        try:
            _write_new(
                path,
                _canonical_json_bytes(
                    {
                        "apiVersion": API_VERSION,
                        "error": type(exc).__name__,
                        "kind": FAILURE_KIND,
                        "message": text,
                        "schemaVersion": SCHEMA_VERSION,
                        "stage": request.stage,
                        "status": "failed",
                    }
                ),
            )
        except Exception:
            return

    def _authenticate_base(self) -> dict[str, object]:
        b1_plan = _canonical_json(_read_identity(self.plan, "b1AcquisitionPlan"))
        inputs = cast(dict[str, object], self.plan["inputs"])
        b1_seal = cast(dict[str, object], inputs["b1EvidenceSeal"])
        result = base_acquisition.verify_acquisition_root(
            b1_plan,
            expected_seal_file_digest=b1_seal["sha256"],
        )
        artifacts = cast(list[dict[str, object]], result["artifacts"])
        base_path = _identity_path(self.plan, "inputs", "baseImage")
        manifest_path = _identity_path(self.plan, "inputs", "packageManifest")
        by_name = {cast(str, row["filename"]): row for row in artifacts}
        if (
            by_name.get(base_path.name, {}).get("sha256")
            != cast(dict[str, object], cast(dict[str, object], self.plan["inputs"])["baseImage"])[
                "sha256"
            ]
            or by_name.get(manifest_path.name, {}).get("sha256")
            != cast(
                dict[str, object],
                cast(dict[str, object], self.plan["inputs"])["packageManifest"],
            )["sha256"]
        ):
            raise _fail()
        return {"b1SealFileSha256": result["sealFileSha256"], "status": "base-authenticated"}

    def _validate_b2(self) -> dict[str, object]:
        b2_plan = _canonical_json(_read_identity(self.plan, "b2AcquisitionPlan"))
        inputs = cast(dict[str, object], self.plan["inputs"])
        b2_seal = cast(dict[str, object], inputs["b2EvidenceSeal"])
        result = guest_inputs.verify_retained_root(
            b2_plan,
            expected_seal_file_sha256=b2_seal["sha256"],
        )
        root = Path(cast(str, result["outputRoot"]))
        expected = {
            "offlineBom": root / "B2-GUEST-BOM.json",
            "offlinePayload": root / "B2-OFFLINE-PAYLOAD.tar",
        }
        for name, path in expected.items():
            if _identity_path(self.plan, "inputs", name) != path:
                raise _fail()
        self._b2 = result
        return {
            "aptPackageCount": result["aptPackageCount"],
            "bomSha256": result["bomSha256"],
            "payloadSha256": result["payloadSha256"],
            "sealFileSha256": result["sealFileSha256"],
            "status": "offline-bom-verified",
        }

    def _b2_result(self) -> dict[str, object]:
        if self._b2 is None:
            self._validate_b2()
        assert self._b2 is not None
        return self._b2

    def _stage_inputs(self, request: guest_image.StageRequest, root: Path) -> dict[str, object]:
        b2 = self._b2_result()
        bundle = _read_identity(self.plan, "guestBundle")
        provision = render_provision_script(
            apt_package_count=cast(int, b2["aptPackageCount"]),
            payload_sha256=cast(str, b2["payloadSha256"]),
            payload_size=cast(
                int,
                cast(
                    dict[str, object],
                    cast(dict[str, object], self.plan["inputs"])["offlinePayload"],
                )["size"],
            ),
            member_manifest_sha256=cast(str, b2["memberManifestSha256"]),
            guest_bundle=bundle,
            umask=request.umask,
        )
        seed = _create_seed(
            self.plan,
            self.runner,
            root,
            user_data=provision,
            instance_id=f"q1-b3-{request.lane}-provision",
            output_name="provision-seed.iso",
            deadline=request.deadline_monotonic,
        )
        return {"seedSha256": _digest_path(seed, maximum=64 * 1024 * 1024)[0]}

    def _create_image(self, request: guest_image.StageRequest, root: Path) -> dict[str, object]:
        qemu_img = _identity_path(self.plan, "tools", "qemuImg")
        base = _identity_path(self.plan, "inputs", "baseImage")
        work = root / "work.qcow2"
        _command(
            self.runner,
            (
                str(qemu_img),
                "convert",
                "-f",
                "qcow2",
                "-O",
                "qcow2",
                "-o",
                "compat=1.1,cluster_size=65536,lazy_refcounts=off",
                str(base),
                str(work),
            ),
            deadline=request.deadline_monotonic,
        )
        _command(
            self.runner,
            (str(qemu_img), "resize", str(work), str(IMAGE_VIRTUAL_SIZE)),
            deadline=request.deadline_monotonic,
        )
        os.chmod(work, 0o600)
        return {"imageVirtualSize": IMAGE_VIRTUAL_SIZE, "status": "image-created"}

    def _run_guest(
        self,
        request: guest_image.StageRequest,
        *,
        image: Path,
        seed: Path,
        serial: Path,
        payload: Path | None,
        snapshot: bool,
    ) -> bytes:
        if serial.exists() or serial.is_symlink():
            raise _fail()
        result = _command(
            self.runner,
            _qemu_argv(
                self.plan,
                image=image,
                seed=seed,
                serial=serial,
                payload=payload,
                snapshot=snapshot,
            ),
            deadline=request.deadline_monotonic,
        )
        if result.stdout or result.stderr:
            _write_new(
                serial.with_suffix(".qemu-output"),
                result.stdout + b"\x00" + result.stderr,
            )
        try:
            raw = serial.read_bytes()
        except OSError as exc:
            raise _fail() from exc
        if not 1 <= len(raw) <= MAX_SERIAL_BYTES:
            raise _fail()
        os.chmod(serial, 0o600)
        return raw

    def _provision(self, request: guest_image.StageRequest, root: Path) -> dict[str, object]:
        raw = self._run_guest(
            request,
            image=root / "work.qcow2",
            seed=root / "provision-seed.iso",
            serial=root / "provision.serial",
            payload=_identity_path(self.plan, "inputs", "offlinePayload"),
            snapshot=False,
        )
        if raw.count(PROVISION_MARKER) != 1:
            raise _fail()
        return {"serialSha256": _digest_bytes(raw), "status": "offline-provisioned"}

    def _finalize(self, request: guest_image.StageRequest, root: Path) -> dict[str, object]:
        qemu_img = _identity_path(self.plan, "tools", "qemuImg")
        raw_image = root / "normalized.raw"
        final = root / "guest.qcow2"
        _command(
            self.runner,
            (
                str(qemu_img),
                "convert",
                "-f",
                "qcow2",
                "-O",
                "raw",
                str(root / "work.qcow2"),
                str(raw_image),
            ),
            deadline=request.deadline_monotonic,
        )
        _command(
            self.runner,
            (
                str(qemu_img),
                "convert",
                "-f",
                "raw",
                "-O",
                "qcow2",
                "-o",
                "compat=1.1,cluster_size=65536,lazy_refcounts=off",
                str(raw_image),
                str(final),
            ),
            deadline=request.deadline_monotonic,
        )
        try:
            raw_image.unlink()
            os.chmod(final, 0o600)
        except OSError as exc:
            raise _fail() from exc
        digest, size = _digest_path(final, maximum=guest_image.MAX_IMAGE_BYTES)
        authority = {"imageSha256": digest, "imageSize": size}
        _write_new(root / "image-authority.json", _canonical_json_bytes(authority))
        return authority

    def _inventory_spec(self, request: guest_image.StageRequest, root: Path) -> dict[str, object]:
        bom = guest_image.validate_bom(_canonical_json(_read_identity(self.plan, "offlineBom")))
        guest = cast(dict[str, object], bom["guest"])
        authority = _canonical_json((root / "image-authority.json").read_bytes())
        inputs = cast(dict[str, object], self.plan["inputs"])
        source = cast(dict[str, object], self.plan["source"])
        kernel = cast(dict[str, object], guest["kernel"])
        return {
            "apiVersion": guest_inventory.API_VERSION,
            "authority": {
                "buildPlanDigest": self.plan["planDigest"],
                "helperSha256": cast(dict[str, object], guest["helper"])["sha256"],
                "imageSha256": authority["imageSha256"],
                "lane": request.lane,
                "requirementsLockSha256": inputs["requirementsLock"]["sha256"],
                "sourceCommit": source["commit"],
                "sourceTree": source["tree"],
                "wheelhouseManifestSha256": inputs["wheelhouseManifest"]["sha256"],
            },
            "expected": {
                "kernelPackage": kernel["package"],
                "kernelRelease": kernel["release"],
            },
            "kind": guest_inventory.SPEC_KIND,
            "paths": {
                "cni": {
                    "config": "/etc/cni/net.d/10-k1s-bridge.conflist",
                    "plugins": {name: f"/opt/cni/bin/{name}" for name in guest_inventory.CNI_NAMES},
                },
                "kernelImage": f"/boot/vmlinuz-{kernel['release']}",
                "offlineEvidence": "/etc/k1s-q1/offline-evidence.json",
                "osRelease": "/usr/lib/os-release",
                "python": "/usr/bin/python3.11",
                "runtime": {
                    "containerd": "/usr/bin/containerd",
                    "crictl": "/usr/local/bin/crictl",
                    "docker": "/usr/bin/docker",
                    "podman": "/usr/bin/podman",
                    "runc": "/usr/bin/runc",
                },
            },
            "schemaVersion": guest_inventory.SCHEMA_VERSION,
        }

    def _extract_inventory(
        self,
        request: guest_image.StageRequest,
        root: Path,
    ) -> dict[str, object]:
        spec = self._inventory_spec(request, root)
        spec_raw = _canonical_json_bytes(spec)
        _write_new(root / "inventory-spec.json", spec_raw)
        seed = _create_seed(
            self.plan,
            self.runner,
            root,
            user_data=render_inventory_script(spec),
            instance_id=f"q1-b3-{request.lane}-inventory",
            output_name="inventory-seed.iso",
            deadline=request.deadline_monotonic,
        )
        raw = self._run_guest(
            request,
            image=root / "guest.qcow2",
            seed=seed,
            serial=root / "inventory.serial",
            payload=None,
            snapshot=True,
        )
        adapter = parse_inventory_serial(raw)
        manifest, receipt = guest_inventory.extract_inventory(spec, adapter=adapter)
        manifest_raw = guest_image.q1_capsule.canonical_json_bytes(manifest)
        receipt_raw = guest_inventory.canonical_json_bytes(receipt)
        filesystem_raw = guest_filesystem.canonical_json_bytes(adapter.filesystem_manifest)
        _write_new(root / "guest-manifest.json", manifest_raw)
        _write_new(root / "inventory-receipt.json", receipt_raw)
        _write_new(root / "filesystem-manifest.json", filesystem_raw)
        return {
            "filesystemManifestSha256": _digest_bytes(filesystem_raw),
            "guestManifestSha256": _digest_bytes(manifest_raw),
            "inventorySha256": _digest_bytes(receipt_raw),
        }

    def _validate_manifest(self, root: Path) -> dict[str, object]:
        manifest_raw = (root / "guest-manifest.json").read_bytes()
        manifest = _canonical_json(manifest_raw)
        bom = guest_image.validate_bom(_canonical_json(_read_identity(self.plan, "offlineBom")))
        expected = cast(dict[str, object], bom["guest"])
        actual = dict(manifest)
        for key in ("apiVersion", "image", "kind", "schemaVersion"):
            actual.pop(key, None)
        if actual != expected:
            raise _fail()
        guest_semantic_raw = _canonical_json_bytes(actual)
        try:
            filesystem = guest_filesystem.validate_manifest(
                _canonical_json(
                    (root / "filesystem-manifest.json").read_bytes(),
                    maximum=guest_filesystem.MAX_MANIFEST_BYTES,
                )
            )
        except (OSError, guest_filesystem.GuestFilesystemPolicyError) as exc:
            raise _fail() from exc
        filesystem_raw = guest_filesystem.canonical_json_bytes(filesystem)
        image_digest, image_size = _digest_path(
            root / "guest.qcow2",
            maximum=guest_image.MAX_IMAGE_BYTES,
        )
        image = cast(dict[str, object], manifest["image"])
        if image.get("sha256") != image_digest:
            raise _fail()
        inventory_raw = (root / "inventory-receipt.json").read_bytes()
        return {
            "filesystemManifestSha256": _digest_bytes(filesystem_raw),
            "filesystemPolicySha256": guest_filesystem.policy_digest(),
            "guestManifestSha256": _digest_bytes(manifest_raw),
            "guestSemanticSha256": _digest_bytes(guest_semantic_raw),
            "imageSha256": image_digest,
            "imageSize": image_size,
            "inventorySha256": _digest_bytes(inventory_raw),
        }

    def execute(self, request: guest_image.StageRequest) -> guest_image.StageObservation:
        root = self._root(request)
        try:
            root = self._prepare_stage(request)
            detail: dict[str, object]
            artifacts: dict[str, object] | None = None
            if request.stage == "authenticate-base":
                detail = self._authenticate_base()
            elif request.stage == "validate-offline-bom":
                detail = self._validate_b2()
            elif request.stage == "stage-inputs":
                detail = self._stage_inputs(request, root)
            elif request.stage == "create-image":
                detail = self._create_image(request, root)
            elif request.stage == "provision-offline":
                detail = self._provision(request, root)
            elif request.stage == "finalize-image":
                detail = self._finalize(request, root)
            elif request.stage == "extract-inventory":
                detail = self._extract_inventory(request, root)
            elif request.stage == "validate-guest-manifest":
                artifacts = self._validate_manifest(root)
                detail = {"status": "guest-manifest-verified", **artifacts}
            else:
                raise _fail()
            self._complete_stage(request, root, detail)
            return guest_image.StageObservation(
                identity=dict(request.authority),
                exit_code=0,
                timed_out=False,
                stdout=_canonical_json_bytes(detail),
                artifacts=artifacts,
            )
        except Exception as exc:
            self._failure(request, root, exc)
            if isinstance(exc, GuestLiveBuildError):
                raise
            raise _fail() from exc


def _read_plan(path: Path, *, expected_digest: str) -> dict[str, object]:
    digest, size = _digest_path(path, maximum=guest_image.MAX_JSON_BYTES)
    if digest != expected_digest or size > guest_image.MAX_JSON_BYTES:
        raise _fail()
    return guest_image.verify_plan(_canonical_json(path.read_bytes()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--expect-plan-sha256", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if DIGEST_RE.fullmatch(args.expect_plan_sha256) is None:
            raise _fail()
        plan = _read_plan(args.plan, expected_digest=args.expect_plan_sha256)
        if args.confirm != f"BUILD-Q1-GUEST:{plan['planDigest']}":
            raise _fail()
        receipt_parent = args.receipt.parent
        parent = cast(dict[str, object], plan["outputParent"])
        if receipt_parent != Path(cast(str, parent["path"])):
            raise _fail()
        receipt = guest_image.run_build(plan, adapter=LiveBuildAdapter(plan))
        _write_new(args.receipt, guest_image.canonical_json_bytes(receipt))
        print(
            json.dumps(
                {"receiptDigest": receipt["receiptDigest"], "status": "built-and-verified"},
                sort_keys=True,
            )
        )
        return 0
    except (GuestLiveBuildError, guest_image.GuestImagePolicyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
