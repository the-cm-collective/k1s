"""Pure, deterministic strict-CRI host planning.

The planner consumes two explicit, canonical, digest-pinned JSON documents.  It
does not inspect the target host, derive authority from the environment, open a
runtime socket, or mutate profile/host state.  The resulting plan is therefore
source-validated but deliberately not a live-runtime qualification.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

API_VERSION = "k1s.dev/v1alpha1"
INTENT_KIND = "StrictCRIHostIntent"
FACTS_KIND = "StrictCRIHostFacts"
PLAN_KIND = "StrictCRIHostPlan"
SCHEMA_VERSION = 1
QUALIFICATION_POSTURE = "source-validated/unqualified"
MAX_INPUT_BYTES = 128 * 1024
REQUIRED_CNI_PLUGINS = ("bridge", "portmap", "firewall", "tuning", "loopback")
CONFIG_TARGETS = ("10-k1s.conflist", "99-loopback.conf")

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_MODE_RE = re.compile(r"0[0-7]{3}")


class ProfileHostPlanError(ValueError):
    """A generic, intentionally redacted strict-CRI planning failure."""


@dataclass(frozen=True, slots=True)
class StrictCRIHostAuthorities:
    """Exact canonical authorities independently reconstructed for execution."""

    intent: dict[str, Any]
    facts: dict[str, Any]
    plan: dict[str, Any]
    intent_digest: str
    facts_digest: str
    plan_file_digest: str
    plan_digest: str


@dataclass(frozen=True, slots=True)
class StrictCRIActionMaterial:
    """One ordered helper action derived only from canonical authorities."""

    index: int
    action_id: str
    kind: str
    target: str
    expected_before: dict[str, Any]
    expected_before_digest: str
    desired_after_digest: str
    rollback: str
    source: dict[str, Any] | None = None
    content: bytes | None = None


def _fail() -> ProfileHostPlanError:
    return ProfileHostPlanError("strict CRI host plan failed closed")


def canonical_json_bytes(value: object) -> bytes:
    """Return the one canonical JSON encoding used by inputs, plans, and digests."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    return (text + "\n").encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_value(value: object) -> str:
    return _digest_bytes(canonical_json_bytes(value))


