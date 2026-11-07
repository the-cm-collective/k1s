"""Owner-private bootstrap records for canonical edge and node profiles.

The record contains credential material and therefore never participates
directly in a public result or public execution-plan digest.  Callers receive
only :meth:`ProfileBootstrapRecord.public_projection`; the private record
digest and exact file identity are retained solely for owner-scoped recovery.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from ae.controller.node_identity import (
    IDENTITY_VERSION,
    NodeIdentityError,
    scoped_node_id,
    validate_node_id,
)
from ae.kctl.profile_paths import (
    ProfileLayout,
    ProfilePathError,
    ProfileRoots,
    validate_bootstrap_id,
)
from ae.kctl.profile_state import ProfileStateError, ProfileStateStore

API_VERSION = "k1s.dev/v1alpha1"
SCHEMA_VERSION = 1
MAX_BOOTSTRAP_BYTES = 128 * 1024
MAX_CREDENTIAL_BYTES = 64 * 1024
MAX_CREATE_ATTEMPTS = 8

BootstrapProfile = Literal["edge-gateway", "node"]
BootstrapBackend = Literal["gateway", "podman", "docker"]
CredentialKind = Literal["nats-creds", "agent-token"]

_RECORD_KEYS = {
    "advertiseEndpoint",
    "apiVersion",
    "backendId",
    "bootstrapId",
    "controllerUrl",
    "createdAt",
    "credential",
    "credentialKind",
    "identityVersion",
    "kind",
    "listenHost",
    "listenPort",
    "nodeId",
    "privateRecordDigest",
    "profile",
    "schemaVersion",
    "siteId",
    "transportUrl",
}


class ProfileBootstrapError(ProfileStateError):
    """A private bootstrap record is absent, ambiguous, or unsafe."""


@contextmanager
def _bootstrap_storage_boundary() -> Iterator[None]:
    """Translate path/filesystem failures without disclosing private paths."""

    try:
        yield
    except ProfileBootstrapError:
        raise
    except (OSError, ProfilePathError) as exc:
        raise ProfileBootstrapError("bootstrap storage is unavailable or unsafe") from exc


@dataclass(frozen=True, slots=True)
class BootstrapFileIdentity:
    """Private exact identity of one bootstrap record file."""

    device: int
    inode: int
    size: int

    def __post_init__(self) -> None:
        if self.device < 0 or self.inode <= 0 or self.size <= 0:
            raise ProfileBootstrapError("bootstrap record file identity is invalid")


@dataclass(frozen=True, slots=True)
class ProfileBootstrapRecord:
    """Parsed private bootstrap authority plus a redacted public projection."""

    profile: BootstrapProfile
    bootstrap_id: str
    identity_version: int
    node_id: str
    site_id: str | None
    controller_url: str | None
    transport_url: str | None
    backend_id: BootstrapBackend
    listen_host: str | None
    listen_port: int | None
    advertise_endpoint: str | None
    credential_kind: CredentialKind = field(repr=False)
    credential: str = field(repr=False)
    created_at: str = field(repr=False)
    private_record_digest: str = field(repr=False)
    file_identity: BootstrapFileIdentity = field(repr=False)
    path: Path = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.profile not in {"edge-gateway", "node"}:
            raise ProfileBootstrapError("bootstrap profile is unsupported")
        _validate_bootstrap_id(self.bootstrap_id)
        if self.identity_version != IDENTITY_VERSION:
            raise ProfileBootstrapError("bootstrap identity version is unsupported")
        _validate_node_identity(self.node_id)
        _validate_timestamp(self.created_at)
        _validate_digest(self.private_record_digest)
        if not self.path.is_absolute():
            raise ProfileBootstrapError("bootstrap record path is not absolute")
        if not self.credential or "\x00" in self.credential:
            raise ProfileBootstrapError("bootstrap credential is invalid")
        if len(self.credential.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
            raise ProfileBootstrapError("bootstrap credential is too large")
        if self.profile == "edge-gateway":
            if (
                self.backend_id != "gateway"
                or self.credential_kind != "nats-creds"
                or self.site_id is None
                or self.transport_url is None
                or self.controller_url is not None
                or self.listen_host is not None
                or self.listen_port is not None
                or self.advertise_endpoint is not None
            ):
                raise ProfileBootstrapError("gateway bootstrap fields are contradictory")
            _validate_node_identity(self.site_id, field="site_id")
            if _scoped_node_identity(self.site_id, self.node_id) != self.node_id:
                raise ProfileBootstrapError("gateway node identity is not site-scoped")
            _validate_endpoint(self.transport_url, field="transport URL", schemes={"nats", "tls"})
        else:
            if (
                self.backend_id not in {"podman", "docker"}
                or self.credential_kind != "agent-token"
                or self.site_id is not None
                or self.transport_url is not None
                or self.controller_url is None
                or self.listen_host is None
                or self.listen_port is None
                or self.advertise_endpoint is None
            ):
                raise ProfileBootstrapError("node bootstrap fields are contradictory")
            _validate_endpoint(
                self.controller_url,
                field="controller URL",
                schemes={"http", "https"},
            )
            _validate_endpoint(
                self.advertise_endpoint,
                field="advertise endpoint",
                schemes={"http", "https"},
            )
            _validate_listen(self.listen_host, self.listen_port)
            if self.credential != self.credential.strip() or any(
                character.isspace() for character in self.credential
            ):
                raise ProfileBootstrapError("node bootstrap token must be one canonical line")

    def public_projection(self) -> dict[str, object]:
        """Return the only bootstrap fields allowed in public results/digests."""

        return {
            "advertiseEndpoint": self.advertise_endpoint,
            "backendId": self.backend_id,
            "bootstrapId": self.bootstrap_id,
            "controllerUrl": self.controller_url,
            "identityVersion": self.identity_version,
            "listenHost": self.listen_host,
            "listenPort": self.listen_port,
            "nodeId": self.node_id,
            "profile": self.profile,
            "siteId": self.site_id,
            "transportUrl": self.transport_url,
        }

    def credential_bytes(self) -> bytes:
        """Return private credential bytes only to an owner-scoped materializer."""

        return self.credential.encode("utf-8")


class ProfileBootstrapStore:
    """Create/load immutable private bootstrap records under the profile lock."""

    def __init__(
        self,
        roots: ProfileRoots,
        requested_profile: str,
        *,
        clock: Callable[[], str] | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        with _bootstrap_storage_boundary():
            layout = ProfileLayout.resolve(roots, requested_profile)
            state_store = ProfileStateStore(roots, requested_profile)
        if layout.canonical_profile not in {"edge-gateway", "node"}:
            raise ProfileBootstrapError("bootstrap creation is limited to edge-gateway and node")
        if layout.requested_profile != layout.canonical_profile:
            raise ProfileBootstrapError("bootstrap creation does not accept profile aliases")
        self.layout = layout
        self.state_store = state_store
        self.clock = clock or _utc_now
        self.random_bytes = random_bytes

    def create_edge_gateway(
        self,
        *,
        site_id: str,
        node_id: str,
        transport_url: str,
        credential_file: Path,
    ) -> ProfileBootstrapRecord:
        with _bootstrap_storage_boundary():
            if self.layout.canonical_profile != "edge-gateway":
                raise ProfileBootstrapError("bootstrap role differs from the selected profile")
            site = _validate_node_identity(site_id, field="site_id")
            scoped = _scoped_node_identity(site, _validate_node_identity(node_id))
            endpoint = _validate_endpoint(
                transport_url,
                field="transport URL",
                schemes={"nats", "tls"},
            )
            credential = _read_credential(
                credential_file,
                owner_uid=self.layout.roots.owner_uid,
                kind="nats-creds",
            )
            return self._create(
                profile="edge-gateway",
                node_id=scoped,
                site_id=site,
                controller_url=None,
                transport_url=endpoint,
                backend_id="gateway",
                listen_host=None,
                listen_port=None,
                advertise_endpoint=None,
                credential_kind="nats-creds",
                credential=credential,
            )

    def create_node(
        self,
        *,
        node_id: str,
        controller_url: str,
        runtime_backend: str,
        listen_host: str,
        listen_port: int,
        advertise_endpoint: str,
        credential_file: Path,
    ) -> ProfileBootstrapRecord:
        with _bootstrap_storage_boundary():
            if self.layout.canonical_profile != "node":
                raise ProfileBootstrapError("bootstrap role differs from the selected profile")
            if runtime_backend not in {"podman", "docker"}:
                raise ProfileBootstrapError("node runtime backend must be podman or docker")
            host, port = _validate_listen(listen_host, listen_port)
            credential = _read_credential(
                credential_file,
                owner_uid=self.layout.roots.owner_uid,
                kind="agent-token",
            )
            return self._create(
                profile="node",
                node_id=_validate_node_identity(node_id),
                site_id=None,
                controller_url=_validate_endpoint(
                    controller_url,
                    field="controller URL",
                    schemes={"http", "https"},
                ),
                transport_url=None,
                backend_id=cast(BootstrapBackend, runtime_backend),
                listen_host=host,
                listen_port=port,
                advertise_endpoint=_validate_endpoint(
                    advertise_endpoint,
                    field="advertise endpoint",
                    schemes={"http", "https"},
                ),
                credential_kind="agent-token",
                credential=credential,
            )

    def load(self, bootstrap_id: str) -> ProfileBootstrapRecord:
        with _bootstrap_storage_boundary():
            identifier = _validate_bootstrap_id(bootstrap_id)
            with self.state_store.locked(shared=True, blocking=True, create=False):
                return self._read_locked(identifier)

    def revalidate(self, expected: ProfileBootstrapRecord) -> ProfileBootstrapRecord:
        """Require exact private bytes and file identity before execution."""

        with _bootstrap_storage_boundary():
            if expected.profile != self.layout.canonical_profile:
                raise ProfileBootstrapError("bootstrap record role changed")
            with self.state_store.locked(shared=True, blocking=True, create=False):
                current = self._read_locked(expected.bootstrap_id)
            if (
                current.private_record_digest != expected.private_record_digest
                or current.file_identity != expected.file_identity
            ):
                raise ProfileBootstrapError("private bootstrap record identity changed")
            return current

    def _create(
        self,
        *,
        profile: BootstrapProfile,
        node_id: str,
        site_id: str | None,
        controller_url: str | None,
        transport_url: str | None,
        backend_id: BootstrapBackend,
        listen_host: str | None,
        listen_port: int | None,
        advertise_endpoint: str | None,
        credential_kind: CredentialKind,
        credential: str,
    ) -> ProfileBootstrapRecord:
        created_at = _validate_timestamp(self.clock())
        with self.state_store.locked(blocking=True):
            self.layout.ensure_bootstrap_directory()
            for _attempt in range(MAX_CREATE_ATTEMPTS):
                raw_identifier = self.random_bytes(16)
                if len(raw_identifier) != 16:
                    raise ProfileBootstrapError(
                        "bootstrap randomness source returned the wrong size"
                    )
                bootstrap_id = _validate_bootstrap_id(f"b1-{raw_identifier.hex()}")
                path = self.layout.bootstrap_path(bootstrap_id)
                if path.exists() or path.is_symlink():
                    continue
                payload: dict[str, object] = {
                    "advertiseEndpoint": advertise_endpoint,
                    "apiVersion": API_VERSION,
                    "backendId": backend_id,
                    "bootstrapId": bootstrap_id,
                    "controllerUrl": controller_url,
                    "createdAt": created_at,
                    "credential": credential,
                    "credentialKind": credential_kind,
                    "identityVersion": IDENTITY_VERSION,
                    "kind": "ProfileBootstrap",
                    "listenHost": listen_host,
                    "listenPort": listen_port,
                    "nodeId": node_id,
                    "profile": profile,
                    "schemaVersion": SCHEMA_VERSION,
                    "siteId": site_id,
                    "transportUrl": transport_url,
                }
                payload["privateRecordDigest"] = _private_digest(payload)
                encoded = _canonical_bytes(payload)
                identity = _write_new_private_file(
                    path,
                    encoded,
                    owner_uid=self.layout.roots.owner_uid,
                    random_bytes=self.random_bytes,
                )
                return _record_from_payload(payload, path=path, file_identity=identity)
        raise ProfileBootstrapError("unable to allocate an unused bootstrap ID")

    def _read_locked(self, bootstrap_id: str) -> ProfileBootstrapRecord:
        path = self.layout.bootstrap_path(bootstrap_id)
        payload, identity = _read_private_file(path, owner_uid=self.layout.roots.owner_uid)
        record = _record_from_payload(payload, path=path, file_identity=identity)
        if record.profile != self.layout.canonical_profile or record.bootstrap_id != bootstrap_id:
            raise ProfileBootstrapError("bootstrap record identity differs from its owner")
        return record


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_node_identity(value: str, *, field: str = "node_id") -> str:
    try:
        return validate_node_id(value, field=field)
    except NodeIdentityError as exc:
        raise ProfileBootstrapError("bootstrap node identity is invalid") from exc


def _scoped_node_identity(site_id: str, node_id: str) -> str:
    try:
        return scoped_node_id(site_id, node_id)
    except NodeIdentityError as exc:
        raise ProfileBootstrapError("bootstrap node identity scope is invalid") from exc


def _validate_bootstrap_id(value: str) -> str:
    try:
        return validate_bootstrap_id(value)
    except ProfilePathError as exc:
        raise ProfileBootstrapError("bootstrap ID is invalid") from exc


def _validate_timestamp(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value.endswith("Z"):
        raise ProfileBootstrapError("bootstrap timestamp is not canonical UTC text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProfileBootstrapError("bootstrap timestamp is not canonical UTC text") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ProfileBootstrapError("bootstrap timestamp is not canonical UTC text")
    canonical = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    if canonical != value:
        raise ProfileBootstrapError("bootstrap timestamp is not canonical UTC text")
    return value


def _validate_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ProfileBootstrapError("private bootstrap digest is invalid")
    return value


def _validate_endpoint(value: str, *, field: str, schemes: set[str]) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ProfileBootstrapError(f"{field} is invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProfileBootstrapError(f"{field} is invalid") from exc
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ProfileBootstrapError(f"{field} is invalid")
    if parsed.path == "/" or parsed.scheme != parsed.scheme.lower():
        raise ProfileBootstrapError(f"{field} must use canonical endpoint spelling")
    return value


def _validate_listen(host: str, port: int) -> tuple[str, int]:
    if not isinstance(host, str) or host != host.strip() or "%" in host:
        raise ProfileBootstrapError("listen host must be a canonical IP literal")
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ProfileBootstrapError("listen host must be a canonical IP literal") from exc
    if str(parsed) != host:
        raise ProfileBootstrapError("listen host must be a canonical IP literal")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProfileBootstrapError("listen port must be in the range 1..65535")
    return host, port


def _read_credential(path: Path, *, owner_uid: int, kind: CredentialKind) -> str:
    if not isinstance(path, Path) or not path.is_absolute() or path.resolve(strict=False) != path:
        raise ProfileBootstrapError("credential source must be an exact absolute path")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ProfileBootstrapError("credential source is unavailable or unsafe") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_CREDENTIAL_BYTES
        ):
            raise ProfileBootstrapError("credential source ownership or mode is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise ProfileBootstrapError("credential source changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise ProfileBootstrapError("credential source changed while being read")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or after.st_nlink != 1:
            raise ProfileBootstrapError("credential source changed while being read")
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if b"\x00" in payload:
        raise ProfileBootstrapError("credential source contains unsupported bytes")
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProfileBootstrapError("credential source is not UTF-8") from exc
    if not value:
        raise ProfileBootstrapError("credential source is empty")
    if kind == "agent-token" and (
        value != value.strip() or any(character.isspace() for character in value)
    ):
        raise ProfileBootstrapError("node bootstrap token must be one canonical line")
    return value


def _without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileBootstrapError("bootstrap record contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ProfileBootstrapError(f"unsupported bootstrap record number: {value}")


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _private_digest(payload: Mapping[str, object]) -> str:
    private = {key: value for key, value in payload.items() if key != "privateRecordDigest"}
    return f"sha256:{hashlib.sha256(_canonical_bytes(private)).hexdigest()}"


def _record_from_payload(
    raw: Mapping[str, object],
    *,
    path: Path,
    file_identity: BootstrapFileIdentity,
) -> ProfileBootstrapRecord:
    if set(raw) != _RECORD_KEYS:
        raise ProfileBootstrapError("bootstrap record has missing or unknown keys")
    if raw["apiVersion"] != API_VERSION or raw["kind"] != "ProfileBootstrap":
        raise ProfileBootstrapError("bootstrap record envelope differs")
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != SCHEMA_VERSION:
        raise ProfileBootstrapError("bootstrap record schema version differs")
    private_digest = _validate_digest(_text(raw["privateRecordDigest"], "private digest"))
    if private_digest != _private_digest(raw):
        raise ProfileBootstrapError("private bootstrap record digest differs")
    profile = _text(raw["profile"], "profile")
    backend = _text(raw["backendId"], "backend")
    credential_kind = _text(raw["credentialKind"], "credential kind")
    if profile not in {"edge-gateway", "node"}:
        raise ProfileBootstrapError("bootstrap profile is unsupported")
    if backend not in {"gateway", "podman", "docker"}:
        raise ProfileBootstrapError("bootstrap backend is unsupported")
    if credential_kind not in {"nats-creds", "agent-token"}:
        raise ProfileBootstrapError("bootstrap credential kind is unsupported")
    identity_version = raw["identityVersion"]
    if type(identity_version) is not int:
        raise ProfileBootstrapError("bootstrap identity version is invalid")
    listen_port = raw["listenPort"]
    if listen_port is not None and type(listen_port) is not int:
        raise ProfileBootstrapError("bootstrap listen port is invalid")
    return ProfileBootstrapRecord(
        profile=cast(BootstrapProfile, profile),
        bootstrap_id=_validate_bootstrap_id(_text(raw["bootstrapId"], "bootstrap ID")),
        identity_version=identity_version,
        node_id=_text(raw["nodeId"], "node ID"),
        site_id=_optional_text(raw["siteId"], "site ID"),
        controller_url=_optional_text(raw["controllerUrl"], "controller URL"),
        transport_url=_optional_text(raw["transportUrl"], "transport URL"),
        backend_id=cast(BootstrapBackend, backend),
        listen_host=_optional_text(raw["listenHost"], "listen host"),
        listen_port=listen_port,
        advertise_endpoint=_optional_text(raw["advertiseEndpoint"], "advertise endpoint"),
        credential_kind=cast(CredentialKind, credential_kind),
        credential=_text(raw["credential"], "credential"),
        created_at=_text(raw["createdAt"], "created at"),
        private_record_digest=private_digest,
        file_identity=file_identity,
        path=path,
    )


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ProfileBootstrapError(f"bootstrap {field_name} is invalid")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _write_new_private_file(
    path: Path,
    payload: bytes,
    *,
    owner_uid: int,
    random_bytes: Callable[[int], bytes],
) -> BootstrapFileIdentity:
    if not payload or len(payload) > MAX_BOOTSTRAP_BYTES:
        raise ProfileBootstrapError("bootstrap record size is invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    temporary: Path | None = None
    for _attempt in range(MAX_CREATE_ATTEMPTS):
        raw = random_bytes(16)
        if len(raw) != 16:
            raise ProfileBootstrapError("bootstrap randomness source returned the wrong size")
        candidate = path.with_name(f".{path.name}.tmp-{raw.hex()}")
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ProfileBootstrapError("bootstrap record target is unavailable or unsafe") from exc
        temporary = candidate
        break
    if temporary is None:
        raise ProfileBootstrapError("unable to allocate a private bootstrap temporary file")

    linked = False
    try:
        try:
            _write_all(fd, payload)
            os.fsync(fd)
            temp_info = os.fstat(fd)
            if (
                not stat.S_ISREG(temp_info.st_mode)
                or temp_info.st_uid != owner_uid
                or stat.S_IMODE(temp_info.st_mode) != 0o600
                or temp_info.st_nlink != 1
                or temp_info.st_size != len(payload)
            ):
                raise ProfileBootstrapError("bootstrap record ownership or mode is unsafe")
        finally:
            os.close(fd)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except OSError as exc:
            raise ProfileBootstrapError(
                "bootstrap record target is unavailable or already exists"
            ) from exc
        linked = True
        temporary.unlink()
        final_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(final_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != len(payload)
            ):
                raise ProfileBootstrapError("bootstrap record ownership or mode is unsafe")
        finally:
            os.close(final_fd)
        _fsync_directory(path.parent)
        return BootstrapFileIdentity(info.st_dev, info.st_ino, info.st_size)
    except Exception:
        if linked:
            with suppress(OSError):
                path.unlink()
        with suppress(OSError):
            temporary.unlink()
        with suppress(OSError):
            _fsync_directory(path.parent)
        raise


def _read_private_file(
    path: Path,
    *,
    owner_uid: int,
) -> tuple[dict[str, object], BootstrapFileIdentity]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ProfileBootstrapError("bootstrap record is unavailable or unsafe") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_BOOTSTRAP_BYTES
        ):
            raise ProfileBootstrapError("bootstrap record ownership or mode is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise ProfileBootstrapError("bootstrap record changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise ProfileBootstrapError("bootstrap record changed while being read")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or after.st_nlink != 1:
            raise ProfileBootstrapError("bootstrap record changed while being read")
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if payload.startswith(b"\xef\xbb\xbf") or b"\x00" in payload or b"\r" in payload:
        raise ProfileBootstrapError("bootstrap record encoding is not canonical")
    try:
        text = payload.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileBootstrapError("bootstrap record is not canonical JSON") from exc
    if type(raw) is not dict or _canonical_bytes(raw) != payload:
        raise ProfileBootstrapError("bootstrap record bytes are not canonical JSON")
    return cast(dict[str, object], raw), BootstrapFileIdentity(
        after.st_dev,
        after.st_ino,
        after.st_size,
    )


def _write_all(fd: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(fd, payload[written:])
        if count <= 0:
            raise ProfileBootstrapError("bootstrap record write did not complete")
        written += count


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "BootstrapBackend",
    "BootstrapFileIdentity",
    "BootstrapProfile",
    "MAX_BOOTSTRAP_BYTES",
    "MAX_CREDENTIAL_BYTES",
    "ProfileBootstrapError",
    "ProfileBootstrapRecord",
    "ProfileBootstrapStore",
]
