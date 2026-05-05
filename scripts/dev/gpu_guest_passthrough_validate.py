#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_DIR = ROOT / "runs"
DEFAULT_GUEST_REPO = "/mnt/host"
DEFAULT_SSH_USER = "ae"
DEFAULT_EXPECTED_GPU = "TITAN RTX"
DEFAULT_MIN_VRAM_GIB = 24
DEFAULT_RUNTIME_HANDLER = "nvidia"
DEFAULT_COMPUTE_IMAGE = "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1"
DEFAULT_COMPUTE_SUCCESS_SIGNAL = "Test PASSED"
DEFAULT_EXECUTION_MODEL = "linux_guest_passthrough"


class ValidationError(RuntimeError):
    """Raised when validator configuration is incomplete or invalid."""


class CommandRunner(Protocol):
    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Run one command and return the completed process."""


@dataclass(frozen=True)
class ValidationConfig:
    run_id: str
    runs_dir: Path
    guest_ip: str | None
    vm_name: str | None
    inventory: Path | None
    ssh_user: str
    ssh_key: str | None
    guest_repo: str | None
    expected_gpu: str
    min_vram_gib: int
    expected_pci_bus_id: str | None
    runtime_handler: str
    compute_image: str
    compute_success_signal: str
    execution_model: str

    @property
    def run_root(self) -> Path:
        return self.runs_dir / self.run_id


class SubprocessRunner:
    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("egpu-%Y%m%dT%H%M%SZ")


def add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--guest-ip", default="", help="Guest IPv4/IPv6 address to validate.")
    parser.add_argument(
        "--vm-name",
        "--guest-name",
        dest="vm_name",
        default="",
        help="Guest VM name. Used with --inventory or the latest state/lab-vm inventory.",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=None,
        help="Optional QEMU inventory JSON written by variant up.",
    )
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    parser.add_argument(
        "--ssh-key",
        default=os.environ.get("SSH_KEY_PATH", str(Path.home() / ".ssh" / "id_rsa")),
        help="SSH private key path.",
    )
    parser.add_argument(
        "--guest-repo",
        default=None,
        help="Repo mount or checkout path inside the guest. Defaults to inventory metadata or /mnt/host.",
    )
    parser.add_argument("--expected-gpu", default=DEFAULT_EXPECTED_GPU)
    parser.add_argument("--min-vram-gib", type=int, default=DEFAULT_MIN_VRAM_GIB)
    parser.add_argument(
        "--expected-pci-bus-id",
        default="",
        help="Optional exact PCI bus id to require, for example 0000:65:00.0.",
    )
    parser.add_argument("--runtime-handler", default=DEFAULT_RUNTIME_HANDLER)
    parser.add_argument("--compute-image", default=DEFAULT_COMPUTE_IMAGE)
    parser.add_argument(
        "--execution-model",
        default=DEFAULT_EXECUTION_MODEL,
        help="Execution model recorded in the emitted plan/summary.",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gpu_guest_passthrough_validate.py",
        description=(
            "Validate that a Linux GPU guest with passthrough is guest-visible, "
            "CRI-ready, and able to run a containerized CUDA workload."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    plan = sub.add_parser("plan", help="Print planned artifacts and expected validation inputs.")
    plan.add_argument("--run-id", default=default_run_id())
    plan.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    plan.add_argument("--json", action="store_true", help="Emit JSON output.")
    add_target_args(plan)

    validate = sub.add_parser("validate", help="Run the passthrough validation and emit JSON.")
    validate.add_argument("--run-id", default=default_run_id())
    validate.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    add_target_args(validate)
    return parser.parse_args(argv)


def make_config(args: argparse.Namespace) -> ValidationConfig:
    ssh_key = str(args.ssh_key or "").strip() or None
    guest_repo = None
    if args.guest_repo is not None:
        guest_repo = str(args.guest_repo).strip() or None
    return ValidationConfig(
        run_id=str(args.run_id),
        runs_dir=Path(args.runs_dir),
        guest_ip=str(args.guest_ip or "").strip() or None,
        vm_name=str(args.vm_name or "").strip() or None,
        inventory=Path(args.inventory) if args.inventory else None,
        ssh_user=str(args.ssh_user or DEFAULT_SSH_USER).strip() or DEFAULT_SSH_USER,
        ssh_key=ssh_key,
        guest_repo=guest_repo,
        expected_gpu=str(args.expected_gpu or DEFAULT_EXPECTED_GPU).strip() or DEFAULT_EXPECTED_GPU,
        min_vram_gib=int(args.min_vram_gib),
        expected_pci_bus_id=str(args.expected_pci_bus_id or "").strip() or None,
        runtime_handler=str(args.runtime_handler or DEFAULT_RUNTIME_HANDLER).strip()
        or DEFAULT_RUNTIME_HANDLER,
        compute_image=str(args.compute_image or DEFAULT_COMPUTE_IMAGE).strip()
        or DEFAULT_COMPUTE_IMAGE,
        compute_success_signal=DEFAULT_COMPUTE_SUCCESS_SIGNAL,
        execution_model=str(args.execution_model or DEFAULT_EXECUTION_MODEL).strip()
        or DEFAULT_EXECUTION_MODEL,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _progress(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[egpu] {stamp} {message}", file=sys.stderr, flush=True)


def _expected_vram_mib(min_vram_gib: int) -> int:
    return int(min_vram_gib) * 1024


def _normalized_gpu_name(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def gpu_name_matches(*, detected: str, expected: str) -> bool:
    normalized_detected = _normalized_gpu_name(detected)
    normalized_expected = _normalized_gpu_name(expected)
    if not normalized_detected or not normalized_expected:
        return False
    return (
        normalized_detected == normalized_expected
        or normalized_expected in normalized_detected
        or normalized_detected in normalized_expected
    )


def parse_memory_total_mib(raw_value: str) -> int:
    text = str(raw_value or "").strip()
    digits = []
    for char in text:
        if char.isdigit():
            digits.append(char)
        elif digits:
            break
    if not digits:
        raise ValidationError(f"unable to parse GPU memory total: {raw_value!r}")
    return int("".join(digits))


def parse_nvidia_smi_output(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reader = csv.reader(line for line in str(text or "").splitlines() if line.strip())
    for idx, fields in enumerate(reader):
        if len(fields) != 3:
            raise ValidationError(
                f"nvidia-smi row {idx} expected 3 fields, got {len(fields)}: {fields!r}"
            )
        name, memory_total, pci_bus_id = (field.strip() for field in fields)
        rows.append(
            {
                "name": name,
                "memory_total_mib": parse_memory_total_mib(memory_total),
                "memory_total_raw": memory_total,
                "pci_bus_id": pci_bus_id,
            }
        )
    return rows


def parse_runtime_ready(output: str) -> bool | None:
    for line in str(output or "").splitlines():
        marker = "CRI condition RuntimeReady="
        if marker not in line:
            continue
        value = line.split(marker, 1)[1].strip().casefold()
        if value.startswith("true"):
            return True
        if value.startswith("false"):
            return False
    return None


def parse_available_runtime_handlers(output: str) -> list[str]:
    for line in str(output or "").splitlines():
        marker = "CRI available runtime handlers="
        if marker not in line:
            continue
        raw = line.split(marker, 1)[1].strip()
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def ssh_base_command(config: ValidationConfig, guest_ip: str) -> list[str]:
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
    ]
    if config.ssh_key:
        cmd.extend(["-i", config.ssh_key])
    cmd.append(f"{config.ssh_user}@{guest_ip}")
    return cmd


def run_guest_command(
    runner: CommandRunner,
    *,
    config: ValidationConfig,
    guest_ip: str,
    command: str,
) -> subprocess.CompletedProcess[str]:
    return runner.run([*ssh_base_command(config, guest_ip), command])


def _normalized_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _inventory_search_paths(config: ValidationConfig) -> list[Path]:
    if config.inventory is not None:
        return [config.inventory]
    if not config.vm_name:
        return []
    host_a_inventory = ROOT / "state" / "libvirt-host-a" / config.vm_name / "inventory.json"
    return [
        host_a_inventory,
        *sorted((ROOT / "state" / "lab-vm").glob("*/inventory.json"), reverse=True),
    ]


def _resolve_inventory_entry(
    config: ValidationConfig,
) -> tuple[Path | None, dict[str, Any] | None]:
    if not config.vm_name:
        return (config.inventory if config.inventory and config.inventory.is_file() else None, None)

    for inventory_path in _inventory_search_paths(config):
        if not inventory_path.is_file():
            continue
        try:
            payload = json.loads(inventory_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"invalid inventory JSON: {inventory_path}") from exc
        if not isinstance(payload, list):
            raise ValidationError(f"inventory must be a list: {inventory_path}")
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if _normalized_text(entry.get("name")) != config.vm_name:
                continue
            return inventory_path, entry
    return (config.inventory if config.inventory and config.inventory.is_file() else None, None)


def resolve_guest_target(config: ValidationConfig) -> dict[str, Any]:
    inventory_path, inventory_entry = _resolve_inventory_entry(config)
    guest_ip = config.guest_ip or _normalized_text((inventory_entry or {}).get("ip"))
    if not guest_ip:
        if not config.vm_name:
            raise ValidationError("either --guest-ip or --vm-name is required")
        raise ValidationError(
            f"unable to resolve guest IP for vm_name={config.vm_name!r}; "
            "pass --guest-ip or a matching --inventory path"
        )
    guest_repo = config.guest_repo or _normalized_text((inventory_entry or {}).get("guest_repo"))
    return {
        "guest_ip": guest_ip,
        "guest_repo": guest_repo or DEFAULT_GUEST_REPO,
        "inventory": inventory_path,
        "inventory_entry": inventory_entry,
    }


def artifacts_for(config: ValidationConfig) -> dict[str, str]:
    run_root = config.run_root
    return {
        "summary": str(run_root / "checks" / "egpu_passthrough_validate.json"),
        "attach": str(run_root / "checks" / "egpu_attach.json"),
        "cri_runtime": str(run_root / "checks" / "egpu_cri_runtime.json"),
        "compute_smoke": str(run_root / "checks" / "egpu_compute_smoke.json"),
    }


def build_plan(config: ValidationConfig) -> dict[str, Any]:
    artifacts = artifacts_for(config)
    return {
        "run_id": config.run_id,
        "run_root": str(config.run_root),
        "phase": "egpu_passthrough_validate",
        "execution_model": config.execution_model,
        "guest": {
            "guest_ip": config.guest_ip,
            "vm_name": config.vm_name,
            "inventory": str(config.inventory) if config.inventory else None,
            "ssh_user": config.ssh_user,
            "ssh_key": config.ssh_key,
            "guest_repo": config.guest_repo or DEFAULT_GUEST_REPO,
        },
        "expected": {
            "gpu_family": config.expected_gpu,
            "min_vram_gib": config.min_vram_gib,
            "runtime_handler": config.runtime_handler,
            "pci_bus_id": config.expected_pci_bus_id,
            "compute_image": config.compute_image,
            "compute_success_signal": config.compute_success_signal,
        },
        "artifacts": artifacts,
    }


def _join_output(stdout: str, stderr: str) -> str:
    content = stdout or ""
    if stderr:
        if content and not content.endswith("\n"):
            content += "\n"
        content += stderr
    return content


def _detail_from_output(output: str, *, fallback: str = "") -> str:
    text = str(output or "").strip()
    if text:
        return text.splitlines()[-1]
    return fallback


def build_attach_check(
    *,
    config: ValidationConfig,
    guest_ip: str,
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    command = "nvidia-smi --query-gpu=name,memory.total,pci.bus_id --format=csv,noheader"
    output = _join_output(result.stdout or "", result.stderr or "")
    parsed_rows: list[dict[str, Any]] = []
    parse_error = ""
    if result.returncode == 0:
        try:
            parsed_rows = parse_nvidia_smi_output(result.stdout or "")
        except ValidationError as exc:
            parse_error = str(exc)
    detected = parsed_rows[0] if len(parsed_rows) == 1 else None
    expected_min_vram_mib = _expected_vram_mib(config.min_vram_gib)
    assertions = {
        "single_gpu": len(parsed_rows) == 1,
        "model_match": bool(
            detected and gpu_name_matches(detected=detected["name"], expected=config.expected_gpu)
        ),
        "min_vram": bool(detected and int(detected["memory_total_mib"]) >= expected_min_vram_mib),
        "pci_bus_id_match": (
            config.expected_pci_bus_id is None
            or bool(
                detected
                and str(detected["pci_bus_id"]).strip().casefold()
                == config.expected_pci_bus_id.casefold()
            )
        ),
        "parse_ok": parse_error == "",
    }
    status = "passed"
    if result.returncode != 0 or not all(assertions.values()):
        status = "failed"
    return {
        "status": status,
        "command": command,
        "guest_ip": guest_ip,
        "returncode": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "expected": {
            "gpu_family": config.expected_gpu,
            "min_vram_gib": config.min_vram_gib,
            "min_vram_mib": expected_min_vram_mib,
            "pci_bus_id": config.expected_pci_bus_id,
        },
        "detected": {
            "gpu_count": len(parsed_rows),
            "gpus": parsed_rows,
        },
        "assertions": assertions,
        "detail": parse_error or _detail_from_output(output),
    }


def build_runtime_check(
    *,
    config: ValidationConfig,
    guest_ip: str,
    result: subprocess.CompletedProcess[str],
    script_path: str,
) -> dict[str, Any]:
    inner = (
        f"AE_CRI_RUNTIME_HANDLER={shlex.quote(config.runtime_handler)} "
        f"AE_CRI_REQUIRE_RUNTIME_READY=1 "
        f"{shlex.quote(script_path)}"
    )
    command = f"sudo bash -lc {shlex.quote(inner)}"
    output = _join_output(result.stdout or "", result.stderr or "")
    runtime_ready = parse_runtime_ready(output)
    available_handlers = parse_available_runtime_handlers(output)
    handler_available = (
        config.runtime_handler in available_handlers if available_handlers else result.returncode == 0
    )
    assertions = {
        "runtime_ready": runtime_ready is True,
        "runtime_handler_available": handler_available,
        "command_succeeded": result.returncode == 0,
    }
    status = "passed" if all(assertions.values()) else "failed"
    return {
        "status": status,
        "command": command,
        "guest_ip": guest_ip,
        "returncode": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "runtime_handler": config.runtime_handler,
        "runtime_ready": runtime_ready,
        "available_runtime_handlers": available_handlers,
        "assertions": assertions,
        "detail": _detail_from_output(output),
    }


def build_compute_smoke_check(
    *,
    config: ValidationConfig,
    guest_ip: str,
    result: subprocess.CompletedProcess[str],
    script_path: str,
) -> dict[str, Any]:
    inner = (
        f"AE_CRI_RUNTIME_HANDLER={shlex.quote(config.runtime_handler)} "
        f"AE_CRI_VECTORADD_IMAGE={shlex.quote(config.compute_image)} "
        f"{shlex.quote(script_path)}"
    )
    command = f"sudo bash -lc {shlex.quote(inner)}"
    output = _join_output(result.stdout or "", result.stderr or "")
    success_signal_present = config.compute_success_signal in (result.stdout or "")
    assertions = {
        "command_succeeded": result.returncode == 0,
        "success_signal_present": success_signal_present,
    }
    status = "passed" if all(assertions.values()) else "failed"
    return {
        "status": status,
        "command": command,
        "guest_ip": guest_ip,
        "returncode": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "image": config.compute_image,
        "runtime_handler": config.runtime_handler,
        "success_signal": config.compute_success_signal,
        "assertions": assertions,
        "detail": _detail_from_output(output),
    }


def run_validation(
    config: ValidationConfig,
    *,
    runner: CommandRunner | None = None,
    write_files: bool = True,
) -> dict[str, Any]:
    if config.min_vram_gib <= 0:
        raise ValidationError("--min-vram-gib must be > 0")
    runner = runner or SubprocessRunner()
    _progress(f"egpu: resolving guest target run_id={config.run_id}")
    guest_target = resolve_guest_target(config)
    guest_ip = str(guest_target["guest_ip"])
    guest_repo_path = str(guest_target["guest_repo"])
    resolved_inventory = guest_target["inventory"]
    artifacts = artifacts_for(config)
    guest_repo = PurePosixPath(guest_repo_path)
    preflight_path = str(guest_repo / "scripts" / "cri_preflight.sh")
    compute_smoke_path = str(guest_repo / "scripts" / "cri_cuda_vectoradd_smoke.sh")
    _progress(f"egpu: guest resolved guest_ip={guest_ip} guest_repo={guest_repo_path}")

    _progress("egpu: running attach check via nvidia-smi")
    attach_result = run_guest_command(
        runner,
        config=config,
        guest_ip=guest_ip,
        command="nvidia-smi --query-gpu=name,memory.total,pci.bus_id --format=csv,noheader",
    )
    attach_check = build_attach_check(config=config, guest_ip=guest_ip, result=attach_result)
    detected_gpu = next(iter((attach_check.get("detected") or {}).get("gpus") or []), None)
    _progress(
        "egpu: attach check "
        f"status={attach_check['status']} "
        f"gpu={((detected_gpu or {}).get('name') if isinstance(detected_gpu, dict) else None) or 'unknown'} "
        f"vram_mib={((detected_gpu or {}).get('memory_total_mib') if isinstance(detected_gpu, dict) else None) or 'unknown'} "
        f"pci={((detected_gpu or {}).get('pci_bus_id') if isinstance(detected_gpu, dict) else None) or 'unknown'}"
    )

    _progress("egpu: running CRI runtime preflight")
    runtime_result = run_guest_command(
        runner,
        config=config,
        guest_ip=guest_ip,
        command=(
            "sudo bash -lc "
            + shlex.quote(
                f"AE_CRI_RUNTIME_HANDLER={shlex.quote(config.runtime_handler)} "
                f"AE_CRI_REQUIRE_RUNTIME_READY=1 "
                f"{shlex.quote(preflight_path)}"
            )
        ),
    )
    runtime_check = build_runtime_check(
        config=config,
        guest_ip=guest_ip,
        result=runtime_result,
        script_path=preflight_path,
    )
    _progress(
        "egpu: runtime preflight "
        f"status={runtime_check['status']} "
        f"runtime_ready={runtime_check.get('runtime_ready')} "
        f"handler={config.runtime_handler} "
        f"available={','.join(runtime_check.get('available_runtime_handlers') or []) or 'unknown'}"
    )

    _progress("egpu: running CUDA compute smoke")
    compute_result = run_guest_command(
        runner,
        config=config,
        guest_ip=guest_ip,
        command=(
            "sudo bash -lc "
            + shlex.quote(
                f"AE_CRI_RUNTIME_HANDLER={shlex.quote(config.runtime_handler)} "
                f"AE_CRI_VECTORADD_IMAGE={shlex.quote(config.compute_image)} "
                f"{shlex.quote(compute_smoke_path)}"
            )
        ),
    )
    compute_check = build_compute_smoke_check(
        config=config,
        guest_ip=guest_ip,
        result=compute_result,
        script_path=compute_smoke_path,
    )
    _progress(
        "egpu: compute smoke "
        f"status={compute_check['status']} "
        f"image={config.compute_image} "
        f"handler={config.runtime_handler}"
    )

    summary = {
        "run_id": config.run_id,
        "run_root": str(config.run_root),
        "phase": "egpu_passthrough_validate",
        "execution_model": config.execution_model,
        "status": (
            "passed"
            if all(
                item["status"] == "passed"
                for item in (attach_check, runtime_check, compute_check)
            )
            else "failed"
        ),
        "guest": {
            "guest_ip": guest_ip,
            "vm_name": config.vm_name,
            "inventory": str(resolved_inventory) if resolved_inventory else None,
            "guest_repo": guest_repo_path,
        },
        "expected": {
            "gpu_family": config.expected_gpu,
            "min_vram_gib": config.min_vram_gib,
            "min_vram_mib": _expected_vram_mib(config.min_vram_gib),
            "pci_bus_id": config.expected_pci_bus_id,
            "runtime_handler": config.runtime_handler,
            "compute_image": config.compute_image,
            "compute_success_signal": config.compute_success_signal,
        },
        "detected": {
            "gpu": next(iter((attach_check.get("detected") or {}).get("gpus") or []), None),
            "runtime_ready": runtime_check.get("runtime_ready"),
            "available_runtime_handlers": runtime_check.get("available_runtime_handlers"),
        },
        "checks": {
            "egpu_attach": attach_check["status"],
            "egpu_cri_runtime": runtime_check["status"],
            "egpu_compute_smoke": compute_check["status"],
        },
        "artifacts": artifacts,
    }

    if write_files:
        _write_json(Path(artifacts["attach"]), attach_check)
        _write_json(Path(artifacts["cri_runtime"]), runtime_check)
        _write_json(Path(artifacts["compute_smoke"]), compute_check)
        _write_json(Path(artifacts["summary"]), summary)
    _progress(f"egpu: complete status={summary['status']}")
    return summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = make_config(args)
    if args.cmd == "plan":
        payload = build_plan(config)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"run_root:  {payload['run_root']}")
            print(f"summary:   {payload['artifacts']['summary']}")
            print(f"attach:    {payload['artifacts']['attach']}")
            print(f"runtime:   {payload['artifacts']['cri_runtime']}")
            print(f"compute:   {payload['artifacts']['compute_smoke']}")
        return 0

    try:
        summary = run_validation(config)
    except ValidationError as exc:
        print(str(exc), file=os.sys.stderr)
        return 2

    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
