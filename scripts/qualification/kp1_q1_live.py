#!/usr/bin/env python3
"""Live SSH/QMP/process adapters for the isolated K1s Q1 capsule.

This module connects the previously synthetic QH1-QH4 contracts.  It does not
select artifacts, repair host prerequisites, grant KVM access, generate a Gate
A attestation, publish a release, or discover a VM by name.  The caller must
supply an already canonical plan, bundle, authority, Gate A documents, and SSH
private key.  Every runtime object is addressed through those exact inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NoReturn, cast

try:
    from scripts.qualification import kp1_q1_capsule as q1_capsule
    from scripts.qualification import kp1_q1_conductor as q1_conductor
    from scripts.qualification import kp1_q1_teardown as q1_teardown
except ModuleNotFoundError:  # Direct execution from scripts/qualification.
    import kp1_q1_capsule as q1_capsule  # type: ignore[import-not-found,no-redef]
    import kp1_q1_conductor as q1_conductor  # type: ignore[import-not-found,no-redef]
    import kp1_q1_teardown as q1_teardown  # type: ignore[import-not-found,no-redef]

API_VERSION = "k1s.dev/q1-live/v1"
SCHEMA_VERSION = 1
MAX_TRANSPORT_OUTPUT = q1_conductor.MAX_STAGE_OUTPUT_BYTES
SSH_READY_SECONDS = 180
SSH_COMMAND_SECONDS = 600


class LivePolicyError(RuntimeError):
    """A live adapter or exact-runtime relation failed closed."""


def _fail() -> NoReturn:
    raise LivePolicyError("K1s Q1 live qualification failed closed")


def canonical_json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise LivePolicyError("K1s Q1 live qualification failed closed") from exc
    return (text + "\n").encode("utf-8")


def digest_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def file_digest(path: Path, *, maximum: int = 2 * 1024 * 1024 * 1024) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise LivePolicyError("K1s Q1 live qualification failed closed") from exc
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


def _write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner:
    """Run an exact argv with no shell and bounded captured output."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        stdin: bytes | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> Completed:
        try:
            result = subprocess.run(  # noqa: S603 - exact argv is caller-bound
                list(argv),
                input=stdin,
                capture_output=True,
                check=False,
                close_fds=True,
                env=dict(environment) if environment is not None else _clean_environment(),
                start_new_session=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LivePolicyError("K1s Q1 live qualification failed closed") from exc
        if len(result.stdout) > MAX_TRANSPORT_OUTPUT or len(result.stderr) > MAX_TRANSPORT_OUTPUT:
            _fail()
        return Completed(result.returncode, result.stdout, result.stderr)


def _clean_environment() -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
    }


@dataclass(frozen=True)
class SshTarget:
    port: int
    private_key: Path
    known_hosts: Path
    user: str = "q1"

    def common_argv(self) -> tuple[str, ...]:
        if not 1024 <= self.port <= 65535 or self.user != "q1":
            _fail()
        return (
            "/usr/bin/ssh",
            "-F",
            "/dev/null",
            "-i",
            str(self.private_key),
            "-p",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "ConnectTimeout=5",
            f"{self.user}@127.0.0.1",
        )


