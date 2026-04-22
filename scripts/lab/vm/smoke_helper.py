#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SMOKE_V2_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "smoke_v2.py"
VARIANT_DOWN_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "variant_down.sh"
DEFAULT_VARIANT = ROOT / "lab" / "variants" / "ha-control-plane-core.yaml"
POLL_INTERVAL_S = 1.0
KEEPALIVE_INTERVAL_S = 50.0
CONFLICTING_PASSTHROUGH_FLAGS = {
    "--variant",
    "--run-id",
    "--down",
    "--auto-down-on-success",
    "--auto-down-on-fail",
    "--keep-on-fail",
    "--no-keep-on-fail",
    "--purge",
    "--destroy-network",
}


class SmokeHelperError(RuntimeError):
    pass


@dataclass
class StreamCapture:
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    global_phases: list[dict[str, Any]] = field(default_factory=list)
    lane_phase_status: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lane_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    ha_summary: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    crash_log: str = ""


@dataclass
class TeardownResult:
    action: str
    returncode: int = 0


@dataclass
class SeenStatus:
    global_phases: set[tuple[str, str, str]] = field(default_factory=set)
    lane_phases: set[tuple[str, str, str, str]] = field(default_factory=set)
    ha_checks: set[tuple[str, str, str]] = field(default_factory=set)


@dataclass
class SudoKeepalive:
    stop_event: threading.Event
    thread: threading.Thread

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2.0)


def utc_now() -> datetime:
    return datetime.now(UTC)


def default_run_id() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wrap smoke_v2 with live status, summary, and optional teardown"
    )
    parser.add_argument(
        "--variant",
        default=str(DEFAULT_VARIANT),
        help="Path to variant yaml (default: HA closeout variant)",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Run id (default: RUN_ID env or UTC timestamp)",
    )
    parser.add_argument(
        "--teardown",
        choices=["on-success", "always", "never"],
        default="on-success",
        help="When to run variant_down.sh",
    )
    parser.add_argument("--purge", action="store_true", help="With teardown, purge state dir")
    parser.add_argument(
        "--destroy-network",
        action="store_true",
        help="With teardown, remove bridge/NAT",
    )
    parser.add_argument(
        "--console",
        choices=["phase", "quiet", "verbose"],
        default="phase",
        help="Console output mode",
    )
    return parser


