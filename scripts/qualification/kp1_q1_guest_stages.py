#!/usr/bin/env python3
"""Closed guest-side stages for the isolated K1s Q1 live capsule.

The program is injected by the digest-bound capsule plan and is executable only
inside the disposable guest.  It accepts a fixed prepare operation and the
fixed QH3 stage names.  Results are metadata-safe canonical JSON; command
output is represented by bounded digests rather than copied into evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import NoReturn

API_VERSION = "k1s.dev/q1-guest-stages/v1"
CONDUCTOR_API_VERSION = "k1s.dev/q1-conductor/v1"
SCHEMA_VERSION = 1
EXPECTED_VERSION = "1.0.0rc1"
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024

ROOT = Path("/home/q1/.local/share/k1s-q1")
PRIVATE_ROOT = ROOT / "runtime"
AUTHORITY_PATH = PRIVATE_ROOT / "private/authority.json"
BUNDLE_PATH = ROOT / "input/k1s-q1-input.bundle"
PAYLOAD_ROOT = ROOT / "payload"
STATE_PATH = PRIVATE_ROOT / "private/stage-state.json"
WORK_ROOT = PRIVATE_ROOT / "work"

STAGES = (
    "boot-containment",
    "offline-install",
    "pure-contracts",
    "strict-cri-prepare-rollback",
    "core-podman",
    "core-docker",
    "edge-gateway",
    "node-strict-cri",
    "node-fencing",
    "security-context",
    "workload-lifecycle",
    "workerbee-evidence",
    "crash-recovery",
    "teardown",
)


class GuestStageError(RuntimeError):
    """A guest-stage request failed its closed qualification contract."""


def _fail() -> NoReturn:
    raise GuestStageError("K1s Q1 guest stage failed closed")


def _canonical(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise GuestStageError("K1s Q1 guest stage failed closed") from exc
    return (text + "\n").encode("utf-8")


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _file_digest(path: Path, *, maximum: int = MAX_BUNDLE_BYTES) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise GuestStageError("K1s Q1 guest stage failed closed") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or not 0 < info.st_size <= maximum
    ):
        _fail()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_canonical(path: Path, *, maximum: int = 2 * 1024 * 1024) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuestStageError("K1s Q1 guest stage failed closed") from exc
    if not 0 < len(raw) <= maximum or type(value) is not dict or _canonical(value) != raw:
        _fail()
    return value


def _write_private(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _authority() -> dict[str, object]:
    value = _load_canonical(AUTHORITY_PATH)
    expected = {
        "apiVersion",
        "bundleId",
        "bundleSha256",
        "planDigest",
        "runId",
        "schemaVersion",
        "sourceCommit",
        "sourceTree",
        "stageRunnerSha256",
        "transactionId",
    }
    if (
        set(value) != expected
        or value["apiVersion"] != API_VERSION
        or value["schemaVersion"] != SCHEMA_VERSION
        or any(
            type(value[key]) is not str or not str(value[key]).startswith("sha256:")
            for key in ("bundleId", "bundleSha256", "planDigest", "stageRunnerSha256")
        )
    ):
        _fail()
    if _file_digest(Path(__file__).resolve(), maximum=1024 * 1024) != value["stageRunnerSha256"]:
        _fail()
    if _file_digest(BUNDLE_PATH) != value["bundleSha256"]:
        _fail()
    return value


def _safe_archive_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.startswith(".env") or part == ".envrc" for part in path.parts)
    ):
        _fail()
    return path


def _extract_bundle(bundle: Path, destination: Path) -> dict[str, object]:
    if destination.exists():
        _fail()
    destination.mkdir(mode=0o700, parents=True)
    try:
        with tarfile.open(bundle, "r:") as archive:
            items = archive.getmembers()
            if not items or items[0].name != "manifest.json" or len(items) > 65:
                _fail()
            if any(not item.isfile() for item in items):
                _fail()
            manifest_raw = archive.extractfile(items[0])
            if manifest_raw is None:
                _fail()
            raw = manifest_raw.read(2 * 1024 * 1024 + 1)
            manifest = json.loads(raw.decode("utf-8"))
            if type(manifest) is not dict or _canonical(manifest) != raw:
                _fail()
            rows = manifest.get("members")
            if type(rows) is not list or len(rows) != len(items) - 1:
                _fail()
            expected = {str(row.get("path")): row for row in rows if type(row) is dict}
            if len(expected) != len(rows):
                _fail()
            for item in items[1:]:
                path = _safe_archive_path(item.name)
                row = expected.get(item.name)
                if row is None or set(row) != {"mode", "path", "role", "sha256", "size"}:
                    _fail()
                if item.size != row["size"] or not 0 < item.size <= MAX_MEMBER_BYTES:
                    _fail()
                source = archive.extractfile(item)
                if source is None:
                    _fail()
                member_raw = source.read(item.size + 1)
                if len(member_raw) != item.size or _digest(member_raw) != row["sha256"]:
                    _fail()
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_private(
                    target,
                    member_raw,
                    mode=0o755 if row["mode"] == "0755" else 0o600,
                )
    except (OSError, KeyError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError):
        _fail()
    return manifest


def _prepare(authority_in: Path, bundle_in: Path) -> dict[str, object]:
    if (
        os.geteuid() != pwd.getpwnam("q1").pw_uid
        or AUTHORITY_PATH.exists()
        or BUNDLE_PATH.exists()
        or PAYLOAD_ROOT.exists()
    ):
        _fail()
    authority = _load_canonical(authority_in)
    runner_digest = _file_digest(Path(__file__).resolve(), maximum=1024 * 1024)
    if authority.get("stageRunnerSha256") != runner_digest:
        _fail()
    bundle_digest = _file_digest(bundle_in)
    if authority.get("bundleSha256") != bundle_digest:
        _fail()
    for directory in (ROOT, ROOT / "input", PRIVATE_ROOT, PRIVATE_ROOT / "private", WORK_ROOT):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    _write_private(AUTHORITY_PATH, _canonical(authority))
    with bundle_in.open("rb") as source:
        _write_private(BUNDLE_PATH, source.read(MAX_BUNDLE_BYTES + 1))
    manifest = _extract_bundle(BUNDLE_PATH, PAYLOAD_ROOT)
    validator = PAYLOAD_ROOT / "payload/helpers/kp1_q1_bundle.py"
    command = (
        "/usr/bin/python3.11",
        str(validator),
        "verify",
        "--bundle",
        str(BUNDLE_PATH),
        "--expect-bundle-sha256",
        str(authority["bundleSha256"]),
        "--run-id",
        str(authority["runId"]),
        "--plan-digest",
        str(authority["planDigest"]),
        "--bundle-id",
        str(authority["bundleId"]),
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and validated argv
        command, capture_output=True, check=False, timeout=60
    )
    if completed.returncode != 0:
        _fail()
    state = {
        "apiVersion": API_VERSION,
        "bundleManifestSha256": _digest(_canonical(manifest)),
        "completedStages": [],
        "kind": "K1sQ1GuestStageState",
        "schemaVersion": SCHEMA_VERSION,
    }
    _write_private(STATE_PATH, _canonical(state))
    return _result("prepare", [("bundle-verified", completed.stdout + completed.stderr)])


def _state() -> dict[str, object]:
    value = _load_canonical(STATE_PATH)
    if set(value) != {
        "apiVersion",
        "bundleManifestSha256",
        "completedStages",
        "kind",
        "schemaVersion",
    }:
        _fail()
    completed = value["completedStages"]
    if type(completed) is not list or any(item not in STAGES for item in completed):
        _fail()
    return value


def _record_stage(stage: str) -> None:
    value = _state()
    completed = list(value["completedStages"])
    expected_index = len(completed)
    if stage in completed or expected_index >= len(STAGES) or STAGES[expected_index] != stage:
        _fail()
    completed.append(stage)
    value["completedStages"] = completed
    pending = STATE_PATH.with_suffix(".pending")
    _write_private(pending, _canonical(value))
    os.replace(pending, STATE_PATH)


def _clean_environment(root: Path, *, backend: str | None = None) -> dict[str, str]:
    env = {
        "HOME": str(root / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": f"{WORK_ROOT}/offline-install/venv/bin:/usr/bin:/bin",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
    }
    if backend is not None:
        env["AE_RUNTIME_BACKEND"] = backend
    for path in (
        Path(env["HOME"]),
        Path(env["XDG_CACHE_HOME"]),
        Path(env["XDG_CONFIG_HOME"]),
        Path(env["XDG_DATA_HOME"]),
        Path(env["XDG_STATE_HOME"]),
    ):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return env


def _run(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 300,
) -> bytes:
    try:
        completed = subprocess.run(  # noqa: S603 - closed stage argv
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            check=False,
            timeout=timeout,
            preexec_fn=None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuestStageError("K1s Q1 guest stage failed closed") from exc
    output = completed.stdout + completed.stderr
    if completed.returncode != 0 or len(output) > MAX_OUTPUT_BYTES:
        _fail()
    return output


def _json_command(
    argv: tuple[str, ...], *, root: Path, backend: str | None = None
) -> tuple[dict[str, object], bytes]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = _run(
        argv,
        cwd=root,
        env=_clean_environment(root, backend=backend),
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuestStageError("K1s Q1 guest stage failed closed") from exc
    if type(value) is not dict:
        _fail()
    return value, raw


def _result(stage: str, checks: list[tuple[str, bytes]]) -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "checks": [
            {"name": name, "sha256": _digest(raw), "size": len(raw), "status": "passed"}
            for name, raw in checks
        ],
        "kind": "K1sQ1GuestStageResult",
        "schemaVersion": SCHEMA_VERSION,
        "stage": stage,
        "status": "passed",
    }


def _stage_boot() -> dict[str, object]:
    if not Path("/etc/k1s-q1-egress-denied").is_file() or os.geteuid() != pwd.getpwnam("q1").pw_uid:
        _fail()
    checks: list[tuple[str, bytes]] = []
    for name, probe in (
        ("dns-denied", lambda: socket.getaddrinfo("example.com", 443)),
        ("egress-denied", lambda: socket.create_connection(("1.1.1.1", 443), timeout=2)),
    ):
        try:
            probe()
        except OSError as exc:
            checks.append((name, type(exc).__name__.encode("ascii")))
        else:
            _fail()
    checks.append(("bundle-authority", _canonical(_authority())))
    return _result("boot-containment", checks)


def _stage_offline_install() -> dict[str, object]:
    root = WORK_ROOT / "offline-install"
    validator = PAYLOAD_ROOT / "payload/helpers/validate_installed_python_artifact.py"
    wheel = PAYLOAD_ROOT / "payload/k1s/k1s_core-1.0.0rc1-py3-none-any.whl"
    wheelhouse = PAYLOAD_ROOT / "payload/offline/wheels"
    lock = PAYLOAD_ROOT / "payload/offline/requirements-release.lock"
    output = _run(
        (
            "/usr/bin/python3.11",
            str(validator),
            "--wheel",
            str(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--requirements-lock",
            str(lock),
            "--work-root",
            str(root),
            "--expected-version",
            EXPECTED_VERSION,
        ),
        cwd=WORK_ROOT,
        env=_clean_environment(WORK_ROOT),
        timeout=600,
    )
    return _result("offline-install", [("installed-artifact", output)])


def _installed_python() -> Path:
    path = WORK_ROOT / "offline-install/venv/bin/python"
    if not path.is_file():
        _fail()
    return path


def _k1s() -> Path:
    path = WORK_ROOT / "offline-install/venv/bin/k1s"
    if not path.is_file():
        _fail()
    return path


def _stage_pure() -> dict[str, object]:
    root = WORK_ROOT / "pure-contracts"
    checks = []
    for argv in (
        (str(_k1s()), "profile", "list", "--output", "json"),
        (str(_k1s()), "profile", "plan", "core", "--output", "json"),
        (str(_k1s()), "profile", "doctor", "core", "--output", "json"),
    ):
        value, raw = _json_command(argv, root=root)
        if value.get("mutating") is True:
            _fail()
        checks.append((argv[2], raw))
    imported = _run(
        (str(_installed_python()), "-c", "import ae; assert ae.__version__ == '1.0.0rc1'"),
        cwd=root,
        env=_clean_environment(root),
    )
    checks.append(("installed-import", imported))
    return _result("pure-contracts", checks)


def _stage_strict_cri() -> dict[str, object]:
    root = WORK_ROOT / "strict-cri"
    program = (
        "from ae.kctl.profile_host_helper import helper_implementation_digest;"
        "from ae.kctl.profile_host_plan import strict_cri_action_materials;"
        "from ae.kctl.profile_node_adapter import StrictCRINodeInputs;"
        "assert helper_implementation_digest().startswith('sha256:');"
        "assert callable(strict_cri_action_materials); assert StrictCRINodeInputs"
    )
    raw = _run(
        (str(_installed_python()), "-c", program),
        cwd=root,
        env=_clean_environment(root),
    )
    return _result("strict-cri-prepare-rollback", [("installed-strict-cri-contract", raw)])


def _profile_lifecycle(stage: str, backend: str) -> dict[str, object]:
    root = WORK_ROOT / stage
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    checks: list[tuple[str, bytes]] = []
    start, raw = _json_command(
        (str(_k1s()), "profile", "start", "core", "--backend", backend, "--output", "json"),
        root=root,
        backend=backend,
    )
    run_id = start.get("run_id")
    if start.get("status") not in {"started", "running"} or type(run_id) is not str:
        _fail()
    checks.append(("start", raw))
    for action in ("status", "restart", "status", "stop"):
        argv = [str(_k1s()), "profile", action, "core"]
        if action == "restart":
            argv.extend(("--backend", backend))
        argv.extend(("--output", "json"))
        value, raw = _json_command(tuple(argv), root=root, backend=backend)
        if value.get("exit_code") != 0:
            _fail()
        checks.append((action, raw))
    current, _ = _json_command(
        (str(_k1s()), "profile", "status", "core", "--output", "json"),
        root=root,
        backend=backend,
    )
    current_run = current.get("run_id")
    if type(current_run) is not str or current.get("phase") != "stopped":
        _fail()
    deleted, raw = _json_command(
        (
            str(_k1s()),
            "profile",
            "delete",
            "core",
            "--run-id",
            current_run,
            "--confirm-delete",
            f"core/{current_run}",
            "--output",
            "json",
        ),
        root=root,
        backend=backend,
    )
    if deleted.get("status") not in {"deleted", "already-deleted"}:
        _fail()
    checks.append(("delete", raw))
    return _result(stage, checks)


def _stage_edge_gateway() -> dict[str, object]:
    root = WORK_ROOT / "edge-gateway"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    credential = root / "gateway.credential"
    credential.write_text("q1-offline-gateway-credential\n", encoding="utf-8")
    credential.chmod(0o600)
    value, raw = _json_command(
        (
            str(_k1s()),
            "profile",
            "bootstrap",
            "create",
            "edge-gateway",
            "--site-id",
            "q1-site",
            "--node-id",
            "q1-gateway",
            "--transport-url",
            "tls://127.0.0.1:4222",
            "--credential-file",
            str(credential),
            "--output",
            "json",
        ),
        root=root,
    )
    if value.get("profile") != "edge-gateway" or type(value.get("bootstrapId")) is not str:
        _fail()
    return _result("edge-gateway", [("bootstrap", raw)])


def _stage_node_strict_cri() -> dict[str, object]:
    root = WORK_ROOT / "node-strict-cri"
    value, raw = _json_command(
        (str(_k1s()), "profile", "plan", "node", "--output", "json"), root=root
    )
    if value.get("canonicalName") != "node" or value.get("executionStatus") != "portable":
        _fail()
    return _result("node-strict-cri", [("node-plan", raw)])


def _stage_node_fencing() -> dict[str, object]:
    root = WORK_ROOT / "node-fencing"
    program = (
        "from datetime import datetime,timedelta,timezone;"
        "from ae.controller.node_identity import identity_view;"
        "n=datetime.now(timezone.utc);"
        "a=identity_view('q1-node',generation=1,lease_expires_at=n+timedelta(seconds=40));"
        "b=identity_view('q1-node',generation=2,lease_expires_at=n+timedelta(seconds=80));"
        "assert a.identity_mode == 'explicit' and a.identity_version == 1;"
        "assert a.generation == 1 and b.generation == 2 and b.node_id == a.node_id"
    )
    raw = _run(
        (str(_installed_python()), "-c", program),
        cwd=root,
        env=_clean_environment(root),
    )
    return _result("node-fencing", [("lease-generation", raw)])


def _stage_security_context() -> dict[str, object]:
    root = WORK_ROOT / "security-context"
    program = (
        "from ae.controller.spec import AppManifest;"
        "from ae.k8s.exporter import ExportOptions,export_k8s_docs;"
        "m=AppManifest.model_validate({'apiVersion':'ae.dev/v1alpha1','kind':'Deployment',"
        "'metadata':{'name':'q1-security','namespace':'q1'},'spec':{'image':'q1.invalid/offline:sealed',"
        "'security':{'runAsUser':1000,'runAsGroup':1000,'readOnlyRootFilesystem':True,"
        "'dropCapabilities':['ALL'],'seccompProfileType':'RuntimeDefault'}}});"
        "docs=export_k8s_docs(m,options=ExportOptions(namespace='q1'));"
        "d=next(x for x in docs if x['kind']=='Deployment');"
        "v=d['spec']['template']['spec']['containers'][0]['securityContext'];"
        "assert v['runAsUser']==1000 and v['readOnlyRootFilesystem'] is True;"
        "assert v['capabilities']['drop']==['ALL'];"
        "assert v['seccompProfile']['type']=='RuntimeDefault'"
    )
    raw = _run(
        (str(_installed_python()), "-c", program),
        cwd=root,
        env=_clean_environment(root),
    )
    return _result("security-context", [("security-projection", raw)])


def _stage_workload() -> dict[str, object]:
    checks: list[tuple[str, bytes]] = []
    for backend, executable in (("podman", "/usr/bin/podman"), ("docker", "/usr/bin/docker")):
        raw = _run(
            (executable, "version"),
            cwd=WORK_ROOT,
            env=_clean_environment(WORK_ROOT, backend=backend),
            timeout=60,
        )
        checks.append((f"{backend}-version", raw))
    return _result("workload-lifecycle", checks)


def _stage_workerbee() -> dict[str, object]:
    root = WORK_ROOT / "workerbee-evidence"
    program = (
        "from ae.kctl.profile_adapter import adapt_workerbee_profile_evidence;"
        "from ae.kctl.profile_adapter import verify_workerbee_profile_evidence;"
        "from ae.kctl.profiles import profile_catalog_payload;"
        "v=adapt_workerbee_profile_evidence('list',profile_catalog_payload(),source_exit_code=0);"
        "assert verify_workerbee_profile_evidence(v)==v"
    )
    raw = _run(
        (str(_installed_python()), "-c", program),
        cwd=root,
        env=_clean_environment(root),
    )
    return _result("workerbee-evidence", [("profile-evidence", raw)])


def _stage_crash_recovery() -> dict[str, object]:
    root = WORK_ROOT / "crash-recovery"
    program = (
        "from ae.kctl.profile_state import ProfileStateStore;"
        "from ae.kctl.profile_paths import ProfileRoots;"
        "r=ProfileRoots.from_environment(); s=ProfileStateStore(r,'local');"
        "assert s.inspect() is None"
    )
    raw = _run(
        (str(_installed_python()), "-c", program),
        cwd=root,
        env=_clean_environment(root),
    )
    return _result("crash-recovery", [("empty-state-recovery", raw)])


def _stage_teardown() -> dict[str, object]:
    checks: list[tuple[str, bytes]] = []
    for path in sorted(WORK_ROOT.glob("*/state/k1s/v1/profiles/*/current.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _fail()
        if type(payload) is not dict or type(payload.get("runId")) is not str:
            _fail()
        checks.append(("terminal-profile-state", _canonical(payload)))
    return _result("teardown", checks or [("no-profile-residue", b"")])


STAGE_HANDLERS = {
    "boot-containment": _stage_boot,
    "offline-install": _stage_offline_install,
    "pure-contracts": _stage_pure,
    "strict-cri-prepare-rollback": _stage_strict_cri,
    "core-podman": lambda: _profile_lifecycle("core-podman", "podman"),
    "core-docker": lambda: _profile_lifecycle("core-docker", "docker"),
    "edge-gateway": _stage_edge_gateway,
    "node-strict-cri": _stage_node_strict_cri,
    "node-fencing": _stage_node_fencing,
    "security-context": _stage_security_context,
    "workload-lifecycle": _stage_workload,
    "workerbee-evidence": _stage_workerbee,
    "crash-recovery": _stage_crash_recovery,
    "teardown": _stage_teardown,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--stage", choices=STAGES)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--authority-in", type=Path)
    parser.add_argument("--bundle-in", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    ns = _parser().parse_args(argv)
    if ns.protocol != CONDUCTOR_API_VERSION or os.geteuid() != pwd.getpwnam("q1").pw_uid:
        _fail()
    if ns.prepare:
        if ns.authority_in is None or ns.bundle_in is None:
            _fail()
        result = _prepare(ns.authority_in.resolve(), ns.bundle_in.resolve())
    else:
        if ns.authority_in is not None or ns.bundle_in is not None or ns.stage not in STAGES:
            _fail()
        _authority()
        result = STAGE_HANDLERS[ns.stage]()
        _record_stage(ns.stage)
    sys.stdout.buffer.write(_canonical(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuestStageError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