class SshTransport:
    def __init__(self, target: SshTarget, *, runner: CommandRunner | None = None) -> None:
        self.target = target
        self.runner = runner or CommandRunner()

    def establish_host_key(self, *, deadline_monotonic: float) -> None:
        if self.target.known_hosts.exists():
            _fail()
        keyscan = shutil.which("ssh-keyscan")
        if keyscan is None:
            _fail()
        while time.monotonic() < deadline_monotonic:
            result = self.runner.run(
                (keyscan, "-T", "5", "-p", str(self.target.port), "127.0.0.1"),
                timeout=10,
            )
            lines = [
                line for line in result.stdout.splitlines() if line and not line.startswith(b"#")
            ]
            if result.returncode == 0 and lines:
                prefix = f"[127.0.0.1]:{self.target.port} ".encode()
                if any(not line.startswith(prefix) for line in lines):
                    _fail()
                _write_private(self.target.known_hosts, b"\n".join(lines) + b"\n")
                return
            time.sleep(1)
        _fail()

    def wait_ready(self, *, deadline_monotonic: float) -> None:
        while time.monotonic() < deadline_monotonic:
            result = self.runner.run(
                (*self.target.common_argv(), "--", "/usr/bin/true"), timeout=10
            )
            if result.returncode == 0:
                return
            time.sleep(1)
        _fail()

    def copy(self, source: Path, remote_name: str) -> None:
        if remote_name not in {"authority.json", "k1s-q1-input.bundle"}:
            _fail()
        scp = shutil.which("scp")
        if scp is None:
            _fail()
        result = self.runner.run(
            (
                scp,
                "-F",
                "/dev/null",
                "-i",
                str(self.target.private_key),
                "-P",
                str(self.target.port),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={self.target.known_hosts}",
                str(source),
                f"q1@127.0.0.1:/home/q1/{remote_name}",
            ),
            timeout=SSH_COMMAND_SECONDS,
        )
        if result.returncode != 0:
            _fail()

    def execute(self, argv: Sequence[str], *, timeout: int = SSH_COMMAND_SECONDS) -> Completed:
        if not argv or any(not item or "\x00" in item for item in argv):
            _fail()
        result = self.runner.run((*self.target.common_argv(), "--", *argv), timeout=timeout)
        return result

    def prepare(self) -> bytes:
        result = self.execute(
            (
                "/usr/bin/python3.11",
                "/opt/k1s-q1/stages/kp1_q1_guest_stages.py",
                "--prepare",
                "--protocol",
                q1_conductor.API_VERSION,
                "--authority-in",
                "/home/q1/authority.json",
                "--bundle-in",
                "/home/q1/k1s-q1-input.bundle",
            )
        )
        if result.returncode != 0:
            _fail()
        return result.stdout


class SshStageAdapter:
    def __init__(self, transport: SshTransport) -> None:
        self.transport = transport

    def execute(self, request: q1_conductor.StageRequest) -> q1_conductor.StageObservation:
        timeout = max(
            1,
            min(
                request.stage.timeout_seconds,
                int(request.deadline_monotonic - time.monotonic()),
            ),
        )
        result = self.transport.execute(request.stage.argv, timeout=timeout)
        output = result.stdout + result.stderr
        evidence = (
            q1_conductor.EvidenceItem(
                kind="guest-stage-result", sha256=digest_bytes(output), size=len(output)
            ),
        )
        return q1_conductor.StageObservation(
            identity=dict(request.authority),
            exit_code=result.returncode,
            timed_out=False,
            stdout=result.stdout,
            stderr=result.stderr,
            evidence=evidence,
        )


class QmpJsonClient:
    def __init__(self, socket_path: Path) -> None:
        if not socket_path.is_absolute():
            _fail()
        self.socket_path = socket_path

    @staticmethod
    def _recv_json(
        stream: BinaryIO,
        maximum: int,
        accept: Callable[[dict[str, object]], bool],
    ) -> dict[str, object]:
        for _ in range(32):
            raw = stream.readline(maximum + 1)
            if not raw or len(raw) > maximum or not raw.endswith(b"\n"):
                _fail()
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _fail()
            if type(value) is dict:
                typed = cast(dict[str, object], value)
                if accept(typed):
                    return typed
        _fail()

    def command(self, command: str, *, timeout: float, maximum: int) -> dict[str, object]:
        if command not in {"query-status", "system_powerdown", "quit"}:
            _fail()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as handle:
                handle.settimeout(timeout)
                handle.connect(str(self.socket_path))
                with handle.makefile("rb") as stream:
                    self._recv_json(stream, maximum, lambda value: "QMP" in value)
                    handle.sendall(b'{"execute":"qmp_capabilities"}\n')
                    capabilities = self._recv_json(
                        stream,
                        maximum,
                        lambda value: "return" in value or "error" in value,
                    )
                    if "error" in capabilities:
                        _fail()
                    request = canonical_json_bytes({"execute": command})
                    handle.sendall(request)
                    response = self._recv_json(
                        stream,
                        maximum,
                        lambda value: "return" in value or "error" in value,
                    )
        except (OSError, TimeoutError) as exc:
            raise LivePolicyError("K1s Q1 live qualification failed closed") from exc
        if "error" in response or "return" not in response:
            _fail()
        return response