def _strip_passthrough_sentinel(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def validate_passthrough_args(args: list[str]) -> None:
    conflicts = [arg for arg in args if arg in CONFLICTING_PASSTHROUGH_FLAGS]
    if conflicts:
        joined = ", ".join(sorted(set(conflicts)))
        raise SmokeHelperError(f"conflicting smoke_v2 args must use helper flags instead: {joined}")


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = build_parser()
    args, passthrough = parser.parse_known_args(argv)
    passthrough = _strip_passthrough_sentinel(passthrough)
    validate_passthrough_args(passthrough)
    if not args.run_id:
        args.run_id = os.environ.get("RUN_ID") or default_run_id()
    return args, passthrough


def _has_flag(args: list[str], flag: str) -> bool:
    return flag in args


def _option_value(args: list[str], flag: str) -> str | None:
    for index, arg in enumerate(args):
        if arg == flag:
            if index + 1 >= len(args):
                raise SmokeHelperError(f"{flag} requires a value")
            return args[index + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return None


def output_root_for(passthrough: list[str]) -> Path:
    raw = _option_value(passthrough, "--output-root")
    if not raw:
        return ROOT / "runs"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def requires_sudo(passthrough: list[str], teardown_policy: str) -> bool:
    plan_only = _has_flag(passthrough, "--plan-only")
    skip_up = _has_flag(passthrough, "--skip-up")
    if plan_only:
        return teardown_policy == "always"
    return not skip_up or teardown_policy != "never"


def run_cmd(
    cmd: list[str], *, capture_output: bool = True, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd,
        text=True,
        capture_output=capture_output,
        check=check,
        cwd=str(ROOT),
    )


def lab_python() -> str:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    python3 = shutil.which("python3")
    if python3:
        return python3
    python = shutil.which("python")
    if python:
        return python
    raise SmokeHelperError("python3 not found on PATH")


def sudo_path() -> str:
    path = shutil.which("sudo")
    if not path:
        raise SmokeHelperError("sudo not found on PATH")
    return path


def require_sudo() -> None:
    result = run_cmd([sudo_path(), "-n", "true"], capture_output=True, check=False)
    if result.returncode != 0:
        raise SmokeHelperError("local sudo credentials are required; run 'sudo -v' and retry")


def start_sudo_keepalive() -> SudoKeepalive:
    stop_event = threading.Event()

    def _loop() -> None:
        sudo = sudo_path()
        while not stop_event.wait(KEEPALIVE_INTERVAL_S):
            subprocess.run(  # noqa: S603
                [sudo, "-n", "true"],
                text=True,
                capture_output=True,
                check=False,
                cwd=str(ROOT),
            )

    thread = threading.Thread(target=_loop, name="sudo-keepalive", daemon=True)
    thread.start()
    return SudoKeepalive(stop_event=stop_event, thread=thread)


def load_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def collect_snapshot(run_dir: Path) -> Snapshot:
    lanes_dir = run_dir / "lanes"
    lane_phase_status: dict[str, list[dict[str, Any]]] = {}
    lane_summaries: dict[str, dict[str, Any]] = {}
    if lanes_dir.is_dir():
        for lane_dir in sorted(path for path in lanes_dir.iterdir() if path.is_dir()):
            phase_status = load_json(lane_dir / "phase_status.json")
            if isinstance(phase_status, list):
                lane_phase_status[lane_dir.name] = phase_status
            summary = load_json(lane_dir / "summary.json")
            if isinstance(summary, dict):
                lane_summaries[lane_dir.name] = summary

    crash_log = ""
    crash_path = run_dir / "crash.log"
    if crash_path.is_file():
        try:
            crash_log = crash_path.read_text(encoding="utf-8")
        except OSError:
            crash_log = ""

    summary = load_json(run_dir / "summary.json")
    ha_summary = load_json(run_dir / "ha_summary.json")
    global_phases = load_json(run_dir / "global_phases.json")
    return Snapshot(
        global_phases=global_phases if isinstance(global_phases, list) else [],
        lane_phase_status=lane_phase_status,
        lane_summaries=lane_summaries,
        ha_summary=ha_summary if isinstance(ha_summary, dict) else None,
        summary=summary if isinstance(summary, dict) else None,
        crash_log=crash_log,
    )


def _duration_text(duration_s: Any) -> str:
    try:
        return f"{float(duration_s):.1f}s"
    except (TypeError, ValueError):
        return "n/a"


def _run_duration(summary: dict[str, Any] | None) -> str:
    if not summary:
        return "n/a"
    started_at = summary.get("run_started_at")
    ended_at = summary.get("run_ended_at")
    if not started_at or not ended_at:
        return "n/a"
    try:
        started = datetime.fromisoformat(str(started_at))
        ended = datetime.fromisoformat(str(ended_at))
    except ValueError:
        return "n/a"
    return _duration_text((ended - started).total_seconds())


def _best_failure_snippet(payload: dict[str, Any] | None, detail: str = "") -> str:
    if isinstance(payload, dict):
        for key in ("stderr_tail", "stdout_tail"):
            value = payload.get(key)
            if isinstance(value, list):
                text = "\n".join(str(item).strip() for item in value if str(item).strip())
                if text:
                    return text.splitlines()[-1]
        for key in ("stderr", "stdout"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value.splitlines()[-1]
    return str(detail or "").strip()


def first_failure(snapshot: Snapshot) -> tuple[str, str] | None:
    summary = snapshot.summary
    if summary:
        for phase in summary.get("global_phases", []):
            if phase.get("status") == "failed":
                detail = _best_failure_snippet(
                    phase if isinstance(phase, dict) else None,
                    str(phase.get("detail") or "failed"),
                )
                return (f"global phase {phase.get('phase')}", detail)
    ha_summary = snapshot.ha_summary
    if ha_summary:
        for check in ha_summary.get("checks", []):
            if check.get("status") == "failed":
                payload = check.get("payload")
                detail = _best_failure_snippet(
                    payload if isinstance(payload, dict) else None,
                    str(check.get("detail") or "failed"),
                )
                return (f"HA check {check.get('name')}", detail)
    if summary:
        for lane in summary.get("lanes", []):
            for phase in lane.get("phase_status", []):
                if phase.get("status") == "failed":
                    detail = _best_failure_snippet(
                        phase if isinstance(phase, dict) else None,
                        str(phase.get("detail") or "failed"),
                    )
                    return (f"lane {lane.get('name')} phase {phase.get('phase')}", detail)
    if snapshot.crash_log.strip():
        return ("unexpected crash", snapshot.crash_log.strip().splitlines()[-1])
    return None


def _print(printer_lock: threading.Lock, message: str) -> None:
    with printer_lock:
        print(message, flush=True)


class Reporter:
    def __init__(self, console_mode: str) -> None:
        self.console_mode = console_mode
        self.seen = SeenStatus()
        self._print_lock = threading.Lock()

    def line(self, message: str) -> None:
        if self.console_mode == "quiet":
            return
        _print(self._print_lock, message)

    def print_stream(self, prefix: str, line: str) -> None:
        if self.console_mode != "verbose":
            return
        _print(self._print_lock, f"[{prefix}] {line.rstrip()}")

    def report_snapshot(self, snapshot: Snapshot) -> None:
        if self.console_mode == "quiet":
            return
        for phase in snapshot.global_phases:
            key = (
                str(phase.get("phase")),
                str(phase.get("status")),
                str(phase.get("ended_at")),
            )
            if key in self.seen.global_phases:
                continue
            self.seen.global_phases.add(key)
            self.line(
                "[lab-vm] global "
                f"{phase.get('phase')} {phase.get('status')} "
                f"({_duration_text(phase.get('duration_s'))}): "
                f"{phase.get('detail')}"
            )
        for lane_name, phases in sorted(snapshot.lane_phase_status.items()):
            for phase in phases:
                key = (
                    lane_name,
                    str(phase.get("phase")),
                    str(phase.get("status")),
                    str(phase.get("ended_at")),
                )
                if key in self.seen.lane_phases:
                    continue
                self.seen.lane_phases.add(key)
                self.line(
                    "[lab-vm] lane "
                    f"{lane_name} {phase.get('phase')} {phase.get('status')} "
                    f"({_duration_text(phase.get('duration_s'))}): "
                    f"{phase.get('detail')}"
                )
        ha_summary = snapshot.ha_summary
        if not ha_summary:
            return
        for check in ha_summary.get("checks", []):
            key = (
                str(check.get("name")),
                str(check.get("status")),
                str(check.get("ended_at")),
            )
            if key in self.seen.ha_checks:
                continue
            self.seen.ha_checks.add(key)
            optional = " optional" if bool(check.get("optional")) else ""
            self.line(
                "[lab-vm] ha "
                f"{check.get('name')} {check.get('status')}{optional} "
                f"({_duration_text(check.get('duration_s'))}): "
                f"{check.get('detail')}"
            )

    def print_final_summary(
        self,
        *,
        run_id: str,
        variant: Path,
        run_dir: Path,
        snapshot: Snapshot,
        teardown_result: TeardownResult,
        capture: StreamCapture,
        smoke_rc: int,
    ) -> None:
        lines = [
            "",
            f"[lab-vm] run_id={run_id}",
            f"[lab-vm] variant={variant}",
        ]
        summary = snapshot.summary
        if summary:
            lines.extend(
                [
                    f"[lab-vm] status={summary.get('status')}",
                    f"[lab-vm] duration={_run_duration(summary)}",
                ]
            )
        else:
            lines.append(f"[lab-vm] status=unknown (smoke_v2 rc={smoke_rc})")
        lines.append(f"[lab-vm] teardown={teardown_result.action}")
        lines.append(f"[lab-vm] artifacts={run_dir}")
        if (run_dir / "summary.json").is_file():
            lines.append(f"[lab-vm] summary_json={run_dir / 'summary.json'}")
        if (run_dir / "ha_summary.json").is_file():
            lines.append(f"[lab-vm] ha_summary_json={run_dir / 'ha_summary.json'}")
        if (run_dir / "crash.log").is_file():
            lines.append(f"[lab-vm] crash_log={run_dir / 'crash.log'}")

        if summary:
            lines.append("[lab-vm] global phases:")
            for phase in summary.get("global_phases", []):
                detail = (
                    _best_failure_snippet(phase if isinstance(phase, dict) else None, "")
                    if phase.get("status") == "failed"
                    else phase.get("detail")
                )
                lines.append(
                    "  - "
                    f"{phase.get('phase')}: {phase.get('status')} "
                    f"({_duration_text(phase.get('duration_s'))}) "
                    f"{detail}"
                )
            lines.append("[lab-vm] lanes:")
            for lane in summary.get("lanes", []):
                lines.append(f"  - {lane.get('name')}: {lane.get('status')}")

        ha_summary = snapshot.ha_summary
        if ha_summary:
            lines.append("[lab-vm] ha checks:")
            for check in ha_summary.get("checks", []):
                marker = "optional" if bool(check.get("optional")) else "required"
                lines.append(
                    "  - "
                    f"{check.get('name')}: {check.get('status')} ({marker}) "
                    f"{check.get('detail')}"
                )

        failure = first_failure(snapshot)
        if failure is not None:
            lines.append(f"[lab-vm] first_failure={failure[0]}: {failure[1]}")
        elif smoke_rc != 0:
            tail = "".join(capture.stderr_lines or capture.stdout_lines).strip().splitlines()
            if tail:
                lines.append(f"[lab-vm] first_failure=smoke_v2: {tail[-1]}")

        for line in lines:
            _print(self._print_lock, line)


def start_capture_thread(
    stream: Any, buffer: list[str], reporter: Reporter, prefix: str
) -> threading.Thread:
    def _reader() -> None:
        assert stream is not None
        for line in stream:
            buffer.append(line)
            reporter.print_stream(prefix, line)

    thread = threading.Thread(target=_reader, name=f"{prefix}-reader", daemon=True)
    thread.start()
    return thread


def build_smoke_command(variant: Path, run_id: str, passthrough: list[str]) -> list[str]:
    return [
        lab_python(),
        str(SMOKE_V2_SCRIPT),
        "--variant",
        str(variant),
        "--run-id",
        run_id,
        *passthrough,
    ]


def inventory_path_for(run_id: str) -> Path:
    return ROOT / "state" / "lab-vm" / run_id / "inventory.json"


def should_teardown(
    policy: str,
    *,
    interrupted: bool,
    summary_status: str | None,
    smoke_rc: int,
) -> bool:
    if policy == "never":
        return False
    if policy == "always":
        return True
    return not interrupted and smoke_rc == 0 and summary_status == "passed"


def run_teardown(
    *,
    variant: Path,
    run_id: str,
    policy: str,
    purge: bool,
    destroy_network: bool,
    interrupted: bool,
    summary_status: str | None,
    smoke_rc: int,
) -> TeardownResult:
    if not should_teardown(
        policy,
        interrupted=interrupted,
        summary_status=summary_status,
        smoke_rc=smoke_rc,
    ):
        return TeardownResult(action=f"skipped (policy={policy})")
    inventory = inventory_path_for(run_id)
    if not inventory.is_file():
        return TeardownResult(action="skipped (inventory missing)")
    cmd = [
        str(VARIANT_DOWN_SCRIPT),
        "--variant",
        str(variant),
        "--run-id",
        run_id,
    ]
    if purge:
        cmd.append("--purge")
    if destroy_network:
        cmd.append("--destroy-network")
    result = run_cmd(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "variant_down failed").strip().splitlines()
        suffix = detail[0] if detail else "variant_down failed"
        return TeardownResult(action=f"failed ({suffix})", returncode=result.returncode)
    details = []
    if purge:
        details.append("purged")
    if destroy_network:
        details.append("network-destroyed")
    suffix = f" ({', '.join(details)})" if details else ""
    return TeardownResult(action=f"completed{suffix}")


def wait_for_summary(run_dir: Path, timeout_s: float = 2.0) -> Snapshot:
    deadline = time.monotonic() + timeout_s
    latest = collect_snapshot(run_dir)
    while time.monotonic() < deadline:
        if latest.summary is not None or latest.crash_log:
            return latest
        time.sleep(0.1)
        latest = collect_snapshot(run_dir)
    return latest


def run_smoke(args: argparse.Namespace, passthrough: list[str]) -> int:
    variant = Path(args.variant).expanduser().resolve()
    output_root = output_root_for(passthrough)
    run_dir = output_root / args.run_id
    reporter = Reporter(args.console)
    reporter.line(
        f"[lab-vm] starting run_id={args.run_id} variant={variant} teardown={args.teardown}"
    )

    keepalive: SudoKeepalive | None = None
    if requires_sudo(passthrough, args.teardown):
        require_sudo()
        keepalive = start_sudo_keepalive()

    capture = StreamCapture()
    interrupted = False
    proc = subprocess.Popen(  # noqa: S603
        build_smoke_command(variant, args.run_id, passthrough),
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    stdout_thread = start_capture_thread(
        proc.stdout,
        capture.stdout_lines,
        reporter,
        "smoke:stdout",
    )
    stderr_thread = start_capture_thread(
        proc.stderr,
        capture.stderr_lines,
        reporter,
        "smoke:stderr",
    )

    def _forward_signal(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        if proc.poll() is None:
            proc.send_signal(signum)

    old_sigint = signal.signal(signal.SIGINT, _forward_signal)
    old_sigterm = signal.signal(signal.SIGTERM, _forward_signal)
    try:
        while proc.poll() is None:
            reporter.report_snapshot(collect_snapshot(run_dir))
            time.sleep(POLL_INTERVAL_S)
        smoke_rc = proc.wait()
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        snapshot = wait_for_summary(run_dir)
        reporter.report_snapshot(snapshot)
        teardown_result = run_teardown(
            variant=variant,
            run_id=args.run_id,
            policy=args.teardown,
            purge=bool(args.purge),
            destroy_network=bool(args.destroy_network),
            interrupted=interrupted,
            summary_status=str(snapshot.summary.get("status")) if snapshot.summary else None,
            smoke_rc=smoke_rc,
        )
        reporter.print_final_summary(
            run_id=args.run_id,
            variant=variant,
            run_dir=run_dir,
            snapshot=snapshot,
            teardown_result=teardown_result,
            capture=capture,
            smoke_rc=smoke_rc,
        )
        return smoke_rc if smoke_rc != 0 else teardown_result.returncode
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        if keepalive is not None:
            keepalive.stop()


def main(argv: list[str] | None = None) -> int:
    args, passthrough = parse_args(argv)
    return run_smoke(args, passthrough)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeHelperError as exc:
        print(f"[lab-vm] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
