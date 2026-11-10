"""Owner-private XDG paths for the installed k1s profile state contract."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ae.kctl.profiles import resolve_profile

_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PATH_SEGMENT_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_RUN_ID_RE = re.compile(r"r1-[0-9a-f]{32}")
_BOOTSTRAP_ID_RE = re.compile(r"b1-[0-9a-f]{32}")


class ProfilePathError(ValueError):
    """An XDG root or owned profile path is unsafe or noncanonical."""


def validate_identifier(value: str, *, field: str) -> str:
    """Return a canonical, single-segment identifier or fail closed."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or _ID_RE.fullmatch(value) is None
    ):
        raise ProfilePathError(f"{field} must be a lowercase DNS-label-like identifier")
    return value


def validate_run_id(value: str) -> str:
    """Return an immutable versioned run identifier or fail closed."""

    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise ProfilePathError("run ID must use the r1-<32 lowercase hex> format")
    return value


def validate_bootstrap_id(value: str) -> str:
    """Return one exact immutable bootstrap identifier or fail closed."""

    if not isinstance(value, str) or _BOOTSTRAP_ID_RE.fullmatch(value) is None:
        raise ProfilePathError("bootstrap ID must use the b1-<32 lowercase hex> format")
    return value


def validate_relative_path(value: str, *, field: str) -> str:
    """Validate an exact canonical relative POSIX path used in a receipt."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or "//" in value
    ):
        raise ProfilePathError(f"{field} must be a canonical relative POSIX path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts) or str(path) != value:
        raise ProfilePathError(f"{field} must be a canonical relative POSIX path")
    for index, part in enumerate(path.parts):
        if _PATH_SEGMENT_RE.fullmatch(part) is None:
            raise ProfilePathError(f"{field}[{index}] must be a canonical filename segment")
    return value


def _canonical_absolute(value: str | Path, *, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ProfilePathError(f"{field} must be an absolute path")
    resolved = path.resolve(strict=False)
    if resolved != path:
        raise ProfilePathError(f"{field} must be canonical and symlink-free")
    return path


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _ensure_private_directory(path: Path, *, owner_uid: int) -> None:
    """Create one directory tree with private components and validate the leaf."""

    missing: list[Path] = []
    current = path
    while _lstat(current) is None:
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ProfilePathError(f"cannot find an existing ancestor for {path}")
        current = parent
    if stat.S_ISLNK(current.lstat().st_mode):
        raise ProfilePathError(f"directory ancestor is a symlink: {current}")
    for candidate in reversed(missing):
        candidate.mkdir(mode=0o700)

    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ProfilePathError(f"owned path is not a regular directory: {path}")
    if info.st_uid != owner_uid:
        raise ProfilePathError(f"owned directory has a foreign owner: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ProfilePathError(f"owned directory must have mode 0700: {path}")


@dataclass(frozen=True, slots=True)
class ProfileRoots:
    """Versioned application roots below the four XDG base directories."""

    config: Path
    state: Path
    data: Path
    cache: Path
    owner_uid: int

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
        owner_uid: int | None = None,
    ) -> ProfileRoots:
        values = dict(os.environ if environ is None else environ)
        keys = (
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        )
        needs_home = any(not values.get(key) for key in keys)
        if home is None and needs_home:
            raw_home = values.get("HOME")
            if not raw_home:
                raise ProfilePathError("HOME is required when an XDG base is not configured")
            home = _canonical_absolute(raw_home, field="HOME")
        elif home is not None:
            home = _canonical_absolute(home, field="HOME")

        defaults = (
            {
                "XDG_CONFIG_HOME": home / ".config",
                "XDG_STATE_HOME": home / ".local/state",
                "XDG_DATA_HOME": home / ".local/share",
                "XDG_CACHE_HOME": home / ".cache",
            }
            if home is not None
            else {}
        )

        def app_root(key: str) -> Path:
            raw = values.get(key)
            base = _canonical_absolute(raw, field=key) if raw else defaults[key]
            return _canonical_absolute(base / "k1s/v1", field=f"{key} k1s root")

        return cls(
            config=app_root("XDG_CONFIG_HOME"),
            state=app_root("XDG_STATE_HOME"),
            data=app_root("XDG_DATA_HOME"),
            cache=app_root("XDG_CACHE_HOME"),
            owner_uid=os.geteuid() if owner_uid is None else owner_uid,
        )

    def ensure(self) -> None:
        """Create and validate the private application roots without repairing them."""

        for root in (self.config, self.state, self.data, self.cache):
            _ensure_private_directory(root.parent, owner_uid=self.owner_uid)
            _ensure_private_directory(root, owner_uid=self.owner_uid)


@dataclass(frozen=True, slots=True)
class ProfileLayout:
    """Exact paths owned by one canonical profile and immutable run."""

    requested_profile: str
    canonical_profile: str
    roots: ProfileRoots
    run_id: str | None = None

    @classmethod
    def resolve(
        cls,
        roots: ProfileRoots,
        requested_profile: str,
        *,
        run_id: str | None = None,
    ) -> ProfileLayout:
        resolved = resolve_profile(requested_profile)
        canonical = validate_identifier(resolved.profile.name, field="profile")
        requested = validate_identifier(requested_profile, field="requested profile")
        if run_id is not None:
            validate_run_id(run_id)
        return cls(
            requested_profile=requested,
            canonical_profile=canonical,
            roots=roots,
            run_id=run_id,
        )

    @property
    def profile_state(self) -> Path:
        return self.roots.state / "profiles" / self.canonical_profile

    @property
    def profile_config(self) -> Path:
        return self.roots.config / "profiles" / self.canonical_profile

    @property
    def bootstraps(self) -> Path:
        return self.profile_config / "bootstraps"

    def bootstrap_path(self, bootstrap_id: str) -> Path:
        return self.bootstraps / f"{validate_bootstrap_id(bootstrap_id)}.json"

    @property
    def lock(self) -> Path:
        return self.profile_state / "profile.lock"

    @property
    def current(self) -> Path:
        return self.profile_state / "current.json"

    @property
    def transaction(self) -> Path:
        """Crash-recovery journal for one exact current/run state mutation."""

        return self.profile_state / "transaction.json"

    def for_run(self, run_id: str) -> ProfileLayout:
        return ProfileLayout.resolve(
            self.roots,
            self.requested_profile,
            run_id=run_id,
        )

    def _require_run_id(self) -> str:
        if self.run_id is None:
            raise ProfilePathError("an immutable run ID is required for this path")
        return validate_run_id(self.run_id)

    @property
    def run_state(self) -> Path:
        return self.profile_state / "runs" / self._require_run_id() / "state.json"

    @property
    def run_logs(self) -> Path:
        return self.profile_state / "runs" / self._require_run_id() / "logs"

    @property
    def run_data(self) -> Path:
        return (
            self.roots.data / "profiles" / self.canonical_profile / "runs" / self._require_run_id()
        )

    @property
    def run_secrets(self) -> Path:
        return self.run_data / "secrets"

    @property
    def run_cache(self) -> Path:
        return (
            self.roots.cache / "profiles" / self.canonical_profile / "runs" / self._require_run_id()
        )

    def log_path(self, component_id: str) -> Path:
        component = validate_identifier(component_id, field="component ID")
        return self.run_logs / f"{component}.log"

    def secret_path(self, secret_id: str) -> Path:
        secret = validate_identifier(secret_id, field="secret ID")
        return self.run_secrets / secret

    def ensure_profile(self) -> None:
        self.roots.ensure()
        _ensure_private_directory(self.roots.state / "profiles", owner_uid=self.roots.owner_uid)
        _ensure_private_directory(self.profile_state, owner_uid=self.roots.owner_uid)
        _ensure_private_directory(self.profile_state / "runs", owner_uid=self.roots.owner_uid)

    def ensure_bootstrap_directory(self) -> None:
        """Create only the exact private configuration hierarchy for this role."""

        self.roots.ensure()
        _ensure_private_directory(self.roots.config / "profiles", owner_uid=self.roots.owner_uid)
        _ensure_private_directory(self.profile_config, owner_uid=self.roots.owner_uid)
        _ensure_private_directory(self.bootstraps, owner_uid=self.roots.owner_uid)

    def ensure_run(self) -> None:
        self.ensure_profile()
        for path in (
            self.run_state.parent,
            self.run_logs,
            self.run_data,
            self.run_secrets,
            self.run_cache,
        ):
            _ensure_private_directory(path, owner_uid=self.roots.owner_uid)


__all__ = [
    "ProfileLayout",
    "ProfilePathError",
    "ProfileRoots",
    "validate_bootstrap_id",
    "validate_identifier",
    "validate_relative_path",
    "validate_run_id",
]