class QmpStageAdapter:
    """Run the closed guest stage and bind it to a live QMP status query."""

    def __init__(self, qmp: QmpJsonClient, ssh: SshTransport) -> None:
        self.qmp = qmp
        self.ssh = ssh

    def execute(self, request: q1_conductor.StageRequest) -> q1_conductor.StageObservation:
        stage_argv = (
            "/usr/bin/python3.11",
            "/opt/k1s-q1/stages/kp1_q1_guest_stages.py",
            "--stage",
            request.stage.name,
            "--protocol",
            q1_conductor.API_VERSION,
        )
        guest = self.ssh.execute(stage_argv, timeout=request.stage.timeout_seconds)
        response = self.qmp.command(
            "query-status",
            timeout=max(1.0, request.deadline_monotonic - time.monotonic()),
            maximum=64 * 1024,
        )
        qmp_raw = canonical_json_bytes(response)
        output = guest.stdout + qmp_raw
        return q1_conductor.StageObservation(
            identity=dict(request.authority),
            exit_code=guest.returncode,
            timed_out=False,
            stdout=output,
            stderr=guest.stderr,
            evidence=(
                q1_conductor.EvidenceItem(
                    kind="guest-qmp-stage", sha256=digest_bytes(output), size=len(output)
                ),
            ),
        )


class FileBundleObserver:
    def __init__(self, path: Path) -> None:
        self.path = path

    def sha256(self) -> str:
        return file_digest(self.path)