def _canonical_absolute_path(raw: object) -> str:
    if type(raw) is not str or not raw.startswith("/") or raw == "/":
        raise _fail()
    if raw.startswith("//") or raw.endswith("/") or "\\" in raw or "\x00" in raw:
        raise _fail()
    if any(ord(character) < 32 for character in raw):
        raise _fail()
    parts = raw.split("/")[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _fail()
    if str(PurePosixPath(raw)) != raw:
        raise _fail()
    return raw


def _canonical_input_path(raw: object) -> str:
    return _canonical_absolute_path(raw)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _reject_constant(_raw: str) -> object:
    raise _fail()


def _load_canonical_document(
    raw_path: object, expected_digest: object
) -> tuple[dict[str, Any], str]:
    path = _canonical_input_path(raw_path)
    digest = _require_digest(expected_digest)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    descriptor = -1
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise _fail()
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _fail()
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise _fail()
        if opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600:
            raise _fail()
        if opened.st_nlink != 1 or not 0 <= opened.st_size <= MAX_INPUT_BYTES:
            raise _fail()
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise _fail()
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise _fail()
        after = os.fstat(descriptor)
        identity_before = (
            opened.st_dev,
            opened.st_ino,
            opened.st_uid,
            opened.st_gid,
            opened.st_mode,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_gid,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise _fail()
        raw = b"".join(chunks)
    except ProfileHostPlanError:
        raise
    except (OSError, OverflowError, ValueError) as exc:
        raise _fail() from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise _fail() from exc

    if _digest_bytes(raw) != digest or b"\x00" in raw or b"\r" in raw:
        raise _fail()
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ProfileHostPlanError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return value, digest


def _closed(value: object, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    return value


def _require_text(value: object, *, maximum: int = 255) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise _fail()
    if value.strip() != value or "\x00" in value or any(ord(item) < 32 for item in value):
        raise _fail()
    return value


def _require_identifier(value: object) -> str:
    text = _require_text(value, maximum=63)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise _fail()
    return text


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _require_int(value: object, *, maximum: int = (1 << 31) - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise _fail()
    return value


def _require_mode(value: object) -> str:
    if type(value) is not str or _MODE_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _require_host_port(value: object) -> str:
    text = _require_text(value)
    try:
        parsed = urlsplit(f"//{text}")
        port = parsed.port
    except ValueError as exc:
        raise _fail() from exc
    if parsed.username is not None or parsed.password is not None or parsed.path:
        raise _fail()
    if parsed.query or parsed.fragment or parsed.hostname is None or port is None or port == 0:
        raise _fail()
    hostname = parsed.hostname
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if not labels or any(_IDENTIFIER_RE.fullmatch(label) is None for label in labels):
            raise _fail() from None
        canonical_host = hostname.lower()
    else:
        canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    if text != f"{canonical_host}:{port}":
        raise _fail()
    return text


def _require_endpoint(value: object) -> tuple[str, str]:
    text = _require_text(value, maximum=4096)
    prefix = "unix://"
    if not text.startswith(prefix):
        raise _fail()
    path = _canonical_absolute_path(text[len(prefix) :])
    if text != f"{prefix}{path}":
        raise _fail()
    return text, path


def _validate_header(value: dict[str, Any], *, kind: str, extra: set[str]) -> None:
    _closed(value, {"apiVersion", "kind", "schemaVersion", *extra})
    if (
        value["apiVersion"] != API_VERSION
        or value["kind"] != kind
        or value["schemaVersion"] != SCHEMA_VERSION
    ):
        raise _fail()


def _validate_intent(value: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "profileId",
        "role",
        "target",
        "cri",
        "containerd",
        "cni",
        "registry",
        "socketAccess",
        "nodeIdentity",
    }
    _validate_header(value, kind=INTENT_KIND, extra=fields)
    profile_id = _require_identifier(value["profileId"])
    role = value["role"]
    if role not in {"core", "node"}:
        raise _fail()

    target = _closed(
        value["target"],
        {"targetId", "hostIdentitySha256", "ownerUid", "ownerGid"},
    )
    normalized_target = {
        "hostIdentitySha256": _require_digest(target["hostIdentitySha256"]),
        "ownerGid": _require_int(target["ownerGid"]),
        "ownerUid": _require_int(target["ownerUid"]),
        "targetId": _require_identifier(target["targetId"]),
    }

    cri = _closed(value["cri"], {"endpoint", "runtimeHandler"})
    endpoint, socket_path = _require_endpoint(cri["endpoint"])
    normalized_cri = {
        "endpoint": endpoint,
        "runtimeHandler": _require_identifier(cri["runtimeHandler"]),
        "socketPath": socket_path,
    }

    containerd = _closed(value["containerd"], {"configPath", "certsRoot"})
    normalized_containerd = {
        "certsRoot": _canonical_absolute_path(containerd["certsRoot"]),
        "configPath": _canonical_absolute_path(containerd["configPath"]),
    }

    cni = _closed(
        value["cni"],
        {
            "bridgeName",
            "configRoot",
            "cniVersion",
            "podCidr",
            "requiredPlugins",
            "sourceBinRoot",
            "targetBinRoot",
        },
    )
    bridge_name = _require_identifier(cni["bridgeName"])
    cni_version = _require_text(cni["cniVersion"], maximum=32)
    if cni_version != "1.0.0":
        raise _fail()
    try:
        network = ipaddress.ip_network(_require_text(cni["podCidr"], maximum=64), strict=True)
    except ValueError as exc:
        raise _fail() from exc
    if network.version != 4 or network.prefixlen > 30:
        raise _fail()
    if (
        type(cni["requiredPlugins"]) is not list
        or tuple(cni["requiredPlugins"]) != REQUIRED_CNI_PLUGINS
    ):
        raise _fail()
    normalized_cni = {
        "bridgeName": bridge_name,
        "configRoot": _canonical_absolute_path(cni["configRoot"]),
        "cniVersion": cni_version,
        "podCidr": str(network),
        "requiredPlugins": list(REQUIRED_CNI_PLUGINS),
        "sourceBinRoot": _canonical_absolute_path(cni["sourceBinRoot"]),
        "targetBinRoot": _canonical_absolute_path(cni["targetBinRoot"]),
    }

    registry = value["registry"]
    if type(registry) is not dict or registry.get("mode") not in {"off", "external-tls"}:
        raise _fail()
    if registry["mode"] == "off":
        _closed(registry, {"mode"})
        normalized_registry: dict[str, Any] = {"mode": "off"}
    else:
        _closed(
            registry,
            {"mode", "host", "scheme", "caObject", "caSha256", "systemTrust"},
        )
        if registry["scheme"] != "https" or registry["systemTrust"] is not False:
            raise _fail()
        normalized_registry = {
            "caObject": _require_identifier(registry["caObject"]),
            "caSha256": _require_digest(registry["caSha256"]),
            "host": _require_host_port(registry["host"]),
            "mode": "external-tls",
            "scheme": "https",
            "systemTrust": False,
        }

    socket_access = _closed(value["socketAccess"], {"mode", "uid", "gid"})
    if socket_access["mode"] not in {"existing", "acl"}:
        raise _fail()
    normalized_socket_access = {
        "gid": _require_int(socket_access["gid"]),
        "mode": socket_access["mode"],
        "uid": _require_int(socket_access["uid"]),
    }
    if (
        normalized_socket_access["uid"] != normalized_target["ownerUid"]
        or normalized_socket_access["gid"] != normalized_target["ownerGid"]
    ):
        raise _fail()

    node_identity = value["nodeIdentity"]
    if role == "core":
        if node_identity is not None:
            raise _fail()
        normalized_node: dict[str, Any] | None = None
    else:
        node = _closed(
            node_identity,
            {"identityVersion", "nodeId", "bootstrapId", "bootstrapPublicDigest"},
        )
        if node["identityVersion"] != 1:
            raise _fail()
        normalized_node = {
            "bootstrapId": _require_identifier(node["bootstrapId"]),
            "bootstrapPublicDigest": _require_digest(node["bootstrapPublicDigest"]),
            "identityVersion": 1,
            "nodeId": _require_identifier(node["nodeId"]),
        }

    return {
        "cni": normalized_cni,
        "containerd": normalized_containerd,
        "cri": normalized_cri,
        "nodeIdentity": normalized_node,
        "profileId": profile_id,
        "registry": normalized_registry,
        "role": role,
        "socketAccess": normalized_socket_access,
        "target": normalized_target,
    }


def _expected_object_paths(intent: dict[str, Any]) -> dict[str, tuple[str, str, bool]]:
    cni = intent["cni"]
    containerd = intent["containerd"]
    expected: dict[str, tuple[str, str, bool]] = {
        "criSocket": (intent["cri"]["socketPath"], "socket", True),
        "containerdConfig": (containerd["configPath"], "regular", True),
        "containerdCertsRoot": (containerd["certsRoot"], "directory", True),
        "cniSourceRoot": (cni["sourceBinRoot"], "directory", True),
        "cniTargetRoot": (cni["targetBinRoot"], "directory", True),
        "cniConfigRoot": (cni["configRoot"], "directory", True),
    }
    for plugin in REQUIRED_CNI_PLUGINS:
        expected[f"cniSource:{plugin}"] = (
            f"{cni['sourceBinRoot']}/{plugin}",
            "regular",
            True,
        )
        expected[f"cniTarget:{plugin}"] = (
            f"{cni['targetBinRoot']}/{plugin}",
            "regular",
            False,
        )
    for name in CONFIG_TARGETS:
        expected[f"cniConfig:{name}"] = (f"{cni['configRoot']}/{name}", "regular", False)
    registry = intent["registry"]
    if registry["mode"] == "external-tls":
        registry_root = f"{containerd['certsRoot']}/{registry['host']}"
        expected.update(
            {
                registry["caObject"]: ("", "regular", True),
                "registryRoot": (registry_root, "directory", True),
                "registryCaTarget": (f"{registry_root}/ca.crt", "regular", False),
                "registryHostsTarget": (f"{registry_root}/hosts.toml", "regular", False),
            }
        )
    return expected


def _validate_parent(value: object, *, child_path: str) -> dict[str, Any]:
    parent = _closed(
        value,
        {"path", "type", "device", "inode", "uid", "gid", "mode", "nlink"},
    )
    expected_parent = str(PurePosixPath(child_path).parent)
    if _canonical_absolute_path(parent["path"]) != expected_parent or parent["type"] != "directory":
        raise _fail()
    normalized: dict[str, Any] = {
        "device": _require_int(parent["device"], maximum=(1 << 63) - 1),
        "gid": _require_int(parent["gid"]),
        "inode": _require_int(parent["inode"], maximum=(1 << 63) - 1),
        "mode": _require_mode(parent["mode"]),
        "nlink": _require_int(parent["nlink"], maximum=(1 << 31) - 1),
        "path": expected_parent,
        "type": "directory",
        "uid": _require_int(parent["uid"]),
    }
    if normalized["uid"] != 0 or normalized["gid"] != 0:
        raise _fail()
    if int(normalized["mode"], 8) & 0o022 or normalized["nlink"] < 1:
        raise _fail()
    return normalized


def _validate_object_fact(
    value: object,
    *,
    expected_path: str,
    expected_type: str,
    required_present: bool,
) -> dict[str, Any]:
    if type(value) is not dict or value.get("state") not in {"present", "absent"}:
        raise _fail()
    if _canonical_absolute_path(value.get("path")) != expected_path:
        raise _fail()
    if value["state"] == "absent":
        if required_present:
            raise _fail()
        _closed(value, {"path", "state", "parent"})
        return {
            "parent": _validate_parent(value["parent"], child_path=expected_path),
            "path": expected_path,
            "state": "absent",
        }

    _closed(
        value,
        {
            "path",
            "state",
            "type",
            "device",
            "inode",
            "uid",
            "gid",
            "mode",
            "nlink",
            "size",
            "sha256",
        },
    )
    if value["type"] != expected_type:
        raise _fail()
    normalized = {
        "device": _require_int(value["device"], maximum=(1 << 63) - 1),
        "gid": _require_int(value["gid"]),
        "inode": _require_int(value["inode"], maximum=(1 << 63) - 1),
        "mode": _require_mode(value["mode"]),
        "nlink": _require_int(value["nlink"], maximum=(1 << 31) - 1),
        "path": expected_path,
        "sha256": value["sha256"],
        "size": _require_int(value["size"], maximum=(1 << 31) - 1),
        "state": "present",
        "type": expected_type,
        "uid": _require_int(value["uid"]),
    }
    mode = int(normalized["mode"], 8)
    if expected_type == "regular":
        if normalized["uid"] != 0 or normalized["gid"] != 0:
            raise _fail()
        normalized["sha256"] = _require_digest(value["sha256"])
        if normalized["nlink"] != 1 or mode & 0o022:
            raise _fail()
    elif expected_type == "socket":
        if value["sha256"] is not None or normalized["size"] != 0 or normalized["nlink"] != 1:
            raise _fail()
        if mode & 0o007:
            raise _fail()
    else:
        if normalized["uid"] != 0 or normalized["gid"] != 0:
            raise _fail()
        if value["sha256"] is not None or normalized["size"] != 0 or normalized["nlink"] < 1:
            raise _fail()
        if mode & 0o022:
            raise _fail()
    return normalized


def _validate_facts(value: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "targetId",
        "hostIdentitySha256",
        "ownerUid",
        "ownerGid",
        "platform",
        "arch",
        "rootless",
        "containerd",
        "objects",
    }
    _validate_header(value, kind=FACTS_KIND, extra=fields)
    target = intent["target"]
    if (
        value["targetId"] != target["targetId"]
        or value["hostIdentitySha256"] != target["hostIdentitySha256"]
        or value["ownerUid"] != target["ownerUid"]
        or value["ownerGid"] != target["ownerGid"]
    ):
        raise _fail()
    if value["platform"] != "linux" or value["arch"] not in {"amd64", "arm64"}:
        raise _fail()
    if value["rootless"] is not False:
        raise _fail()
    containerd = _closed(
        value["containerd"],
        {"version", "configActive", "criApiVersion", "runtimeHandlers"},
    )
    if containerd["configActive"] is not True or containerd["criApiVersion"] != "v1":
        raise _fail()
    version = _require_text(containerd["version"], maximum=64)
    handlers = containerd["runtimeHandlers"]
    if type(handlers) is not list or not handlers:
        raise _fail()
    normalized_handlers = [_require_identifier(handler) for handler in handlers]
    if normalized_handlers != sorted(set(normalized_handlers)):
        raise _fail()
    if intent["cri"]["runtimeHandler"] not in normalized_handlers:
        raise _fail()

    expected = _expected_object_paths(intent)
    objects = value["objects"]
    if type(objects) is not dict or set(objects) != set(expected):
        raise _fail()
    normalized_objects: dict[str, Any] = {}
    for name in sorted(expected):
        path, object_type, required = expected[name]
        if not path:
            source = objects[name]
            if type(source) is not dict or source.get("state") != "present":
                raise _fail()
            path = _canonical_absolute_path(source.get("path"))
        normalized_objects[name] = _validate_object_fact(
            objects[name],
            expected_path=path,
            expected_type=object_type,
            required_present=required,
        )

    for plugin in REQUIRED_CNI_PLUGINS:
        source = normalized_objects[f"cniSource:{plugin}"]
        if int(source["mode"], 8) & 0o111 == 0 or source["size"] == 0:
            raise _fail()
        if source["device"] != normalized_objects["cniSourceRoot"]["device"]:
            raise _fail()

    parent_roots = {
        **{f"cniTarget:{plugin}": "cniTargetRoot" for plugin in REQUIRED_CNI_PLUGINS},
        **{f"cniConfig:{name}": "cniConfigRoot" for name in CONFIG_TARGETS},
    }
    if intent["registry"]["mode"] == "external-tls":
        parent_roots.update(
            {"registryCaTarget": "registryRoot", "registryHostsTarget": "registryRoot"}
        )
        ca_source = normalized_objects[intent["registry"]["caObject"]]
        if ca_source["size"] == 0:
            raise _fail()
    for child_name, root_name in parent_roots.items():
        child = normalized_objects[child_name]
        root = normalized_objects[root_name]
        if child["state"] == "absent":
            parent = child["parent"]
            if (
                parent["path"] != root["path"]
                or parent["device"] != root["device"]
                or parent["inode"] != root["inode"]
                or parent["uid"] != root["uid"]
                or parent["gid"] != root["gid"]
                or parent["mode"] != root["mode"]
            ):
                raise _fail()
        elif child["device"] != root["device"]:
            raise _fail()
    registry = intent["registry"]
    if registry["mode"] == "external-tls":
        source = normalized_objects[registry["caObject"]]
        if source["sha256"] != registry["caSha256"]:
            raise _fail()

    return {
        "arch": value["arch"],
        "containerd": {
            "configActive": True,
            "criApiVersion": "v1",
            "runtimeHandlers": normalized_handlers,
            "version": version,
        },
        "hostIdentitySha256": target["hostIdentitySha256"],
        "objects": normalized_objects,
        "ownerGid": target["ownerGid"],
        "ownerUid": target["ownerUid"],
        "platform": "linux",
        "rootless": False,
        "targetId": target["targetId"],
    }


def _desired_file_digest(path: str, content_digest: str, *, mode: str) -> str:
    return _digest_value(
        {
            "gid": 0,
            "mode": mode,
            "path": path,
            "sha256": content_digest,
            "type": "regular",
            "uid": 0,
        }
    )


def _action(
    *,
    action_id: str,
    kind: str,
    target: str,
    before: dict[str, Any],
    desired: str,
) -> dict[str, str]:
    return {
        "desiredAfterDigest": desired,
        "expectedBeforeDigest": _digest_value(before),
        "id": action_id,
        "kind": kind,
        "rollback": "none"
        if kind in {"validate", "verify"}
        else ("remove-created" if before["state"] == "absent" else "restore-exact-before"),
        "target": target,
    }


def _cni_config_bytes(intent: dict[str, Any], name: str) -> bytes:
    cni = intent["cni"]
    if name == "10-k1s.conflist":
        value: object = {
            "cniVersion": cni["cniVersion"],
            "name": "k1s",
            "plugins": [
                {
                    "bridge": cni["bridgeName"],
                    "ipMasq": True,
                    "ipam": {"ranges": [[{"subnet": cni["podCidr"]}]], "type": "host-local"},
                    "isGateway": True,
                    "type": "bridge",
                },
                {"capabilities": {"portMappings": True}, "type": "portmap"},
                {"type": "firewall"},
                {"type": "tuning"},
            ],
        }
    elif name == "99-loopback.conf":
        value = {"cniVersion": cni["cniVersion"], "name": "lo", "type": "loopback"}
    else:  # pragma: no cover - closed caller set
        raise _fail()
    return canonical_json_bytes(value)


def _registry_hosts_bytes(intent: dict[str, Any]) -> bytes:
    registry = intent["registry"]
    root = f"{intent['containerd']['certsRoot']}/{registry['host']}"
    endpoint = f"https://{registry['host']}"
    return (
        f'server = "{endpoint}"\n\n'
        f'[host."{endpoint}"]\n'
        '  capabilities = ["pull", "resolve"]\n'
        f'  ca = "{root}/ca.crt"\n'
    ).encode()


def build_strict_cri_host_plan(
    intent_document: dict[str, Any],
    facts_document: dict[str, Any],
    *,
    intent_digest: str,
    facts_digest: str,
) -> dict[str, Any]:
    """Validate canonical values and build one secret-free deterministic plan."""

    intent = _validate_intent(intent_document)
    facts = _validate_facts(facts_document, intent)
    actions: list[dict[str, str]] = []
    objects = facts["objects"]

    for action_id, object_name in (
        ("validate-cri-socket", "criSocket"),
        ("validate-containerd-config", "containerdConfig"),
    ):
        item = objects[object_name]
        actions.append(
            _action(
                action_id=action_id,
                kind="validate",
                target=item["path"],
                before=item,
                desired=_digest_value(item),
            )
        )

    for plugin in REQUIRED_CNI_PLUGINS:
        source = objects[f"cniSource:{plugin}"]
        target = objects[f"cniTarget:{plugin}"]
        desired = _desired_file_digest(target["path"], source["sha256"], mode="0755")
        target_matches = (
            target["state"] == "present"
            and target["sha256"] == source["sha256"]
            and target["mode"] == "0755"
        )
        actions.append(
            _action(
                action_id=f"cni-plugin-{plugin}",
                kind="verify" if target_matches else "copy-regular",
                target=target["path"],
                before=target,
                desired=desired,
            )
        )

    for name in CONFIG_TARGETS:
        target = objects[f"cniConfig:{name}"]
        content_digest = _digest_bytes(_cni_config_bytes(intent, name))
        desired = _desired_file_digest(target["path"], content_digest, mode="0644")
        target_matches = (
            target["state"] == "present"
            and target["sha256"] == content_digest
            and target["mode"] == "0644"
        )
        actions.append(
            _action(
                action_id=f"cni-config-{name}",
                kind="verify" if target_matches else "write-canonical",
                target=target["path"],
                before=target,
                desired=desired,
            )
        )

    registry = intent["registry"]
    if registry["mode"] == "external-tls":
        for action_id, object_name, content_digest in (
            ("registry-ca", "registryCaTarget", registry["caSha256"]),
            ("registry-hosts", "registryHostsTarget", _digest_bytes(_registry_hosts_bytes(intent))),
        ):
            target = objects[object_name]
            desired = _desired_file_digest(target["path"], content_digest, mode="0644")
            target_matches = (
                target["state"] == "present"
                and target["sha256"] == content_digest
                and target["mode"] == "0644"
            )
            actions.append(
                _action(
                    action_id=action_id,
                    kind="verify"
                    if target_matches
                    else ("copy-regular" if action_id == "registry-ca" else "write-canonical"),
                    target=target["path"],
                    before=target,
                    desired=desired,
                )
            )

    socket = objects["criSocket"]
    access = intent["socketAccess"]
    if access["mode"] == "existing":
        socket_mode = int(socket["mode"], 8)
        owner_access = socket["uid"] == access["uid"] and socket_mode & 0o600 == 0o600
        group_access = socket["gid"] == access["gid"] and socket_mode & 0o060 == 0o060
        if not (owner_access or group_access):
            raise _fail()
    acl_digest = _digest_value(
        {
            "gid": access["gid"],
            "path": socket["path"],
            "permissions": "rw",
            "uid": access["uid"],
        }
    )
    actions.append(
        _action(
            action_id="cri-socket-access",
            kind="verify" if access["mode"] == "existing" else "grant-exact-acl",
            target=socket["path"],
            before=socket,
            desired=acl_digest,
        )
    )

    plan: dict[str, Any] = {
        "actions": actions,
        "apiVersion": API_VERSION,
        "cni": intent["cni"],
        "containerd": {
            **intent["containerd"],
            "criApiVersion": facts["containerd"]["criApiVersion"],
            "runtimeHandler": intent["cri"]["runtimeHandler"],
            "version": facts["containerd"]["version"],
        },
        "cri": {key: value for key, value in intent["cri"].items() if key != "socketPath"},
        "factsDigest": _require_digest(facts_digest),
        "intentDigest": _require_digest(intent_digest),
        "kind": PLAN_KIND,
        "mutating": False,
        "nodeIdentity": intent["nodeIdentity"],
        "profileId": intent["profileId"],
        "qualificationPosture": QUALIFICATION_POSTURE,
        "registry": intent["registry"],
        "role": intent["role"],
        "schemaVersion": SCHEMA_VERSION,
        "socketAccess": intent["socketAccess"],
        "target": {
            **intent["target"],
            "arch": facts["arch"],
            "platform": facts["platform"],
        },
        "warnings": ["Live CRI/containerd enforcement is not qualified by this source-only plan."],
    }
    plan["planDigest"] = _digest_value(plan)
    return plan


def plan_strict_cri_from_files(
    *,
    intent_path: object,
    expected_intent_digest: object,
    facts_path: object,
    expected_facts_digest: object,
) -> dict[str, Any]:
    """Load two exact immutable authorities and return the deterministic plan."""

    intent, intent_digest = _load_canonical_document(intent_path, expected_intent_digest)
    facts, facts_digest = _load_canonical_document(facts_path, expected_facts_digest)
    return build_strict_cri_host_plan(
        intent,
        facts,
        intent_digest=intent_digest,
        facts_digest=facts_digest,
    )


def load_strict_cri_host_authorities(
    *,
    intent_path: object,
    expected_intent_digest: object,
    facts_path: object,
    expected_facts_digest: object,
    plan_path: object,
    expected_plan_file_digest: object,
    expected_plan_digest: object,
) -> StrictCRIHostAuthorities:
    """Reload and byte-for-byte reconstruct all execution authorities.

    This function is intentionally suitable for use on both sides of the
    helper protocol.  It never accepts an action list from the coordinator.
    """

    intent_raw, intent_digest = _load_canonical_document(intent_path, expected_intent_digest)
    facts_raw, facts_digest = _load_canonical_document(facts_path, expected_facts_digest)
    plan_raw, plan_file_digest = _load_canonical_document(plan_path, expected_plan_file_digest)
    rebuilt = build_strict_cri_host_plan(
        intent_raw,
        facts_raw,
        intent_digest=intent_digest,
        facts_digest=facts_digest,
    )
    plan_digest = _require_digest(expected_plan_digest)
    if plan_raw != rebuilt or canonical_json_bytes(plan_raw) != canonical_json_bytes(rebuilt):
        raise _fail()
    if plan_raw.get("planDigest") != plan_digest:
        raise _fail()
    return StrictCRIHostAuthorities(
        intent=intent_raw,
        facts=facts_raw,
        plan=plan_raw,
        intent_digest=intent_digest,
        facts_digest=facts_digest,
        plan_file_digest=plan_file_digest,
        plan_digest=plan_digest,
    )


def strict_cri_action_materials(
    authorities: StrictCRIHostAuthorities,
) -> tuple[StrictCRIActionMaterial, ...]:
    """Derive the closed ordered helper action set from canonical authorities."""

    intent = authorities.intent
    facts = authorities.facts
    plan = authorities.plan
    objects = facts["objects"]
    before_by_action: dict[str, dict[str, Any]] = {
        "validate-cri-socket": objects["criSocket"],
        "validate-containerd-config": objects["containerdConfig"],
        "cri-socket-access": objects["criSocket"],
    }
    source_by_action: dict[str, dict[str, Any]] = {}
    content_by_action: dict[str, bytes] = {}
    for plugin in REQUIRED_CNI_PLUGINS:
        action_id = f"cni-plugin-{plugin}"
        before_by_action[action_id] = objects[f"cniTarget:{plugin}"]
        source_by_action[action_id] = objects[f"cniSource:{plugin}"]
    for name in CONFIG_TARGETS:
        action_id = f"cni-config-{name}"
        before_by_action[action_id] = objects[f"cniConfig:{name}"]
        content_by_action[action_id] = _cni_config_bytes(intent, name)
    if intent["registry"]["mode"] == "external-tls":
        before_by_action["registry-ca"] = objects["registryCaTarget"]
        source_by_action["registry-ca"] = objects[intent["registry"]["caObject"]]
        before_by_action["registry-hosts"] = objects["registryHostsTarget"]
        content_by_action["registry-hosts"] = _registry_hosts_bytes(intent)

    result: list[StrictCRIActionMaterial] = []
    actions = plan.get("actions")
    if type(actions) is not list:
        raise _fail()
    for index, raw in enumerate(actions):
        if type(raw) is not dict or set(raw) != {
            "desiredAfterDigest",
            "expectedBeforeDigest",
            "id",
            "kind",
            "rollback",
            "target",
        }:
            raise _fail()
        action_id = _require_text(raw["id"], maximum=63)
        if action_id not in before_by_action:
            raise _fail()
        before = before_by_action[action_id]
        if raw["expectedBeforeDigest"] != _digest_value(before):
            raise _fail()
        target = _canonical_absolute_path(raw["target"])
        if target != before["path"]:
            raise _fail()
        kind = _require_text(raw["kind"], maximum=32)
        rollback = _require_text(raw["rollback"], maximum=32)
        if kind not in {
            "validate",
            "verify",
            "copy-regular",
            "write-canonical",
            "grant-exact-acl",
        }:
            raise _fail()
        if rollback not in {"none", "remove-created", "restore-exact-before"}:
            raise _fail()
        result.append(
            StrictCRIActionMaterial(
                index=index,
                action_id=action_id,
                kind=kind,
                target=target,
                expected_before=before,
                expected_before_digest=_require_digest(raw["expectedBeforeDigest"]),
                desired_after_digest=_require_digest(raw["desiredAfterDigest"]),
                rollback=rollback,
                source=source_by_action.get(action_id),
                content=content_by_action.get(action_id),
            )
        )
    if [item.action_id for item in result] != [str(item["id"]) for item in actions]:
        raise _fail()
    return tuple(result)


def render_strict_cri_host_plan_json(plan: dict[str, Any]) -> str:
    return canonical_json_bytes(plan).decode("utf-8")


def render_strict_cri_host_plan_text(plan: dict[str, Any]) -> str:
    lines = [
        f"profile={plan['profileId']}",
        f"role={plan['role']}",
        f"target={plan['target']['targetId']}",
        f"qualification={plan['qualificationPosture']}",
        f"mutating={str(plan['mutating']).lower()}",
        f"planDigest={plan['planDigest']}",
    ]
    lines.extend(
        f"action={item['id']} kind={item['kind']} target={item['target']}"
        for item in plan["actions"]
    )
    lines.extend(f"warning={warning}" for warning in plan["warnings"])
    return "\n".join(lines) + "\n"