class FileReplayLedger:
    def __init__(self, root: Path) -> None:
        self.root = root

    def claim(self, receipt_id: str) -> bool:
        if not receipt_id.startswith("sha256:"):
            return False
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / (receipt_id.removeprefix("sha256:") + ".claim")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            raise LivePolicyError("K1s Q1 live qualification failed closed") from exc
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write((receipt_id + "\n").encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        return True


class ProcObserver:
    @staticmethod
    def _start_ticks(raw: str) -> int:
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split()
        if closing < 0 or len(fields) < 20:
            _fail()
        return int(fields[19])

    def observe(self, pid: int) -> q1_teardown.ProcessIdentity | None:
        root = Path("/proc") / str(pid)
        try:
            stat_raw = (root / "stat").read_text(encoding="utf-8")
            executable = os.readlink(root / "exe")
            argv_raw = (root / "cmdline").read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LivePolicyError("K1s Q1 live qualification failed closed") from exc
        argv = tuple(part.decode("utf-8") for part in argv_raw.split(b"\0") if part)
        if not argv:
            _fail()
        if argv[0] != executable:
            if Path(argv[0]).resolve() != Path(executable).resolve():
                _fail()
            argv = (executable, *argv[1:])
        return q1_teardown.ProcessIdentity(
            pid=pid,
            start_time_ticks=self._start_ticks(stat_raw),
            executable=executable,
            argv=argv,
        )


class TeardownQmpAdapter:
    def __init__(self, client: QmpJsonClient) -> None:
        self.client = client
        self.powerdown_sent = False

    def execute(self, request: q1_teardown.QmpRequest) -> q1_teardown.QmpResponse:
        if self.powerdown_sent and request.command == "query-status":
            time.sleep(2)
        response = self.client.command(
            request.command,
            timeout=max(1.0, request.deadline_monotonic - time.monotonic()),
            maximum=request.maximum_response_bytes,
        )
        payload = canonical_json_bytes(response)
        returned = cast(dict[str, object], response["return"])
        if request.command == "query-status":
            status = returned.get("status")
            if type(status) is not str:
                _fail()
        else:
            status = q1_teardown.QMP_ACK_STATUS
        if request.command == "system_powerdown":
            self.powerdown_sent = True
        return q1_teardown.QmpResponse(request.command, cast(str, status), payload)


class SlowProcObserver(ProcObserver):
    def observe(self, pid: int) -> q1_teardown.ProcessIdentity | None:
        result = super().observe(pid)
        if result is not None:
            time.sleep(0.25)
        return result


def _inventory_entries() -> tuple[q1_teardown.InventoryEntry, ...]:
    entries: list[q1_teardown.InventoryEntry] = []
    for interface in sorted(Path("/sys/class/net").iterdir(), key=lambda path: path.name):
        try:
            ifindex = (interface / "ifindex").read_text(encoding="ascii").strip()
        except OSError:
            _fail()
        identity = digest_bytes(
            b"k1s-q1-network-interface\0" + f"{interface.name}:{ifindex}".encode("ascii")
        )
        entries.append(q1_teardown.InventoryEntry("network-interface", identity))
    for path in (Path("/run/containerd/containerd.sock"), Path("/var/run/docker.sock")):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail()
        identity = digest_bytes(
            b"k1s-q1-runtime-object\0"
            + canonical_json_bytes(
                {
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "mode": stat.S_IMODE(info.st_mode),
                    "name": path.name,
                }
            )
        )
        entries.append(q1_teardown.InventoryEntry("runtime-object", identity))
    return tuple(entries)


def _process_identity(run_root: Path) -> q1_teardown.ProcessIdentity:
    try:
        raw = (run_root / "vm/qemu.pid").read_text(encoding="ascii").strip()
        pid = int(raw)
    except (OSError, ValueError) as exc:
        raise LivePolicyError("K1s Q1 live qualification failed closed") from exc
    observed = ProcObserver().observe(pid)
    if observed is None:
        _fail()
    return observed


def _remove_verified_run_root(root: Path) -> None:
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        _fail()
    for child in root.iterdir():
        child_info = child.lstat()
        if (
            not stat.S_ISDIR(child_info.st_mode)
            or stat.S_ISLNK(child_info.st_mode)
            or child_info.st_uid != os.getuid()
            or stat.S_IMODE(child_info.st_mode) != 0o700
        ):
            _fail()
        q1_capsule._remove_exact_tree(child, expected_parent=root)  # noqa: SLF001
    root.rmdir()


def _write_evidence(root: Path, name: str, value: object) -> None:
    _write_private(root / name, q1_teardown.canonical_json_bytes(value))


def _exact_process_present(observer: ProcObserver, expected: q1_teardown.ProcessIdentity) -> bool:
    observed = observer.observe(expected.pid)
    if observed is None:
        return False
    if observed.private_payload() != expected.private_payload():
        _fail()
    return True


def _retain_failed_runtime_evidence(run_root: Path, retained_evidence_root: Path) -> None:
    root_info = run_root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        _fail()
    vm_info = (run_root / "vm").lstat()
    if (
        not stat.S_ISDIR(vm_info.st_mode)
        or stat.S_ISLNK(vm_info.st_mode)
        or vm_info.st_uid != os.getuid()
        or vm_info.st_dev != root_info.st_dev
        or stat.S_IMODE(vm_info.st_mode) != 0o700
    ):
        _fail()
    retained: list[dict[str, object]] = []
    for source_name, retained_name in (
        ("serial.log", "failed-serial.log"),
        ("qemu-host.stderr", "failed-qemu-host.stderr"),
    ):
        source = run_root / "vm" / source_name
        if not source.exists() and not source.is_symlink():
            continue
        info = source.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_dev != root_info.st_dev
            or not 0 < info.st_size <= q1_teardown.MAX_EVIDENCE_MEMBER_BYTES
        ):
            _fail()
        raw = source.read_bytes()
        observed = source.lstat()
        if (
            len(raw) != info.st_size
            or observed.st_dev != info.st_dev
            or observed.st_ino != info.st_ino
            or observed.st_mode != info.st_mode
            or observed.st_uid != info.st_uid
            or observed.st_size != info.st_size
        ):
            _fail()
        _write_private(retained_evidence_root / retained_name, raw)
        retained.append(
            {
                "name": retained_name,
                "sha256": digest_bytes(raw),
                "size": len(raw),
            }
        )
    _write_evidence(
        retained_evidence_root,
        "failed-runtime-evidence.json",
        {
            "apiVersion": API_VERSION,
            "kind": "K1sQ1FailedRuntimeEvidence",
            "members": retained,
            "schemaVersion": SCHEMA_VERSION,
        },
    )


def _recover_failed_capsule(
    *,
    run_root: Path,
    capsule_parent: Path,
    run_id: str,
    process: q1_teardown.ProcessIdentity,
    qmp_client: QmpJsonClient,
    retained_evidence_root: Path,
) -> None:
    """Stop and remove one exact failed capsule without process discovery."""

    observer = ProcObserver()
    if _exact_process_present(observer, process):
        qmp_client.command("quit", timeout=10, maximum=64 * 1024)
        for _ in range(40):
            if not _exact_process_present(observer, process):
                break
            time.sleep(0.25)
        else:
            _fail()
    _retain_failed_runtime_evidence(run_root, retained_evidence_root)
    run_receipt = run_root / "guest/run.json"
    run_receipt_digest = file_digest(run_receipt, maximum=q1_capsule.MAX_JSON_BYTES)
    run_receipt_value = json.loads(run_receipt.read_text(encoding="utf-8"))
    cleanup_receipt = q1_capsule.teardown_private_layout(
        capsule_parent,
        expect_run_id=run_id,
        expected_run_receipt_file_digest=run_receipt_digest,
        confirmation=f"{run_id}:{run_receipt_value['receiptDigest']}",
        process_absent=True,
    )
    q1_capsule.verify_clean(
        capsule_parent,
        expect_run_id=run_id,
        receipt_path=cleanup_receipt,
        expected_receipt_file_digest=file_digest(
            cleanup_receipt, maximum=q1_capsule.MAX_JSON_BYTES
        ),
    )
    _remove_verified_run_root(run_root)
    _write_evidence(
        retained_evidence_root,
        "failed-run-cleanup.json",
        {
            "apiVersion": API_VERSION,
            "kind": "K1sQ1FailedRunCleanup",
            "processIdentityDigest": process.digest(),
            "recoveredAtUnix": int(time.time()),
            "runId": run_id,
            "schemaVersion": SCHEMA_VERSION,
            "status": "stopped-and-removed",
        },
    )


def _execute_live_unguarded(
    *,
    plan: Mapping[str, object],
    expected_plan_digest: str,
    host_preflight_path: Path,
    expected_host_preflight_digest: str,
    gate_a_path: Path,
    expected_gate_a_digest: str,
    input_bundle_path: Path,
    expected_input_bundle_digest: str,
    expected_bundle_id: str,
    ssh_private_key: Path,
    authority: q1_conductor.RunAuthority,
    retained_evidence_root: Path,
    replay_ledger_root: Path,
    failure_context: dict[str, object],
) -> dict[str, object]:
    """Launch, execute, tear down, seal, and replay one exact live authority."""

    verified_plan = q1_capsule.verify_plan(dict(plan))
    authority_value = authority.as_dict()
    if (
        authority_value["runId"] != verified_plan["runId"]
        or authority_value["planDigest"] != expected_plan_digest
        or authority_value["bundleSha256"] != expected_input_bundle_digest
        or authority_value["bundleId"] != expected_bundle_id
        or retained_evidence_root.exists()
        or replay_ledger_root.exists()
    ):
        _fail()
    retained_evidence_root.mkdir(mode=0o700, parents=True)
    replay_ledger_root.mkdir(mode=0o700, parents=True)
    before = q1_teardown.build_inventory(
        authority_value, captured_at_unix=int(time.time()), entries=_inventory_entries()
    )
    run_id = cast(str, authority_value["runId"])
    capsule_parent = Path(cast(dict[str, object], verified_plan["capsuleParent"])["path"])
    run_root = q1_capsule.execute_plan(
        verified_plan,
        expected_plan_digest=expected_plan_digest,
        confirmation=f"{run_id}:{expected_plan_digest}",
        host_preflight_path=host_preflight_path,
        expected_host_preflight_digest=expected_host_preflight_digest,
        gate_a_path=gate_a_path,
        expected_gate_a_digest=expected_gate_a_digest,
        input_bundle_path=input_bundle_path,
        expected_input_bundle_digest=expected_input_bundle_digest,
        expected_bundle_id=expected_bundle_id,
    )
    process = _process_identity(run_root)
    qmp_client = QmpJsonClient(run_root / "vm/qmp.sock")
    failure_context.update(
        {
            "capsule_parent": capsule_parent,
            "process": process,
            "qmp_client": qmp_client,
            "retained_evidence_root": retained_evidence_root,
            "run_id": run_id,
            "run_root": run_root,
        }
    )
    network = cast(dict[str, object], verified_plan["network"])
    target = SshTarget(
        port=cast(int, network["sshHostPort"]),
        private_key=ssh_private_key,
        known_hosts=run_root / "input/known_hosts",
    )
    transport = SshTransport(target)
    deadline = time.monotonic() + SSH_READY_SECONDS
    transport.establish_host_key(deadline_monotonic=deadline)
    transport.wait_ready(deadline_monotonic=deadline)
    guest_authority = {
        "apiVersion": "k1s.dev/q1-guest-stages/v1",
        "bundleId": authority_value["bundleId"],
        "bundleSha256": authority_value["bundleSha256"],
        "planDigest": authority_value["planDigest"],
        "runId": authority_value["runId"],
        "schemaVersion": 1,
        "sourceCommit": authority_value["sourceCommit"],
        "sourceTree": authority_value["sourceTree"],
        "stageRunnerSha256": cast(
            dict[str, object],
            cast(dict[str, object], verified_plan["artifacts"])["guestStageRunner"],
        )["sha256"],
        "transactionId": authority_value["transactionId"],
    }
    authority_transfer = run_root / "input/authority.json"
    _write_private(authority_transfer, canonical_json_bytes(guest_authority))
    transport.copy(authority_transfer, "authority.json")
    transport.copy(input_bundle_path, "k1s-q1-input.bundle")
    prepare_output = transport.prepare()
    if not prepare_output:
        _fail()
    adapters = q1_conductor.AdapterSet(
        ssh=SshStageAdapter(transport),
        qmp=QmpStageAdapter(qmp_client, transport),
        process=SshStageAdapter(transport),
    )
    conductor = q1_conductor.run_conductor(
        authority,
        adapters=adapters,
        bundle=FileBundleObserver(input_bundle_path),
        replay_ledger=FileReplayLedger(replay_ledger_root),
    )
    shutdown = q1_teardown.exact_qmp_shutdown(
        authority,
        process_identity=process,
        qmp=TeardownQmpAdapter(qmp_client),
        process_observer=SlowProcObserver(),
        clock=q1_conductor.SystemClock(),
    )
    run_receipt = run_root / "guest/run.json"
    run_receipt_digest = file_digest(run_receipt, maximum=q1_capsule.MAX_JSON_BYTES)
    run_receipt_value = json.loads(run_receipt.read_text(encoding="utf-8"))
    cleanup_receipt = q1_capsule.teardown_private_layout(
        capsule_parent,
        expect_run_id=run_id,
        expected_run_receipt_file_digest=run_receipt_digest,
        confirmation=f"{run_id}:{run_receipt_value['receiptDigest']}",
        process_absent=True,
    )
    q1_capsule.verify_clean(
        capsule_parent,
        expect_run_id=run_id,
        receipt_path=cleanup_receipt,
        expected_receipt_file_digest=file_digest(
            cleanup_receipt, maximum=q1_capsule.MAX_JSON_BYTES
        ),
    )
    _remove_verified_run_root(run_root)
    failure_context.clear()
    after = q1_teardown.build_inventory(
        authority_value, captured_at_unix=int(time.time()), entries=_inventory_entries()
    )
    comparison = q1_teardown.compare_inventories(before, after, authority_value)
    zero = q1_teardown.build_zero_residue_receipt(
        capsule_parent=capsule_parent,
        retained_evidence_root=retained_evidence_root,
        authority=authority,
        shutdown_receipt=shutdown,
        inventory_before=before,
        inventory_after=after,
        checked_at_unix=int(time.time()),
    )
    for name, value in (
        ("conductor-receipt.json", conductor),
        ("inventory-after.json", after),
        ("inventory-before.json", before),
        ("inventory-comparison.json", comparison),
        ("shutdown-receipt.json", shutdown),
        ("zero-residue.json", zero),
    ):
        _write_evidence(retained_evidence_root, name, value)
    seal = q1_teardown.seal_evidence(retained_evidence_root, authority)
    seal_file_digest = file_digest(
        retained_evidence_root / "Q1-EVIDENCE-SEAL.json",
        maximum=q1_teardown.MAX_EVIDENCE_MEMBER_BYTES,
    )
    replay = q1_teardown.replay_evidence(
        retained_evidence_root,
        authority,
        expected_seal_file_sha256=seal_file_digest,
    )
    if replay != seal or conductor["status"] != "passed":
        _fail()
    return {
        "apiVersion": API_VERSION,
        "conductorReceiptDigest": conductor["receiptDigest"],
        "evidenceRootSha256": digest_bytes(str(retained_evidence_root).encode("utf-8")),
        "kind": "K1sQ1LiveQualificationResult",
        "runId": run_id,
        "schemaVersion": SCHEMA_VERSION,
        "sealDigest": seal["sealDigest"],
        "sealFileSha256": seal_file_digest,
        "status": "live-qualified",
    }


def execute_live(
    *,
    plan: Mapping[str, object],
    expected_plan_digest: str,
    host_preflight_path: Path,
    expected_host_preflight_digest: str,
    gate_a_path: Path,
    expected_gate_a_digest: str,
    input_bundle_path: Path,
    expected_input_bundle_digest: str,
    expected_bundle_id: str,
    ssh_private_key: Path,
    authority: q1_conductor.RunAuthority,
    retained_evidence_root: Path,
    replay_ledger_root: Path,
) -> dict[str, object]:
    """Run one authority and recover its exact capsule after any failure."""

    failure_context: dict[str, object] = {}
    try:
        return _execute_live_unguarded(
            plan=plan,
            expected_plan_digest=expected_plan_digest,
            host_preflight_path=host_preflight_path,
            expected_host_preflight_digest=expected_host_preflight_digest,
            gate_a_path=gate_a_path,
            expected_gate_a_digest=expected_gate_a_digest,
            input_bundle_path=input_bundle_path,
            expected_input_bundle_digest=expected_input_bundle_digest,
            expected_bundle_id=expected_bundle_id,
            ssh_private_key=ssh_private_key,
            authority=authority,
            retained_evidence_root=retained_evidence_root,
            replay_ledger_root=replay_ledger_root,
            failure_context=failure_context,
        )
    except BaseException:
        if failure_context:
            try:
                _recover_failed_capsule(
                    run_root=cast(Path, failure_context["run_root"]),
                    capsule_parent=cast(Path, failure_context["capsule_parent"]),
                    run_id=cast(str, failure_context["run_id"]),
                    process=cast(q1_teardown.ProcessIdentity, failure_context["process"]),
                    qmp_client=cast(QmpJsonClient, failure_context["qmp_client"]),
                    retained_evidence_root=cast(Path, failure_context["retained_evidence_root"]),
                )
            except BaseException as cleanup_error:
                raise LivePolicyError(
                    "K1s Q1 live qualification failed; exact capsule recovery also failed"
                ) from cleanup_error
        raise
