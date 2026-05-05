#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import gpu_guest_passthrough_validate as egpu_validate

DEFAULT_RUNS_DIR = ROOT / "runs"
CRI_SEED_BUNDLE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_seed_bundle.sh"
CRI_SEED_MANIFEST = ROOT / "lab" / "variants" / "cri_seed_images.lock.json"
CRI_TORCH_CUDA_PROBE_SCRIPT = ROOT / "scripts" / "cri_torch_cuda_probe.sh"
CRI_VLLM_STARTUP_PROBE_SCRIPT = ROOT / "scripts" / "cri_vllm_startup_probe.sh"
CRI_SEED_VALIDATION_PROFILE = "all"
HOST_A_CELL_LANES = frozenset({"cell-a-single", "cell-ab-pp2-ray", "cell-ab-pp2-mp"})
DEFAULT_TEST_MODEL_ID = "HuggingFaceTB/SmolLM2-1.7B-Instruct"
FAST_TEST_MODEL_ID = "HuggingFaceTB/SmolLM2-360M-Instruct"
DEFAULT_TEST_VLLM_IMAGE = "docker.io/library/k1s-vllm-openai:host-a-cu121-v2"
DEFAULT_CRI_PROBE_TIMEOUT = "180s"
PROBE_DEBUG_EXCERPT_LIMIT = 4000
SEED_COPY_HEADROOM_BYTES = 1 * 1024 * 1024 * 1024
SEED_COPY_FREE_SPACE_MULTIPLIER = 2


@dataclass(frozen=True)
class CellLane:
    name: str
    manifest: Path


@dataclass(frozen=True)
class TestModelSpec:
    model_id: str
    revision: str | None
    local_path: str


@dataclass(frozen=True)
class SeedImageSpec:
    ref: str
    expected_image_id: str | None = None


CELL_LANES = (
    CellLane("cell-a-single", ROOT / "specs/examples/inference/cell-a-single.yaml"),
    CellLane("cell-b-single", ROOT / "specs/examples/inference/cell-b-single.yaml"),
    CellLane("cell-ab-pp2-ray", ROOT / "specs/examples/inference/cell-ab-pp2-ray.yaml"),
    CellLane("cell-ab-pp2-mp", ROOT / "specs/examples/inference/cell-ab-pp2-mp.yaml"),
)
CELL_LANE_BY_NAME = {lane.name: lane for lane in CELL_LANES}


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("f0n-%Y%m%dT%H%M%SZ")


def resolve_cell_lanes(selected: list[str] | None) -> list[CellLane]:
    if not selected:
        return list(CELL_LANES)
    lanes: list[CellLane] = []
    for name in selected:
        lane = CELL_LANE_BY_NAME.get(str(name))
        if lane is None:
            known = ", ".join(sorted(CELL_LANE_BY_NAME))
            raise SystemExit(f"unknown cell lane {name!r} (known: {known})")
        lanes.append(lane)
    return lanes


def build_plan(
    *, run_id: str, runs_dir: Path, cell_lane_names: list[str] | None = None
) -> dict[str, Any]:
    run_root = runs_dir / run_id
    selected_lanes = resolve_cell_lanes(cell_lane_names)
    egpu_config = egpu_validate.ValidationConfig(
        run_id=run_id,
        runs_dir=runs_dir,
        guest_ip=None,
        vm_name=None,
        inventory=None,
        ssh_user=egpu_validate.DEFAULT_SSH_USER,
        ssh_key=str(Path.home() / ".ssh" / "id_rsa"),
        guest_repo=egpu_validate.DEFAULT_GUEST_REPO,
        expected_gpu=egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id=None,
        runtime_handler=egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=egpu_validate.DEFAULT_COMPUTE_IMAGE,
        compute_success_signal=egpu_validate.DEFAULT_COMPUTE_SUCCESS_SIGNAL,
        execution_model=egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    egpu_plan = egpu_validate.build_plan(egpu_config)
    cells: list[dict[str, Any]] = []
    for lane in selected_lanes:
        cell_root = run_root / "cells" / lane.name
        cells.append(
            {
                "name": lane.name,
                "manifest": str(lane.manifest),
                "artifacts": {
                    "rendered_manifest": str(cell_root / "manifest-rendered.yaml"),
                    "apply": str(cell_root / "apply.txt"),
                    "status_initial": str(cell_root / "status-initial.json"),
                    "join_debug_initial": str(cell_root / "join-debug-initial.json"),
                    "events_initial": str(cell_root / "events-initial.txt"),
                    "api_probe_initial": str(cell_root / "api-probe-initial.json"),
                    "delete": str(cell_root / "delete.txt"),
                    "reapply": str(cell_root / "reapply.txt"),
                    "status_reapplied": str(cell_root / "status-reapplied.json"),
                    "join_debug_reapplied": str(cell_root / "join-debug-reapplied.json"),
                    "events_reapplied": str(cell_root / "events-reapplied.txt"),
                    "api_probe_reapplied": str(cell_root / "api-probe-reapplied.json"),
                    "teardown": str(cell_root / "teardown.txt"),
                },
            }
        )
    return {
        "run_id": run_id,
        "run_root": str(run_root),
        "artifacts": {
            "model_bootstrap": str(run_root / "ae" / "model-bootstrap.json"),
            "vllm_image_probe": str(run_root / "ae" / "vllm-image-probe.json"),
            "vllm_image_probe_transcript": str(run_root / "ae" / "vllm-image-probe.transcript.txt"),
            "vllm_startup_probe": str(run_root / "ae" / "vllm-startup-probe.json"),
            "vllm_startup_probe_transcript": str(
                run_root / "ae" / "vllm-startup-probe.transcript.txt"
            ),
        },
        "inventory": {
            "nodes": str(run_root / "ae" / "nodes.json"),
            "plan": str(run_root / "plan.json"),
            "summary": str(run_root / "summary.json"),
        },
        "execution_hosts": [
            {
                "host_id": "core-a",
                "node_id": "core-a--hub",
                "execution_model": "linux_guest_passthrough",
            },
            {
                "host_id": "edge-b",
                "node_id": "edge-b--gpu-1",
                "execution_model": "host_native",
            },
        ],
        "checks": {
            "egpu_passthrough_validate": egpu_plan["artifacts"]["summary"],
            "egpu_attach": egpu_plan["artifacts"]["attach"],
            "egpu_cri_runtime": egpu_plan["artifacts"]["cri_runtime"],
            "egpu_compute_smoke": egpu_plan["artifacts"]["compute_smoke"],
        },
        "phases": [
            {
                "name": "egpu_passthrough_validate",
                "execution_model": "linux_guest_passthrough",
                "artifacts": egpu_plan["artifacts"],
            },
            {
                "name": "cell_validation",
                "execution_model": "mixed_lane",
                "cell_count": len(selected_lanes),
            },
        ],
        "cells": cells,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="f0n_nvidia_validate.py",
        description="Collect operator-run F0n Nvidia lane evidence for the physical A/B baseline.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    plan = sub.add_parser("plan", help="Print the run plan and artifact layout.")
    plan.add_argument("--run-id", default=default_run_id())
    plan.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    plan.add_argument("--json", action="store_true", help="Emit JSON output.")
    plan.add_argument(
        "--cell-lane",
        action="append",
        choices=sorted(CELL_LANE_BY_NAME),
        help="Restrict the plan to one or more named cell lanes.",
    )

    collect = sub.add_parser("collect", help="Execute the canonical F0n validation flow.")
    collect.add_argument("--run-id", default=default_run_id())
    collect.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    collect.add_argument("--force", action="store_true", help="Overwrite an existing run directory.")
    collect.add_argument(
        "--ae-bin",
        default="",
        help="Optional ae binary override. Defaults to `python -m ae.cli` from this repo.",
    )
    collect.add_argument("--limit-events", type=int, default=20)
    collect.add_argument(
        "--skip-egpu-passthrough-validate",
        action="store_true",
        help="Skip the guest passthrough validator. This bypasses the canonical host-A proof step.",
    )
    collect.add_argument(
        "--cell-lane",
        action="append",
        choices=sorted(CELL_LANE_BY_NAME),
        help="Restrict collection to one or more named cell lanes.",
    )
    collect.add_argument(
        "--cell-ready-timeout",
        type=float,
        default=300.0,
        help="Seconds to keep reconciling a cell before declaring it not ready.",
    )
    collect.add_argument(
        "--cell-ready-poll-interval",
        type=float,
        default=5.0,
        help="Seconds between ready-state reconcile polls.",
    )
    collect.add_argument(
        "--controller-env",
        type=Path,
        default=None,
        help="Optional controller env file used to seed AE_STATE_* for `ae` CLI calls.",
    )
    collect.add_argument(
        "--test-model-id",
        default=DEFAULT_TEST_MODEL_ID,
        help=(
            "Public instruct model downloaded onto the Host A guest for this test workflow. "
            f"Fast fallback: {FAST_TEST_MODEL_ID}."
        ),
    )
    collect.add_argument(
        "--test-model-revision",
        default="",
        help="Optional Hugging Face revision for the test model.",
    )
    collect.add_argument(
        "--test-model-local-path",
        default="",
        help="Absolute guest path used for both download staging and MODEL_PATH mount.",
    )
    collect.add_argument(
        "--test-vllm-image",
        default=DEFAULT_TEST_VLLM_IMAGE,
        help=(
            "Pinned vLLM image used for Host A inference workload validation. "
            "Defaults to a CUDA 12.1-era image compatible with the current Host A driver lane. "
            "Local k1s-vllm-openai images must be prebuilt before collect runs."
        ),
    )
    egpu_validate.add_target_args(collect)
    return parser.parse_args()


def _ae_prefix(ae_bin: str) -> list[str]:
    override = str(ae_bin or "").strip()
    if override:
        return [override]
    return [sys.executable, "-m", "ae.cli"]


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = [value]
        values[key] = parsed[0] if len(parsed) == 1 else " ".join(parsed)
    return values


def _default_controller_env() -> Path | None:
    env_override = str(os.getenv("CONTROLLER_ENV_FILE") or "").strip()
    if env_override:
        candidate = Path(env_override).expanduser()
        if candidate.is_file():
            return candidate
    preferred = ROOT / "state" / "profiles" / "k1s-core" / "controller.env"
    if preferred.is_file():
        return preferred
    profile_root = ROOT / "state" / "profiles"
    if profile_root.is_dir():
        candidates = sorted(
            profile_root.glob("*/controller.env"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    fallback = ROOT / "state" / "env.sh"
    if fallback.is_file():
        return fallback
    return None


def _ae_env(controller_env: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    existing = str(env.get("PYTHONPATH") or "").strip()
    parts = [str(SRC)]
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env_file = controller_env or _default_controller_env()
    if env_file:
        payload = _read_env_file(env_file)
        for key in (
            "AE_STATE_BACKEND",
            "AE_STATE_DB",
            "AE_ETCD_ENDPOINTS",
            "AE_ETCD_PREFIX",
            "AE_SITE_ID",
        ):
            value = str(payload.get(key) or "").strip()
            if value and not str(env.get(key) or "").strip():
                env[key] = value
    if not str(env.get("AE_INFERENCE_EXPERIMENTAL") or "").strip():
        env["AE_INFERENCE_EXPERIMENTAL"] = "1"
    return env


def _run_command(*, cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _capture_text(proc: subprocess.CompletedProcess[str]) -> str:
    content = proc.stdout or ""
    if proc.stderr:
        if content and not content.endswith("\n"):
            content += "\n"
        content += proc.stderr
    return content


def _write_capture(
    path: Path,
    content: str,
    *,
    append: bool = False,
    label: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if append and path.exists():
        with path.open("a", encoding="utf-8") as handle:
            if label:
                handle.write(f"\n\n# {label}\n")
            handle.write(content)
        return
    if label:
        content = f"# {label}\n{content}"
    path.write_text(content, encoding="utf-8")


def _run_capture(*, cmd: list[str], path: Path, env: dict[str, str]) -> None:
    proc = _run_command(cmd=cmd, env=env)
    _write_capture(path, _capture_text(proc))
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)} -> {path}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _progress(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[f0n] {stamp} {message}", file=sys.stderr, flush=True)


def _cell_health_url(api_endpoint: str) -> str:
    raw = str(api_endpoint or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    path = (parts.path or "").rstrip("/")
    if path.endswith("/health"):
        health_path = path or "/health"
    elif not path or path == "/":
        health_path = "/health"
    else:
        health_path = f"{path}/health"
    return urlunsplit((parts.scheme or "http", parts.netloc, health_path, "", ""))


def _default_test_model_local_path(model_id: str) -> str:
    base = str(model_id or "").strip().rsplit("/", 1)[-1].casefold()
    slug = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-") or "model"
    return f"/models/{slug}"


def _selected_test_model(args: argparse.Namespace) -> TestModelSpec:
    model_id = str(getattr(args, "test_model_id", DEFAULT_TEST_MODEL_ID) or "").strip()
    revision = str(getattr(args, "test_model_revision", "") or "").strip() or None
    local_path = str(getattr(args, "test_model_local_path", "") or "").strip()
    if not local_path:
        local_path = _default_test_model_local_path(model_id)
    return TestModelSpec(model_id=model_id, revision=revision, local_path=local_path)


def _selected_test_vllm_image(args: argparse.Namespace) -> str:
    image = str(getattr(args, "test_vllm_image", DEFAULT_TEST_VLLM_IMAGE) or "").strip()
    if not image:
        return DEFAULT_TEST_VLLM_IMAGE
    return image


def _is_vllm_image_ref(ref: str) -> bool:
    image = _normalize_image_ref(ref)
    tail = image.rsplit("/", 1)[-1]
    return tail.startswith("vllm-openai:") or tail.startswith("k1s-vllm-openai:")


def _render_manifest_for_test_model(
    *,
    source_manifest: Path,
    rendered_manifest: Path,
    test_model: TestModelSpec,
    test_vllm_image: str,
) -> None:
    payload = yaml.safe_load(source_manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"unexpected manifest payload in {source_manifest}")
    spec = payload.setdefault("spec", {})
    if not isinstance(spec, dict):
        raise SystemExit(f"manifest spec must be a mapping in {source_manifest}")
    model = spec.setdefault("model", {})
    if not isinstance(model, dict):
        raise SystemExit(f"manifest model spec must be a mapping in {source_manifest}")
    model["modelId"] = test_model.model_id
    model["localPath"] = test_model.local_path
    if test_model.revision:
        model["revision"] = test_model.revision
    else:
        model.pop("revision", None)
    executor = spec.setdefault("executor", {})
    if not isinstance(executor, dict):
        raise SystemExit(f"manifest executor spec must be a mapping in {source_manifest}")
    executor["launcherImage"] = test_vllm_image
    executor["mpImage"] = test_vllm_image
    rendered_manifest.parent.mkdir(parents=True, exist_ok=True)
    rendered_manifest.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _prepare_rendered_manifests(
    *,
    plan: dict[str, Any],
    test_model: TestModelSpec,
    test_vllm_image: str,
) -> None:
    for cell in plan.get("cells") or []:
        source_manifest = Path(str(cell.get("manifest") or ""))
        rendered_manifest = Path(str((cell.get("artifacts") or {}).get("rendered_manifest") or ""))
        _render_manifest_for_test_model(
            source_manifest=source_manifest,
            rendered_manifest=rendered_manifest,
            test_model=test_model,
            test_vllm_image=test_vllm_image,
        )


def _host_a_rendered_manifest(plan: dict[str, Any]) -> tuple[str, Path] | None:
    for cell in plan.get("cells") or []:
        name = str(cell.get("name") or "").strip()
        if name not in HOST_A_CELL_LANES:
            continue
        artifacts = cell.get("artifacts") or {}
        rendered_manifest_raw = str(artifacts.get("rendered_manifest") or "").strip()
        if not rendered_manifest_raw:
            continue
        rendered_manifest = Path(rendered_manifest_raw).expanduser()
        return name, rendered_manifest
    return None


def _rendered_manifest_startup_probe_config(rendered_manifest: Path) -> dict[str, str | None]:
    payload = yaml.safe_load(rendered_manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"unexpected manifest payload in {rendered_manifest}")
    spec = payload.get("spec") or {}
    if not isinstance(spec, dict):
        raise SystemExit(f"manifest spec must be a mapping in {rendered_manifest}")
    model = spec.get("model") or {}
    if not isinstance(model, dict):
        raise SystemExit(f"manifest model spec must be a mapping in {rendered_manifest}")
    model_path = str(model.get("localPath") or "").strip()
    if not model_path:
        raise SystemExit(f"manifest model.localPath must be set in {rendered_manifest}")
    executor = spec.get("executor") or {}
    if not isinstance(executor, dict):
        raise SystemExit(f"manifest executor spec must be a mapping in {rendered_manifest}")
    dtype = str(executor.get("dtype") or "").strip() or None
    return {"model_path": model_path, "dtype": dtype}


def _read_status_payload(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"failed to parse cell status JSON at {path}: {exc}") from exc


def _wait_for_cell_ready(
    *,
    ae: list[str],
    manifest: str,
    name: str,
    apply_path: Path,
    status_path: Path,
    env: dict[str, str],
    timeout_s: float,
    poll_interval_s: float,
    join_debug_path: Path | None = None,
    join_debug_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    attempt = 0
    last_reported_phase = ""
    heartbeat_every = max(1, int(max(1.0, float(timeout_s)) // max(1.0, min(30.0, float(timeout_s)))))
    heartbeat_every = max(1, min(6, heartbeat_every))
    _progress(f"cell {name}: waiting for READY (timeout={int(timeout_s)}s manifest={manifest})")
    while True:
        attempt += 1
        apply_proc = _run_command(cmd=[*ae, "cell", "apply", "-f", manifest], env=env)
        _write_capture(
            apply_path,
            _capture_text(apply_proc),
            append=attempt > 1,
            label=f"attempt {attempt}",
        )
        status_proc = _run_command(cmd=[*ae, "cell", "status", name, "--json"], env=env)
        _write_capture(status_path, _capture_text(status_proc))
        if status_proc.returncode != 0:
            status_cmd = [*ae, "cell", "status", name, "--json"]
            raise SystemExit(
                "cell status failed after apply attempt "
                f"{attempt}: {' '.join(status_cmd)} -> {status_path}"
            )
        payload = _read_status_payload(status_path)
        phase = str(payload.get("phase") or "").strip().upper()
        if phase != last_reported_phase or attempt == 1 or attempt % heartbeat_every == 0:
            allocations = payload.get("allocations") or {}
            api_endpoint = str(allocations.get("api_endpoint") or "").strip()
            last_error = str(payload.get("last_error") or "").strip()
            remaining_s = max(0, int(deadline - time.monotonic()))
            details = [f"attempt={attempt}", f"phase={phase or 'UNKNOWN'}", f"remaining={remaining_s}s"]
            if api_endpoint:
                details.append(f"api={api_endpoint}")
            if last_error:
                details.append(f"last_error={last_error}")
            _progress(f"cell {name}: " + " ".join(details))
            last_reported_phase = phase
        if phase == "READY":
            _progress(f"cell {name}: READY after attempt={attempt}")
            return payload
        if phase == "FAILED":
            if join_debug_path is not None:
                try:
                    _write_join_debug_artifact(
                        ae=ae,
                        env=env,
                        cell_name=name,
                        status_payload=payload,
                        path=join_debug_path,
                        join_debug_context=join_debug_context,
                    )
                except Exception as exc:  # noqa: BLE001
                    _write_json(
                        join_debug_path,
                        {
                            "status": "failed",
                            "cell_name": name,
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "detail": f"failed to collect join debug: {exc}",
                            "status_payload": payload,
                        },
                    )
            raise SystemExit(f"cell {name} entered FAILED phase -> {status_path}")
        if time.monotonic() >= deadline:
            if join_debug_path is not None:
                try:
                    _write_join_debug_artifact(
                        ae=ae,
                        env=env,
                        cell_name=name,
                        status_payload=payload,
                        path=join_debug_path,
                        join_debug_context=join_debug_context,
                    )
                except Exception as exc:  # noqa: BLE001
                    _write_json(
                        join_debug_path,
                        {
                            "status": "failed",
                            "cell_name": name,
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "detail": f"failed to collect join debug: {exc}",
                            "status_payload": payload,
                        },
                    )
            raise SystemExit(
                f"cell {name} did not reach READY within {int(timeout_s)}s -> {status_path}"
            )
        time.sleep(max(0.1, float(poll_interval_s)))


def _best_effort_delete_cell(*, ae: list[str], name: str, env: dict[str, str]) -> None:
    # Reruns can inherit a stale FAILED cell object from a prior controller instance.
    _run_command(cmd=[*ae, "cell", "delete", name], env=env)


def _probe_cell_api(*, status_payload: dict[str, Any], path: Path) -> None:
    allocations = status_payload.get("allocations") or {}
    api_endpoint = str(allocations.get("api_endpoint") or "").strip()
    health_url = _cell_health_url(api_endpoint)
    if not health_url:
        raise SystemExit(f"cell status missing allocations.api_endpoint -> {path}")
    payload: dict[str, Any] = {
        "api_endpoint": api_endpoint,
        "health_url": health_url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.get(health_url, timeout=5)
        payload["status_code"] = int(resp.status_code)
        payload["ok"] = 200 <= int(resp.status_code) < 300
        text = (resp.text or "").strip()
        if text:
            payload["body_excerpt"] = text[:400]
    except requests.RequestException as exc:
        payload["ok"] = False
        payload["error"] = f"{exc.__class__.__name__}: {exc}"
        _write_json(path, payload)
        raise SystemExit(f"cell api probe failed: {health_url} -> {path}") from exc
    _write_json(path, payload)
    if not bool(payload.get("ok")):
        raise SystemExit(
            f"cell api probe returned HTTP {payload.get('status_code')} for {health_url} -> {path}"
        )


def _cell_lanes_require_host_a_seed(plan: dict[str, Any]) -> bool:
    return any(str(cell.get("name") or "") in HOST_A_CELL_LANES for cell in plan.get("cells") or [])


def _ordered_unique(images: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for image in images:
        ref = str(image or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        ordered.append(ref)
    return ordered


def _normalize_image_ref(image: str) -> str:
    raw = str(image or "").strip()
    if not raw:
        return raw
    if "/" not in raw:
        return f"docker.io/library/{raw}"
    host = raw.split("/", 1)[0]
    if "." in host or ":" in host:
        return raw
    if raw.count("/") == 1:
        return f"docker.io/{raw}"
    return raw


def _normalize_image_id(image_id: str | None) -> str | None:
    value = str(image_id or "").strip()
    if not value:
        return None
    lowered = value.casefold()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", lowered):
        return lowered
    if re.fullmatch(r"[0-9a-f]{64}", lowered):
        return f"sha256:{lowered}"
    return None


def _image_id_matches_expected(*, guest_image_id: str | None, expected_image_id: str | None) -> bool:
    guest = _normalize_image_id(guest_image_id)
    expected = _normalize_image_id(expected_image_id)
    return bool(guest and expected and guest == expected)


def _seed_manifest_payload(manifest: Path = CRI_SEED_MANIFEST) -> dict[str, Any]:
    return json.loads(manifest.read_text(encoding="utf-8"))


def _seed_image_spec_from_entry(entry: Any, *, manifest: Path) -> SeedImageSpec:
    if isinstance(entry, str):
        ref = _normalize_image_ref(entry)
        if not ref:
            raise SystemExit(f"invalid empty image ref entry in {manifest}")
        return SeedImageSpec(ref=ref)
    if isinstance(entry, dict):
        ref = _normalize_image_ref(str(entry.get("ref") or ""))
        if not ref:
            raise SystemExit(f"invalid seed image entry without ref in {manifest}: {entry!r}")
        return SeedImageSpec(
            ref=ref,
            expected_image_id=_normalize_image_id(entry.get("expected_image_id")),
        )
    raise SystemExit(f"invalid seed image entry in {manifest}: {entry!r}")


def _ordered_unique_seed_images(images: list[SeedImageSpec]) -> list[SeedImageSpec]:
    merged: dict[str, SeedImageSpec] = {}
    ordered_refs: list[str] = []
    for item in images:
        ref = _normalize_image_ref(item.ref)
        if not ref:
            continue
        current = SeedImageSpec(ref=ref, expected_image_id=_normalize_image_id(item.expected_image_id))
        existing = merged.get(ref)
        if existing is None:
            merged[ref] = current
            ordered_refs.append(ref)
            continue
        if existing.expected_image_id is None and current.expected_image_id is not None:
            merged[ref] = current
    return [merged[ref] for ref in ordered_refs]


def _seed_image_entry_payload(image: SeedImageSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {"ref": image.ref}
    if image.expected_image_id:
        payload["expected_image_id"] = image.expected_image_id
    return payload


def _seed_manifest_image_groups(manifest: Path = CRI_SEED_MANIFEST) -> dict[str, list[SeedImageSpec]]:
    payload = _seed_manifest_payload(manifest)
    images = payload.get("images") or {}
    if not isinstance(images, dict):
        raise SystemExit(f"invalid CRI seed manifest images payload in {manifest}")
    return {
        "bootstrap": _ordered_unique_seed_images(
            [_seed_image_spec_from_entry(image, manifest=manifest) for image in list(images.get("bootstrap") or [])]
        ),
        "core": _ordered_unique_seed_images(
            [_seed_image_spec_from_entry(image, manifest=manifest) for image in list(images.get("core") or [])]
        ),
        "edge": _ordered_unique_seed_images(
            [_seed_image_spec_from_entry(image, manifest=manifest) for image in list(images.get("edge") or [])]
        ),
    }


def _seed_manifest_for_images(
    *,
    run_root: Path,
    filename: str,
    image_groups: dict[str, list[SeedImageSpec]],
) -> Path:
    payload = _seed_manifest_payload()
    payload["schema_version"] = max(2, int(payload.get("schema_version") or 0))
    payload["images"] = {
        "bootstrap": [
            _seed_image_entry_payload(item)
            for item in _ordered_unique_seed_images(list(image_groups.get("bootstrap") or []))
        ],
        "core": [
            _seed_image_entry_payload(item)
            for item in _ordered_unique_seed_images(list(image_groups.get("core") or []))
        ],
        "edge": [
            _seed_image_entry_payload(item)
            for item in _ordered_unique_seed_images(list(image_groups.get("edge") or []))
        ],
    }
    manifest_path = run_root / "ae" / filename
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return manifest_path


def _rewrite_seed_manifest_images(
    *,
    manifest_path: Path,
    image_groups: dict[str, list[SeedImageSpec]],
) -> None:
    payload = _seed_manifest_payload(manifest_path)
    payload["schema_version"] = max(2, int(payload.get("schema_version") or 0))
    payload["images"] = {
        "bootstrap": [
            _seed_image_entry_payload(item)
            for item in _ordered_unique_seed_images(list(image_groups.get("bootstrap") or []))
        ],
        "core": [
            _seed_image_entry_payload(item)
            for item in _ordered_unique_seed_images(list(image_groups.get("core") or []))
        ],
        "edge": [
            _seed_image_entry_payload(item)
            for item in _ordered_unique_seed_images(list(image_groups.get("edge") or []))
        ],
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _rendered_manifest_workload_images(plan: dict[str, Any]) -> list[str]:
    images: list[str] = []
    for cell in plan.get("cells") or []:
        name = str(cell.get("name") or "")
        if name not in HOST_A_CELL_LANES:
            continue
        rendered_manifest = Path(str((cell.get("artifacts") or {}).get("rendered_manifest") or ""))
        if not rendered_manifest.is_file():
            raise SystemExit(f"rendered manifest missing before validation seed restore: {rendered_manifest}")
        payload = yaml.safe_load(rendered_manifest.read_text(encoding="utf-8")) or {}
        spec = payload.get("spec") or {}
        executor = spec.get("executor") or {}
        for key in ("rayImage", "launcherImage", "mpImage"):
            image = str(executor.get(key) or "").strip()
            if image:
                images.append(_normalize_image_ref(image))
    return _ordered_unique(images)


def _validation_seed_image_groups(
    *, plan: dict[str, Any], args: argparse.Namespace
) -> dict[str, list[SeedImageSpec]]:
    base_groups = _seed_manifest_image_groups()
    egpu_config = egpu_validate.make_config(args)
    workload_images = _rendered_manifest_workload_images(plan)
    core_images = _ordered_unique_seed_images(
        [
            SeedImageSpec(ref=_normalize_image_ref(egpu_config.compute_image)),
            *[SeedImageSpec(ref=image) for image in workload_images],
        ]
    )
    return {
        "bootstrap": list(base_groups["bootstrap"]),
        "core": core_images,
        "edge": [],
    }


def _required_seed_images(image_groups: dict[str, list[SeedImageSpec]]) -> list[SeedImageSpec]:
    return _ordered_unique_seed_images(
        [
            *list(image_groups.get("bootstrap") or []),
            *list(image_groups.get("core") or []),
            *list(image_groups.get("edge") or []),
        ]
    )


def _required_seed_image_refs(image_groups: dict[str, list[SeedImageSpec]]) -> list[str]:
    return [image.ref for image in _required_seed_images(image_groups)]


def _filter_seed_image_groups(
    *,
    image_groups: dict[str, list[SeedImageSpec]],
    keep_refs: set[str],
) -> dict[str, list[SeedImageSpec]]:
    return {
        section: [image for image in refs if image.ref in keep_refs]
        for section, refs in image_groups.items()
    }


def _required_guest_seed_space_bytes(bundle_size_bytes: int) -> int:
    size = max(0, int(bundle_size_bytes))
    return size * SEED_COPY_FREE_SPACE_MULTIPLIER + SEED_COPY_HEADROOM_BYTES


def _cleanup_guest_seed_staging(
    *,
    config: egpu_validate.ValidationConfig,
    guest_ip: str,
    before_refs: set[str],
    required_images: list[str],
    stale_refs: list[str] | None = None,
    min_free_bytes: int | None = None,
) -> dict[str, Any]:
    obsolete_vllm = sorted(
        ref
        for ref in before_refs
        if _is_vllm_image_ref(ref) and ref not in required_images
    )
    targeted_removals = _ordered_unique([*obsolete_vllm, *list(stale_refs or [])])
    cleanup_parts = [
        "rm -f /tmp/*-cri-seed-images.oci.tar",
        'avail_before=$(df -B1 --output=avail /tmp 2>/dev/null | tail -n1 | tr -dc "0-9" || echo 0)',
        "purged_namespace=0",
    ]
    for image in targeted_removals:
        cleanup_parts.append(
            f"ids=$(crictl ps -a --image {shlex.quote(image)} -q 2>/dev/null || true); "
            'if [[ -n "$ids" ]]; then crictl rm -f $ids >/dev/null 2>&1 || true; fi'
        )
        cleanup_parts.append(
            f"ctr -n k8s.io images rm --sync {shlex.quote(image)} >/dev/null 2>&1 || true"
        )
    if min_free_bytes is not None:
        cleanup_parts.append(
            f'if [[ "${{avail_before:-0}}" -lt {int(min_free_bytes)} ]]; then '
            "ids=$(crictl ps -a -q 2>/dev/null || true); "
            'if [[ -n "$ids" ]]; then crictl rm -f $ids >/dev/null 2>&1 || true; fi; '
            "imgs=$(ctr -n k8s.io images ls -q 2>/dev/null || true); "
            'if [[ -n "$imgs" ]]; then ctr -n k8s.io images rm --sync $imgs >/dev/null 2>&1 || true; fi; '
            "purged_namespace=1; "
            "fi"
        )
    cleanup_parts.extend(
        [
            'avail_after=$(df -B1 --output=avail /tmp 2>/dev/null | tail -n1 | tr -dc "0-9" || echo 0)',
            'printf "__seed_cleanup__ avail_before=%s avail_after=%s purged_namespace=%s\\n" "${avail_before:-0}" "${avail_after:-0}" "${purged_namespace:-0}"',
        ]
    )
    cleanup_parts.append("df -h / /tmp /var/lib/containerd || true")
    proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote("set -euo pipefail; " + "; ".join(cleanup_parts)),
    )
    detail = (proc.stdout or proc.stderr or "").strip()
    avail_before_bytes: int | None = None
    avail_after_bytes: int | None = None
    purged_namespace = False
    match = re.search(
        r"__seed_cleanup__ avail_before=(\d+) avail_after=(\d+) purged_namespace=(\d+)",
        detail,
    )
    if match:
        avail_before_bytes = int(match.group(1))
        avail_after_bytes = int(match.group(2))
        purged_namespace = match.group(3) == "1"
    status = "ok" if proc.returncode == 0 else "failed"
    if (
        status == "ok"
        and min_free_bytes is not None
        and avail_after_bytes is not None
        and avail_after_bytes < int(min_free_bytes)
    ):
        status = "failed"
    return {
        "removed_images": targeted_removals,
        "detail": detail,
        "status": status,
        "avail_before_bytes": avail_before_bytes,
        "avail_after_bytes": avail_after_bytes,
        "purged_namespace": purged_namespace,
        "required_free_bytes": int(min_free_bytes) if min_free_bytes is not None else None,
    }


def _seed_bundle_paths(run_id: str, *, label: str = "core-seed") -> tuple[str, Path]:
    seed_run_id = f"{run_id}-{label}"
    bundle = ROOT / "state" / "lab-vm" / seed_run_id / "seeds" / "cri-seed-images.oci.tar"
    return seed_run_id, bundle


def _seed_bundle_info_path(bundle: Path) -> Path:
    return bundle.parent / "cri-seed-info.json"


def _seed_bundle_image_specs(bundle: Path) -> list[SeedImageSpec]:
    payload = json.loads(_seed_bundle_info_path(bundle).read_text(encoding="utf-8"))
    entries = payload.get("images") or []
    if not isinstance(entries, list):
        raise SystemExit(f"invalid CRI seed metadata images payload in {_seed_bundle_info_path(bundle)}")
    return _ordered_unique_seed_images(
        [_seed_image_spec_from_entry(entry, manifest=_seed_bundle_info_path(bundle)) for entry in entries]
    )


def _guest_image_states(
    *,
    config: egpu_validate.ValidationConfig,
    guest_ip: str,
    image_refs: list[str],
) -> dict[str, dict[str, Any]]:
    refs = _ordered_unique([_normalize_image_ref(ref) for ref in image_refs if str(ref or "").strip()])
    if not refs:
        return {}
    python_cmd = r"""
import json
import os
import subprocess

refs = json.loads(os.environ["IMAGE_REFS_JSON"])
payload = {}
for ref in refs:
    proc = subprocess.run(
        ["crictl", "inspecti", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        payload[ref] = {
            "present": False,
            "image_id": None,
            "detail": (proc.stderr or proc.stdout or "").strip(),
        }
        continue
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        payload[ref] = {
            "present": False,
            "image_id": None,
            "detail": f"invalid inspecti payload: {exc}",
        }
        continue
    status = data.get("status") or {}
    payload[ref] = {
        "present": True,
        "image_id": status.get("id") or None,
    }
print(json.dumps(payload))
""".strip()
    remote_cmd = (
        f"IMAGE_REFS_JSON={shlex.quote(json.dumps(refs))} "
        f"python3 -c {shlex.quote(python_cmd)}"
    )
    runner = egpu_validate.SubprocessRunner()
    proc = egpu_validate.run_guest_command(
        runner,
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote(remote_cmd),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"failed to inspect guest CRI images for {guest_ip}: {detail}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        detail = (proc.stdout or proc.stderr or "").strip()
        raise SystemExit(f"failed to parse guest CRI image state payload for {guest_ip}: {exc}: {detail}")
    if not isinstance(data, dict):
        raise SystemExit(f"invalid guest CRI image state payload for {guest_ip}: {data!r}")
    states: dict[str, dict[str, Any]] = {}
    for ref in refs:
        state = data.get(ref) or {}
        if not isinstance(state, dict):
            state = {}
        states[ref] = {
            "present": bool(state.get("present")),
            "image_id": _normalize_image_id(state.get("image_id")),
            "detail": str(state.get("detail") or "").strip(),
        }
    return states


def _classify_guest_seed_images(
    required_images: list[SeedImageSpec],
    guest_states: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    missing: list[str] = []
    stale: list[str] = []
    fresh: list[str] = []
    for image in required_images:
        state = guest_states.get(image.ref) or {}
        present = bool(state.get("present"))
        guest_image_id = _normalize_image_id(state.get("image_id"))
        expected_image_id = _normalize_image_id(image.expected_image_id)
        if not present:
            missing.append(image.ref)
        elif expected_image_id and guest_image_id and guest_image_id != expected_image_id:
            stale.append(image.ref)
        else:
            fresh.append(image.ref)
    return missing, stale, fresh


def _uses_lab_vm_hostshare(guest_target: dict[str, Any]) -> bool:
    guest_repo = str(guest_target.get("guest_repo") or "").strip()
    if guest_repo and guest_repo != egpu_validate.DEFAULT_GUEST_REPO:
        return False
    inventory = guest_target.get("inventory")
    if inventory is None:
        return guest_repo == egpu_validate.DEFAULT_GUEST_REPO
    inventory_path = Path(inventory).resolve()
    try:
        inventory_path.relative_to((ROOT / "state" / "libvirt-host-a").resolve())
        return False
    except ValueError:
        return True


def _scp_base_command(config: egpu_validate.ValidationConfig) -> list[str]:
    cmd = [
        "scp",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
    ]
    if config.ssh_key:
        cmd.extend(["-i", config.ssh_key])
    return cmd


def _parse_probe_payload(output: str) -> dict[str, Any] | None:
    for raw in reversed(str(output or "").splitlines()):
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _probe_python_result(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    nested = payload.get("result")
    if isinstance(nested, dict):
        return nested
    python_signal_keys = {
        "torch_version",
        "cuda_version",
        "cuda_device_count",
        "cuda_device_count_error",
        "cuda_is_available",
        "cuda_is_available_error",
        "cuda_tensor_ok",
        "cuda_tensor_device",
        "cuda_error",
    }
    if any(key in payload for key in python_signal_keys):
        result_keys = python_signal_keys | {"status", "error"}
        return {key: value for key, value in payload.items() if key in result_keys}
    return None


def _truncate_probe_text(value: Any, *, limit: int = PROBE_DEBUG_EXCERPT_LIMIT) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head - len("\n...\n"))
    return f"{text[:head]}\n...\n{text[-tail:]}"


def _probe_debug_excerpt(payload: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("debug")
    if not isinstance(raw, dict):
        return {}
    excerpt: dict[str, str] = {}
    for key in (
        "image_inspect",
        "pod_inspect",
        "container_inspect",
        "container_logs",
        "crictl_images",
        "ctr_images",
        "containerd_journal",
    ):
        text = _truncate_probe_text(raw.get(key))
        if text:
            excerpt[key] = text
    return excerpt


def _app_name_parts(app_name: str) -> tuple[str | None, str]:
    raw = str(app_name or "").strip()
    if "/" not in raw:
        return None, raw
    namespace, name = raw.split("/", 1)
    return (namespace or None), name


def _command_capture_payload(
    *,
    cmd: list[str],
    env: dict[str, str],
    truncate: int = 12000,
) -> dict[str, Any]:
    proc = _run_command(cmd=cmd, env=env)
    detail = _truncate_probe_text(_capture_text(proc), limit=truncate)
    return {
        "command": " ".join(cmd),
        "returncode": int(proc.returncode),
        "detail": detail,
    }


def _guest_command_capture_payload(
    *,
    config: egpu_validate.ValidationConfig,
    guest_ip: str,
    command: str,
    truncate: int = 12000,
) -> dict[str, Any]:
    proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command=command,
    )
    detail = _truncate_probe_text(_capture_text(proc), limit=truncate)
    return {
        "command": command,
        "returncode": int(proc.returncode),
        "detail": detail,
    }


def _capture_needs_cri_fallback(capture: dict[str, Any]) -> bool:
    detail = str((capture or {}).get("detail") or "")
    return "No status recorded for " in detail


def _workload_pod_name(workload: dict[str, Any]) -> str:
    states = workload.get("pod_states") or []
    for item in states:
        if not isinstance(item, dict):
            continue
        pod_name = str(item.get("pod_name") or "").strip()
        if pod_name:
            return pod_name
    return ""


def _guest_cri_log_capture_payload(
    *,
    config: egpu_validate.ValidationConfig,
    guest_ip: str,
    workload: dict[str, Any],
    tail: int = 200,
    truncate: int = 12000,
) -> dict[str, Any]:
    _namespace, short_name = _app_name_parts(str(workload.get("app_name") or ""))
    pod_name = _workload_pod_name(workload)
    python_cmd = r"""
import json
import os
import subprocess
import sys

POD_NAME = str(os.environ.get("POD_NAME") or "").strip()
APP_NAME = str(os.environ.get("APP_NAME") or "").strip()
TAIL = int(os.environ.get("TAIL") or "200")

def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)

def _text(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stdout or "").strip()
    if proc.stderr:
        text = f"{text}\n{proc.stderr.strip()}".strip()
    return text

result = {
    "pod_name": POD_NAME,
    "app_name": APP_NAME,
    "command": "",
    "returncode": 1,
    "detail": "",
}

ps_proc = _run(["sudo", "crictl", "ps", "-a", "-o", "json"])
if ps_proc.returncode != 0:
    result["detail"] = _text(ps_proc) or "crictl ps failed"
    print(json.dumps(result))
    raise SystemExit(0)

try:
    payload = json.loads(ps_proc.stdout or "{}")
except json.JSONDecodeError:
    payload = {}

target = None
matched_by = ""
for container in payload.get("containers", []):
    labels = container.get("labels") or {}
    if POD_NAME and labels.get("ae.pod_name") == POD_NAME:
        target = container
        matched_by = "ae.pod_name"
        break
    if APP_NAME and labels.get("ae.app") == APP_NAME and target is None:
        target = container
        matched_by = "ae.app"

if not isinstance(target, dict):
    result["detail"] = "unable to find matching CRI container"
    print(json.dumps(result))
    raise SystemExit(0)

container_id = str(target.get("id") or "").strip()
result["container_id"] = container_id
result["matched_by"] = matched_by
if not container_id:
    result["detail"] = "matched container missing id"
    print(json.dumps(result))
    raise SystemExit(0)

logs_cmd = ["sudo", "crictl", "logs", "--tail", str(TAIL), container_id]
result["command"] = " ".join(logs_cmd)
logs_proc = _run(logs_cmd)
result["returncode"] = int(logs_proc.returncode)
result["detail"] = _text(logs_proc)
print(json.dumps(result))
""".strip()
    remote_cmd = (
        f"POD_NAME={shlex.quote(pod_name)} "
        f"APP_NAME={shlex.quote(short_name)} "
        f"TAIL={int(tail)} "
        f"python3 -c {shlex.quote(python_cmd)}"
    )
    capture = _guest_command_capture_payload(
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote(remote_cmd),
        truncate=truncate,
    )
    payload = _parse_probe_payload(str(capture.get("detail") or ""))
    if isinstance(payload, dict):
        if payload.get("detail"):
            payload["detail"] = _truncate_probe_text(payload.get("detail"), limit=truncate)
        return payload
    return capture


def _workload_lookup_tables(
    workloads: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_pod_name: dict[str, dict[str, Any]] = {}
    by_short_name: dict[str, dict[str, Any]] = {}
    for workload in workloads:
        pod_name = _workload_pod_name(workload)
        if pod_name:
            by_pod_name[pod_name] = workload
        _namespace, short_name = _app_name_parts(str(workload.get("app_name") or ""))
        if short_name:
            by_short_name[short_name] = workload
    return by_pod_name, by_short_name


def _node_containers_payload(
    *,
    guest_ip: str,
    workloads: list[dict[str, Any]],
) -> dict[str, Any]:
    url = f"http://{guest_ip}:9111/v1/containers"
    payload: dict[str, Any] = {"url": url, "ok": False}
    try:
        resp = requests.get(url, timeout=5)
        payload["status_code"] = int(resp.status_code)
        payload["ok"] = 200 <= int(resp.status_code) < 300
        raw = resp.json()
    except requests.RequestException as exc:
        payload["error"] = f"{exc.__class__.__name__}: {exc}"
        return payload
    except ValueError as exc:
        payload["error"] = f"{exc.__class__.__name__}: {exc}"
        return payload

    containers = raw.get("containers") if isinstance(raw, dict) else None
    if not isinstance(containers, list):
        payload["error"] = "missing containers list"
        return payload

    by_pod_name, by_short_name = _workload_lookup_tables(workloads)
    selected: list[dict[str, Any]] = []
    for item in containers:
        if not isinstance(item, dict):
            continue
        labels = item.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        matched = None
        pod_name = str(labels.get("ae.pod_name") or "").strip()
        if pod_name:
            matched = by_pod_name.get(pod_name)
        if matched is None:
            app_name = str(labels.get("ae.app") or "").strip()
            if app_name:
                matched = by_short_name.get(app_name)
        if matched is None:
            continue
        summary = {
            "role": str(matched.get("role") or ""),
            "app_name": str(matched.get("app_name") or ""),
            "name": item.get("name"),
            "labels": labels,
            "uid": item.get("uid"),
            "host_ports": item.get("host_ports"),
            "port_map": item.get("port_map"),
            "host_ip": item.get("host_ip"),
            "restart_count": item.get("restart_count"),
            "started_at": item.get("started_at"),
            "running": item.get("running"),
            "pod_ip": item.get("pod_ip"),
        }
        selected.append(summary)
    payload["all_count"] = len(containers)
    payload["containers"] = selected
    return payload


def _controller_health_probe_payload(api_endpoint: str) -> dict[str, Any]:
    health_url = _cell_health_url(api_endpoint)
    payload: dict[str, Any] = {
        "api_endpoint": str(api_endpoint or "").strip(),
        "health_url": health_url,
        "ok": False,
    }
    if not health_url:
        payload["error"] = "missing health url"
        return payload
    try:
        resp = requests.get(health_url, timeout=5)
        payload["status_code"] = int(resp.status_code)
        payload["ok"] = 200 <= int(resp.status_code) < 300
        text = (resp.text or "").strip()
        if text:
            payload["body_excerpt"] = text[:400]
    except requests.RequestException as exc:
        payload["error"] = f"{exc.__class__.__name__}: {exc}"
    return payload


def _guest_join_debug_snapshot(
    *,
    config: egpu_validate.ValidationConfig,
    guest_ip: str,
    api_endpoint: str,
    workloads: list[dict[str, Any]],
    master_addr: str,
    master_port: int,
    node_containers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = str(api_endpoint or "").strip()
    if not raw:
        return {"status": "skipped", "detail": "missing api endpoint"}
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    port = parts.port
    if port is None:
        return {"status": "skipped", "detail": f"missing port in api endpoint {api_endpoint!r}"}
    probe_host = parts.hostname or guest_ip
    node_container_items = []
    if isinstance(node_containers, dict):
        raw_items = node_containers.get("containers")
        if isinstance(raw_items, list):
            node_container_items = [item for item in raw_items if isinstance(item, dict)]
    launcher_pod_ip = ""
    for item in node_container_items:
        if str(item.get("role") or "") == "ray-launcher":
            launcher_pod_ip = str(item.get("pod_ip") or "").strip()
            if launcher_pod_ip:
                break
    workload_specs: list[dict[str, str]] = []
    for workload in workloads:
        workload_specs.append(
            {
                "role": str(workload.get("role") or ""),
                "app_name": str(workload.get("app_name") or ""),
                "pod_name": _workload_pod_name(workload),
                "short_name": _app_name_parts(str(workload.get("app_name") or ""))[1],
            }
        )
    python_cmd = r"""
import json
import os
import shlex
import socket
import subprocess
import urllib.error
import urllib.request

PORT = int(os.environ["API_PORT"])
GUEST_IP = os.environ["GUEST_IP"]
PROBE_HOST = os.environ["PROBE_HOST"]
MASTER_ADDR = str(os.environ.get("MASTER_ADDR") or "").strip()
MASTER_PORT = int(os.environ.get("MASTER_PORT") or "0")
LAUNCHER_POD_IP = str(os.environ.get("LAUNCHER_POD_IP") or "").strip()
WORKLOAD_SPECS = json.loads(str(os.environ.get("WORKLOAD_SPECS") or "[]"))

def _probe(url: str) -> dict:
    payload = {"url": url, "ok": False}
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            body = resp.read(400).decode("utf-8", "replace").strip()
            payload["status_code"] = int(resp.getcode())
            payload["ok"] = 200 <= int(resp.getcode()) < 300
            if body:
                payload["body_excerpt"] = body[:400]
    except Exception as exc:
        payload["error"] = f"{exc.__class__.__name__}: {exc}"
    return payload

def _shell(command: str) -> dict:
    proc = subprocess.run(
        ["bash", "-lc", command],
        capture_output=True,
        text=True,
        check=False,
    )
    text = (proc.stdout or "").strip()
    if proc.stderr:
        text = f"{text}\n{proc.stderr.strip()}".strip()
    return {"returncode": int(proc.returncode), "detail": text}

def _ns_shell(pid: int, command: str) -> dict:
    if pid <= 0:
        return {"returncode": 1, "detail": "missing pid"}
    proc = subprocess.run(
        ["sudo", "nsenter", "-t", str(pid), "-n", "bash", "-lc", command],
        capture_output=True,
        text=True,
        check=False,
    )
    text = (proc.stdout or "").strip()
    if proc.stderr:
        text = f"{text}\n{proc.stderr.strip()}".strip()
    return {"returncode": int(proc.returncode), "detail": text}

def _ns_probe(pid: int, url: str) -> dict:
    if pid <= 0:
        return {"url": url, "ok": False, "error": "missing pid"}
    python_cmd = (
        "import json, sys, urllib.request; "
        "payload={'url': sys.argv[1], 'ok': False}; "
        "try:\n"
        "    resp = urllib.request.urlopen(sys.argv[1], timeout=3)\n"
        "    body = resp.read(400).decode('utf-8', 'replace').strip()\n"
        "    payload['status_code'] = int(resp.getcode())\n"
        "    payload['ok'] = 200 <= int(resp.getcode()) < 300\n"
        "    if body:\n"
        "        payload['body_excerpt'] = body[:400]\n"
        "except Exception as exc:\n"
        "    payload['error'] = f'{exc.__class__.__name__}: {exc}'\n"
        "print(json.dumps(payload))"
    )
    proc = subprocess.run(
        ["sudo", "nsenter", "-t", str(pid), "-n", "python3", "-c", python_cmd, url],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        payload = {"url": url, "ok": False, "error": (proc.stderr or proc.stdout or "").strip()}
    if not isinstance(payload, dict):
        payload = {"url": url, "ok": False, "error": "invalid probe payload"}
    return payload

def _ns_tcp_probe(pid: int, host: str, port: int) -> dict:
    payload = {"host": host, "port": int(port), "ok": False}
    if pid <= 0:
        payload["error"] = "missing pid"
        return payload
    if not host or port <= 0:
        payload["error"] = "missing target"
        return payload
    python_cmd = (
        "import json, socket, sys; "
        "host = sys.argv[1]; port = int(sys.argv[2]); payload={'host': host, 'port': port, 'ok': False}; "
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM); sock.settimeout(3.0); "
        "try:\n"
        "    sock.connect((host, port))\n"
        "    payload['ok'] = True\n"
        "except Exception as exc:\n"
        "    payload['error'] = f'{exc.__class__.__name__}: {exc}'\n"
        "finally:\n"
        "    sock.close()\n"
        "print(json.dumps(payload))"
    )
    proc = subprocess.run(
        ["sudo", "nsenter", "-t", str(pid), "-n", "python3", "-c", python_cmd, host, str(int(port))],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        parsed = payload | {"error": (proc.stderr or proc.stdout or "").strip()}
    return parsed if isinstance(parsed, dict) else payload

def _load_json_shell(args: list[str]) -> dict:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}

def _env_excerpt(pid: int) -> dict:
    if pid <= 0:
        return {"returncode": 1, "detail": "missing pid"}
    command = (
        "tr '\\0' '\\n' < /proc/{pid}/environ | "
        "egrep '^(MASTER_ADDR|FABRIC_IP|RAY_ROLE|API_PORT|DTYPE|MODEL_PATH|TP|PP|CUDA_VISIBLE_DEVICES)=' || true"
    ).format(pid=pid)
    return _shell("sudo bash -lc " + shlex.quote(command))

containers_payload = _load_json_shell(["sudo", "crictl", "ps", "-a", "-o", "json"])
containers = containers_payload.get("containers") if isinstance(containers_payload, dict) else None
if not isinstance(containers, list):
    containers = []

def _find_container(spec: dict) -> tuple[dict | None, str]:
    pod_name = str(spec.get("pod_name") or "").strip()
    short_name = str(spec.get("short_name") or "").strip()
    fallback = None
    fallback_match = ""
    for item in containers:
        if not isinstance(item, dict):
            continue
        labels = item.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        if pod_name and str(labels.get("ae.pod_name") or "").strip() == pod_name:
            return item, "ae.pod_name"
        if short_name and str(labels.get("ae.app") or "").strip() == short_name and fallback is None:
            fallback = item
            fallback_match = "ae.app"
    return fallback, fallback_match

workload_probes = {}
for spec in WORKLOAD_SPECS:
    role = str(spec.get("role") or "").strip()
    record = {
        "role": role,
        "app_name": str(spec.get("app_name") or ""),
        "pod_name": str(spec.get("pod_name") or ""),
    }
    container, matched_by = _find_container(spec if isinstance(spec, dict) else {})
    if not isinstance(container, dict):
        record["status"] = "container_not_found"
        workload_probes[role or f"workload-{len(workload_probes)}"] = record
        continue
    container_id = str(container.get("id") or "").strip()
    record["status"] = "ok" if container_id else "missing_container_id"
    record["container_id"] = container_id
    if matched_by:
        record["matched_by"] = matched_by
    if not container_id:
        workload_probes[role or f"workload-{len(workload_probes)}"] = record
        continue
    inspect_payload = _load_json_shell(["sudo", "crictl", "inspect", container_id])
    info = inspect_payload.get("info") if isinstance(inspect_payload, dict) else None
    pid = int((info or {}).get("pid") or 0) if isinstance(info, dict) else 0
    record["pid"] = pid
    record["ss"] = _ns_shell(pid, "ss -ltnp || true")
    record["env"] = _env_excerpt(pid)
    if role == "ray-launcher":
        record["loopback_health"] = _ns_probe(pid, f"http://127.0.0.1:{PORT}/health")
        if MASTER_ADDR and MASTER_PORT > 0:
            record["master_tcp"] = _ns_tcp_probe(pid, MASTER_ADDR, MASTER_PORT)
    elif role == "ray-head":
        if MASTER_PORT > 0:
            record["local_master_tcp"] = _ns_tcp_probe(pid, "127.0.0.1", MASTER_PORT)
        if MASTER_ADDR and MASTER_PORT > 0:
            record["advertised_master_tcp"] = _ns_tcp_probe(pid, MASTER_ADDR, MASTER_PORT)
    workload_probes[role or f"workload-{len(workload_probes)}"] = record

payload = {
    "loopback_health": _probe(f"http://127.0.0.1:{PORT}/health"),
    "guest_ip_health": _probe(f"http://{GUEST_IP}:{PORT}/health"),
    "ss_18080": _shell(f"ss -ltnp | grep -F ':{PORT}' || true"),
    "crictl_ps": _shell("sudo crictl ps -a || true"),
    "ip_route": _shell("ip route || true"),
    "ip_addr_ae0": _shell("ip addr show ae0 || true"),
    "ip_addr_cni0": _shell("ip addr show cni0 || true"),
    "hostport_nat_18080": _shell(f"sudo iptables -t nat -S | grep -F -- '--dport {PORT}' || true"),
    "route_to_launcher_pod": _shell(f"ip route get {LAUNCHER_POD_IP} || true") if LAUNCHER_POD_IP else {
        "returncode": 0,
        "detail": "missing launcher pod ip",
    },
    "workload_probes": workload_probes,
}
if PROBE_HOST and PROBE_HOST not in {"127.0.0.1", GUEST_IP}:
    payload["probe_host_health"] = _probe(f"http://{PROBE_HOST}:{PORT}/health")
print(json.dumps(payload))
""".strip()
    remote_cmd = (
        f"GUEST_IP={shlex.quote(guest_ip)} "
        f"PROBE_HOST={shlex.quote(probe_host)} "
        f"API_PORT={int(port)} "
        f"MASTER_ADDR={shlex.quote(str(master_addr or '').strip())} "
        f"MASTER_PORT={int(master_port)} "
        f"LAUNCHER_POD_IP={shlex.quote(launcher_pod_ip)} "
        f"WORKLOAD_SPECS={shlex.quote(json.dumps(workload_specs))} "
        f"python3 -c {shlex.quote(python_cmd)}"
    )
    proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote(remote_cmd),
    )
    detail = (proc.stdout or "").strip()
    if proc.stderr:
        detail = f"{detail}\n{proc.stderr.strip()}".strip()
    payload = _parse_probe_payload(detail)
    if isinstance(payload, dict):
        payload["status"] = "ok" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            payload["detail"] = _truncate_probe_text(detail)
        for key in (
            "ss_18080",
            "crictl_ps",
            "ip_route",
            "ip_addr_ae0",
            "ip_addr_cni0",
            "hostport_nat_18080",
            "route_to_launcher_pod",
        ):
            entry = payload.get(key)
            if isinstance(entry, dict) and entry.get("detail"):
                entry["detail"] = _truncate_probe_text(entry["detail"], limit=8000)
        workload_probes = payload.get("workload_probes")
        if isinstance(workload_probes, dict):
            for probe in workload_probes.values():
                if not isinstance(probe, dict):
                    continue
                for key in ("ss", "env"):
                    entry = probe.get(key)
                    if isinstance(entry, dict) and entry.get("detail"):
                        entry["detail"] = _truncate_probe_text(entry["detail"], limit=8000)
        return payload
    return {
        "status": "failed" if proc.returncode else "unknown",
        "detail": _truncate_probe_text(detail),
        "returncode": int(proc.returncode),
    }


def _logs_indicate_startup_failure(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(
        token in lowered
        for token in (
            "traceback",
            "runtimeerror",
            "exception",
            "error:",
            "failed to",
            "address already in use",
            "cuda error",
        )
    )


def _classify_join_debug(
    *,
    controller_probe: dict[str, Any],
    guest_snapshot: dict[str, Any] | None,
    launcher_logs: str,
    head_logs: str,
) -> str:
    if bool((controller_probe or {}).get("ok")):
        return "listener_reachable_controller"
    snapshot = guest_snapshot or {}
    workload_probes = (
        (snapshot.get("workload_probes") or {}) if isinstance(snapshot, dict) else {}
    )
    launcher_probe = (
        workload_probes.get("ray-launcher") if isinstance(workload_probes, dict) else None
    )
    launcher_in_pod_ok = bool(
        ((launcher_probe or {}).get("loopback_health") or {}).get("ok")
        if isinstance(launcher_probe, dict)
        else False
    )
    loopback_ok = bool(((snapshot.get("loopback_health") or {}) if isinstance(snapshot, dict) else {}).get("ok"))
    guest_ip_ok = bool(((snapshot.get("guest_ip_health") or {}) if isinstance(snapshot, dict) else {}).get("ok"))
    if launcher_in_pod_ok and guest_ip_ok:
        return "listener_reachable_guest_local_only"
    if launcher_in_pod_ok:
        return "listener_in_pod_only"
    if loopback_ok and guest_ip_ok:
        return "listener_reachable_guest_local_only"
    if loopback_ok and not guest_ip_ok:
        return "listener_loopback_only"
    ss_detail = str(((snapshot.get("ss_18080") or {}) if isinstance(snapshot, dict) else {}).get("detail") or "")
    if ("127.0.0.1:" in ss_detail or "::1:" in ss_detail) and not guest_ip_ok:
        return "listener_loopback_only"
    if _logs_indicate_startup_failure(launcher_logs) or _logs_indicate_startup_failure(head_logs):
        return "launcher_failed"
    if "18080" not in ss_detail:
        return "launcher_running_no_listener"
    return "unknown"


def _write_join_debug_artifact(
    *,
    ae: list[str],
    env: dict[str, str],
    cell_name: str,
    status_payload: dict[str, Any],
    path: Path,
    join_debug_context: dict[str, Any] | None,
) -> None:
    allocations = status_payload.get("allocations") or {}
    execution = allocations.get("execution") or {}
    workloads = [item for item in list(execution.get("workloads") or []) if isinstance(item, dict)]
    api_endpoint = str(allocations.get("api_endpoint") or "").strip()
    payload: dict[str, Any] = {
        "status": "failed" if str(status_payload.get("phase") or "").strip().upper() == "FAILED" else "timeout",
        "cell_name": cell_name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "phase": str(status_payload.get("phase") or ""),
        "last_error": str(status_payload.get("last_error") or ""),
        "api_endpoint": api_endpoint,
        "health_url": _cell_health_url(api_endpoint),
        "status_payload": status_payload,
        "workloads": workloads,
    }
    controller_probe = _controller_health_probe_payload(api_endpoint)
    payload["controller_probe"] = controller_probe
    workload_logs: dict[str, Any] = {}
    launcher_log_text = ""
    head_log_text = ""
    guest_snapshot: dict[str, Any] | None = None
    node_containers: dict[str, Any] | None = None
    guest_config = None
    guest_ip = ""
    if join_debug_context is not None:
        guest_config = join_debug_context.get("config")
        guest_ip = str(join_debug_context.get("guest_ip") or "").strip()
        if guest_ip:
            node_containers = _node_containers_payload(guest_ip=guest_ip, workloads=workloads)
            payload["node_containers"] = node_containers
    for item in workloads:
        role = str(item.get("role") or "")
        app_name = str(item.get("app_name") or "")
        if not role or not app_name:
            continue
        namespace, short_name = _app_name_parts(app_name)
        if not short_name:
            continue
        cmd = [*ae, "logs"]
        if namespace:
            cmd.extend(["--namespace", namespace])
        cmd.extend([short_name, "--tail", "200"])
        capture = _command_capture_payload(cmd=cmd, env=env)
        if (
            _capture_needs_cri_fallback(capture)
            and isinstance(guest_config, egpu_validate.ValidationConfig)
            and guest_ip
        ):
            capture["cri_fallback"] = _guest_cri_log_capture_payload(
                config=guest_config,
                guest_ip=guest_ip,
                workload=item,
                tail=200,
            )
        workload_logs[role] = {"app_name": app_name, **capture}
        log_text = str(capture.get("detail") or "")
        cri_fallback = capture.get("cri_fallback")
        if _capture_needs_cri_fallback(capture) and isinstance(cri_fallback, dict):
            fallback_detail = str(cri_fallback.get("detail") or "")
            if fallback_detail:
                log_text = fallback_detail
        if role == "ray-launcher":
            launcher_log_text = log_text
        elif role == "ray-head":
            head_log_text = log_text
    if workload_logs:
        payload["workload_logs"] = workload_logs
    if join_debug_context is not None:
        config = join_debug_context.get("config")
        guest_ip = str(join_debug_context.get("guest_ip") or "").strip()
        if isinstance(config, egpu_validate.ValidationConfig) and guest_ip and api_endpoint:
            guest_snapshot = _guest_join_debug_snapshot(
                config=config,
                guest_ip=guest_ip,
                api_endpoint=api_endpoint,
                workloads=workloads,
                master_addr=str(allocations.get("master_addr") or ""),
                master_port=int(allocations.get("master_port") or 0),
                node_containers=node_containers,
            )
            payload["guest_snapshot"] = guest_snapshot
    payload["classification"] = _classify_join_debug(
        controller_probe=controller_probe,
        guest_snapshot=guest_snapshot,
        launcher_logs=launcher_log_text,
        head_logs=head_log_text,
    )
    _write_json(path, payload)


def _join_debug_context(args: argparse.Namespace) -> dict[str, Any] | None:
    try:
        config = egpu_validate.make_config(args)
    except Exception:
        return None
    if not (config.guest_ip or config.vm_name):
        return None
    try:
        guest_target = egpu_validate.resolve_guest_target(config)
    except Exception:
        return {"config": config, "guest_ip": ""}
    return {
        "config": config,
        "guest_ip": str(guest_target.get("guest_ip") or "").strip(),
    }


def _ensure_guest_vllm_image_probe(
    *,
    args: argparse.Namespace,
    run_root: Path,
    plan: dict[str, Any],
    test_vllm_image: str,
) -> None:
    if not _cell_lanes_require_host_a_seed(plan):
        return
    config = egpu_validate.make_config(args)
    if not (config.guest_ip or config.vm_name):
        return
    guest_target = egpu_validate.resolve_guest_target(config)
    guest_ip = str(guest_target["guest_ip"])
    guest_repo = str(guest_target.get("guest_repo") or config.guest_repo or "").strip()
    if not guest_repo:
        raise SystemExit("unable to determine guest repo path for vLLM image probe")
    guest_script = PurePosixPath(guest_repo) / "scripts" / "cri_torch_cuda_probe.sh"
    probe_timeout = str(
        os.getenv("AE_CRI_PROBE_TIMEOUT", DEFAULT_CRI_PROBE_TIMEOUT) or DEFAULT_CRI_PROBE_TIMEOUT
    ).strip() or DEFAULT_CRI_PROBE_TIMEOUT
    probe_cmd = (
        f"AE_CRI_PROBE_IMAGE={shlex.quote(test_vllm_image)} "
        f"AE_CRI_RUNTIME_HANDLER={shlex.quote(config.runtime_handler)} "
        f"AE_CRI_PROBE_TIMEOUT={shlex.quote(probe_timeout)} "
        f"{shlex.quote(str(guest_script))}"
    )
    started_at = time.monotonic()
    proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote(probe_cmd),
    )
    duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
    artifact_path = run_root / "ae" / "vllm-image-probe.json"
    transcript_path = run_root / "ae" / "vllm-image-probe.transcript.txt"
    detail = (proc.stdout or "").strip()
    if proc.stderr:
        detail = f"{detail}\n{proc.stderr.strip()}".strip()
    _write_capture(transcript_path, detail)
    raw_result = _parse_probe_payload(detail)
    probe_result = _probe_python_result(raw_result)
    payload: dict[str, Any] = {
        "status": "failed",
        "guest_ip": guest_ip,
        "guest_repo": guest_repo,
        "image": test_vllm_image,
        "runtime_handler": config.runtime_handler,
        "probe_timeout": str(raw_result.get("timeout") or probe_timeout)
        if isinstance(raw_result, dict)
        else probe_timeout,
        "duration_ms": int(raw_result.get("duration_ms"))
        if isinstance(raw_result, dict) and isinstance(raw_result.get("duration_ms"), int)
        else duration_ms,
        "phase": (
            str(raw_result.get("phase") or "").strip()
            if isinstance(raw_result, dict) and str(raw_result.get("phase") or "").strip()
            else ("python" if probe_result is not None else "unknown")
        ),
        "detail": (
            str(raw_result.get("error") or "").strip()
            if isinstance(raw_result, dict) and str(raw_result.get("error") or "").strip()
            else _truncate_probe_text(detail)
        ),
        "transcript": str(transcript_path),
    }
    if isinstance(raw_result, dict) and isinstance(raw_result.get("durations_ms"), dict):
        payload["durations_ms"] = raw_result["durations_ms"]
    if probe_result is not None:
        payload["result"] = probe_result
    debug_excerpt = _probe_debug_excerpt(raw_result)
    if debug_excerpt:
        payload["debug_excerpt"] = debug_excerpt
    if (
        proc.returncode == 0
        and probe_result is not None
        and str(probe_result.get("status") or "").strip().lower() == "ready"
        and bool(probe_result.get("cuda_is_available"))
        and bool(probe_result.get("cuda_tensor_ok"))
    ):
        payload["status"] = "ready"
    _write_json(artifact_path, payload)
    result_summary = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    _progress(
        "vllm-probe: "
        f"status={payload['status']} "
        f"phase={payload.get('phase') or 'unknown'} "
        f"cuda_available={result_summary.get('cuda_is_available')} "
        f"cuda_tensor_ok={result_summary.get('cuda_tensor_ok')} "
        f"duration_ms={payload.get('duration_ms')}"
    )
    if payload["status"] != "ready":
        raise SystemExit(f"selected vLLM image failed guest CUDA probe -> {artifact_path}")


def _ensure_guest_test_model(
    *,
    args: argparse.Namespace,
    run_root: Path,
    test_model: TestModelSpec,
) -> None:
    plan = build_plan(
        run_id=str(args.run_id),
        runs_dir=Path(args.runs_dir),
        cell_lane_names=list(args.cell_lane or []),
    )
    if not _cell_lanes_require_host_a_seed(plan):
        return
    config = egpu_validate.make_config(args)
    if not (config.guest_ip or config.vm_name):
        return
    guest_target = egpu_validate.resolve_guest_target(config)
    guest_ip = str(guest_target["guest_ip"])
    guest_repo = str(guest_target.get("guest_repo") or config.guest_repo or "").strip()
    if not guest_repo:
        raise SystemExit("unable to determine guest repo path for model bootstrap")
    guest_python = "python3"
    guest_script = PurePosixPath(guest_repo) / "scripts" / "dev" / "bootstrap_inference_model.py"
    inner_cmd = [
        guest_python,
        str(guest_script),
        "--model-id",
        test_model.model_id,
        "--local-path",
        test_model.local_path,
        "--json",
    ]
    if test_model.revision:
        inner_cmd.extend(["--revision", test_model.revision])
    remote_cmd = "sudo bash -lc " + shlex.quote(" ".join(shlex.quote(part) for part in inner_cmd))
    proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command=remote_cmd,
    )
    artifact_path = run_root / "ae" / "model-bootstrap.json"
    detail = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        payload = {
            "status": "failed",
            "guest_ip": guest_ip,
            "guest_repo": guest_repo,
            "model_id": test_model.model_id,
            "revision": test_model.revision,
            "local_path": test_model.local_path,
            "detail": detail,
        }
        _write_json(artifact_path, payload)
        raise SystemExit(f"failed to bootstrap test model on guest -> {artifact_path}")
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        payload = {
            "status": "failed",
            "guest_ip": guest_ip,
            "guest_repo": guest_repo,
            "model_id": test_model.model_id,
            "revision": test_model.revision,
            "local_path": test_model.local_path,
            "detail": detail,
            "error": f"JSONDecodeError: {exc}",
        }
        _write_json(artifact_path, payload)
        raise SystemExit(f"invalid model bootstrap output -> {artifact_path}") from exc
    payload = {
        "status": str(result.get("status") or "ready"),
        "guest_ip": guest_ip,
        "guest_repo": guest_repo,
        "results": [result],
    }
    _write_json(artifact_path, payload)
    _progress(
        "model-bootstrap: "
        f"status={payload['status']} "
        f"result={result.get('result') or 'unknown'} "
        f"model_id={result.get('model_id') or test_model.model_id} "
        f"local_path={result.get('local_path') or test_model.local_path}"
    )
    if payload["status"] != "ready":
        raise SystemExit(f"guest test model bootstrap reported failure -> {artifact_path}")


def _ensure_guest_vllm_startup_probe(
    *,
    args: argparse.Namespace,
    run_root: Path,
    plan: dict[str, Any],
    test_vllm_image: str,
) -> None:
    if not _cell_lanes_require_host_a_seed(plan):
        return
    target = _host_a_rendered_manifest(plan)
    if target is None:
        return
    lane_name, rendered_manifest = target
    startup_config = _rendered_manifest_startup_probe_config(rendered_manifest)
    model_path = str(startup_config.get("model_path") or "").strip()
    dtype = str(startup_config.get("dtype") or "").strip() or None
    config = egpu_validate.make_config(args)
    if not (config.guest_ip or config.vm_name):
        return
    guest_target = egpu_validate.resolve_guest_target(config)
    guest_ip = str(guest_target["guest_ip"])
    guest_repo = str(guest_target.get("guest_repo") or config.guest_repo or "").strip()
    if not guest_repo:
        raise SystemExit("unable to determine guest repo path for vLLM startup probe")
    guest_script = PurePosixPath(guest_repo) / "scripts" / "cri_vllm_startup_probe.sh"
    probe_timeout = str(
        os.getenv("AE_CRI_VLLM_STARTUP_PROBE_TIMEOUT", DEFAULT_CRI_PROBE_TIMEOUT)
        or DEFAULT_CRI_PROBE_TIMEOUT
    ).strip() or DEFAULT_CRI_PROBE_TIMEOUT
    probe_parts = [
        f"AE_CRI_PROBE_IMAGE={shlex.quote(test_vllm_image)}",
        f"AE_CRI_RUNTIME_HANDLER={shlex.quote(config.runtime_handler)}",
        f"AE_CRI_PROBE_TIMEOUT={shlex.quote(probe_timeout)}",
        f"AE_CRI_MODEL_PATH={shlex.quote(model_path)}",
    ]
    if dtype:
        probe_parts.append(f"AE_CRI_VLLM_DTYPE={shlex.quote(dtype)}")
    probe_parts.append(shlex.quote(str(guest_script)))
    probe_cmd = " ".join(probe_parts)
    started_at = time.monotonic()
    proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote(probe_cmd),
    )
    duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
    artifact_path = run_root / "ae" / "vllm-startup-probe.json"
    transcript_path = run_root / "ae" / "vllm-startup-probe.transcript.txt"
    detail = (proc.stdout or "").strip()
    if proc.stderr:
        detail = f"{detail}\n{proc.stderr.strip()}".strip()
    _write_capture(transcript_path, detail)
    raw_result = _parse_probe_payload(detail)
    startup_result = (
        raw_result.get("result")
        if isinstance(raw_result, dict) and isinstance(raw_result.get("result"), dict)
        else None
    )
    payload: dict[str, Any] = {
        "status": "failed",
        "guest_ip": guest_ip,
        "guest_repo": guest_repo,
        "cell_lane": lane_name,
        "rendered_manifest": str(rendered_manifest),
        "image": test_vllm_image,
        "runtime_handler": config.runtime_handler,
        "model_path": model_path,
        "probe_timeout": str(raw_result.get("timeout") or probe_timeout)
        if isinstance(raw_result, dict)
        else probe_timeout,
        "duration_ms": int(raw_result.get("duration_ms"))
        if isinstance(raw_result, dict) and isinstance(raw_result.get("duration_ms"), int)
        else duration_ms,
        "phase": (
            str(raw_result.get("phase") or "").strip()
            if isinstance(raw_result, dict) and str(raw_result.get("phase") or "").strip()
            else "unknown"
        ),
        "detail": (
            str(raw_result.get("error") or "").strip()
            if isinstance(raw_result, dict) and str(raw_result.get("error") or "").strip()
            else _truncate_probe_text(detail)
        ),
        "transcript": str(transcript_path),
    }
    if dtype:
        payload["dtype"] = dtype
    if isinstance(raw_result, dict) and isinstance(raw_result.get("durations_ms"), dict):
        payload["durations_ms"] = raw_result["durations_ms"]
    if startup_result is not None:
        payload["result"] = startup_result
    debug_excerpt = _probe_debug_excerpt(raw_result)
    if debug_excerpt:
        payload["debug_excerpt"] = debug_excerpt
    if (
        proc.returncode == 0
        and isinstance(raw_result, dict)
        and str(raw_result.get("status") or "").strip().lower() == "ready"
    ):
        payload["status"] = "ready"
    _write_json(artifact_path, payload)
    result_summary = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    _progress(
        "vllm-startup-probe: "
        f"status={payload['status']} "
        f"phase={payload.get('phase') or 'unknown'} "
        f"ready_signal={result_summary.get('ready_signal')} "
        f"duration_ms={payload.get('duration_ms')}"
    )
    if payload["status"] != "ready":
        raise SystemExit(f"selected vLLM image failed guest startup probe -> {artifact_path}")


def _ensure_guest_validation_seed_cache(
    *,
    args: argparse.Namespace,
    run_root: Path,
    plan: dict[str, Any],
) -> None:
    config = egpu_validate.make_config(args)
    if not (config.guest_ip or config.vm_name):
        return
    guest_target = egpu_validate.resolve_guest_target(config)
    guest_ip = str(guest_target["guest_ip"])
    _progress(f"validation-seed: resolving guest cache state guest_ip={guest_ip}")
    image_groups = _validation_seed_image_groups(plan=plan, args=args)
    seed_manifest = _seed_manifest_for_images(
        run_root=run_root,
        filename="validation-seed-manifest.json",
        image_groups=image_groups,
    )
    required_images = _required_seed_images(image_groups)
    required_image_refs = _required_seed_image_refs(image_groups)
    artifact_path = run_root / "ae" / "validation-seed-cache.json"

    _rewrite_seed_manifest_images(
        manifest_path=seed_manifest,
        image_groups=image_groups,
    )
    cleanup = {
        "status": "skipped",
        "detail": "",
        "removed_images": [],
        "purged_namespace": False,
        "avail_before_bytes": None,
        "avail_after_bytes": None,
        "required_free_bytes": None,
    }

    seed_run_id, bundle = _seed_bundle_paths(str(args.run_id), label="validation-seed")
    host_env = os.environ.copy()
    selected_vllm_image = _normalize_image_ref(_selected_test_vllm_image(args))
    metadata_proc = _run_command(
        cmd=[
            "bash",
            str(CRI_SEED_BUNDLE_SCRIPT),
            "--run-id",
            seed_run_id,
            "--manifest",
            str(seed_manifest),
            "--profile",
            CRI_SEED_VALIDATION_PROFILE,
            "--metadata-only",
        ],
        env=host_env,
    )
    if metadata_proc.returncode != 0:
        detail = _capture_text(metadata_proc).strip()
        _write_json(
            artifact_path,
            {
                "status": "failed",
                "guest_ip": guest_ip,
                "profile": CRI_SEED_VALIDATION_PROFILE,
                "manifest": str(seed_manifest),
                "cleanup": cleanup,
                "detail": detail,
            },
        )
        raise SystemExit(f"failed to resolve validation seed metadata -> {artifact_path}")

    expected_images = _seed_bundle_image_specs(bundle)
    expected_by_ref = {image.ref: image for image in expected_images}
    image_groups = {
        section: [expected_by_ref.get(image.ref) or image for image in refs]
        for section, refs in image_groups.items()
    }
    full_image_groups = {
        section: list(refs)
        for section, refs in image_groups.items()
    }
    required_images = _required_seed_images(image_groups)
    required_image_refs = [image.ref for image in required_images]
    _rewrite_seed_manifest_images(
        manifest_path=seed_manifest,
        image_groups=image_groups,
    )
    before_images = _guest_image_states(
        config=config,
        guest_ip=guest_ip,
        image_refs=required_image_refs,
    )
    missing_before, stale_before, fresh_before = _classify_guest_seed_images(
        required_images,
        before_images,
    )
    _progress(
        "validation-seed: guest cache classified "
        f"fresh={len(fresh_before)} missing={len(missing_before)} stale={len(stale_before)}"
    )
    refresh_before = _ordered_unique([*missing_before, *stale_before])
    if not refresh_before:
        _progress("validation-seed: guest cache already fresh; skipping import")
        _write_json(
            artifact_path,
            {
                "status": "ready",
                "guest_ip": guest_ip,
                "profile": CRI_SEED_VALIDATION_PROFILE,
                "manifest": str(seed_manifest),
                "bundle": str(bundle),
                "missing_before": [],
                "stale_before": [],
                "fresh_before": fresh_before,
                "expected_images": [_seed_image_entry_payload(image) for image in required_images],
                "guest_images_before": before_images,
                "selected_vllm_image": selected_vllm_image,
                "selected_vllm_image_present_after_import": bool(
                    (before_images.get(selected_vllm_image) or {}).get("present")
                ),
                "selected_vllm_image_matches_expected_after_import": (
                    selected_vllm_image in expected_by_ref
                    and bool((before_images.get(selected_vllm_image) or {}).get("present"))
                    and _image_id_matches_expected(
                        guest_image_id=(before_images.get(selected_vllm_image) or {}).get("image_id"),
                        expected_image_id=expected_by_ref[selected_vllm_image].expected_image_id,
                    )
                ),
            },
        )
        return

    def build_validation_seed_bundle(
        current_image_groups: dict[str, list[SeedImageSpec]],
        *,
        progress_message: str,
    ) -> list[SeedImageSpec]:
        _rewrite_seed_manifest_images(
            manifest_path=seed_manifest,
            image_groups=current_image_groups,
        )
        _progress(progress_message)

        build_proc = _run_command(
            cmd=[
                "bash",
                str(CRI_SEED_BUNDLE_SCRIPT),
                "--run-id",
                seed_run_id,
                "--manifest",
                str(seed_manifest),
                "--profile",
                CRI_SEED_VALIDATION_PROFILE,
                "--output",
                str(bundle),
            ],
            env=host_env,
        )
        if build_proc.returncode != 0:
            detail = _capture_text(build_proc).strip()
            _write_json(
                artifact_path,
                {
                    "status": "failed",
                    "guest_ip": guest_ip,
                    "profile": CRI_SEED_VALIDATION_PROFILE,
                    "missing_before": missing_before,
                    "stale_before": stale_before,
                    "fresh_before": fresh_before,
                    "expected_images": [_seed_image_entry_payload(image) for image in required_images],
                    "guest_images_before": before_images,
                    "bundle": str(bundle),
                    "manifest": str(seed_manifest),
                    "cleanup": cleanup,
                    "detail": detail,
                },
            )
            raise SystemExit(f"failed to build validation seed bundle -> {artifact_path}")

        built_expected_images = _seed_bundle_image_specs(bundle)
        if built_expected_images:
            expected_by_ref.update({image.ref: image for image in built_expected_images})
        return [expected_by_ref.get(image.ref) or image for image in required_images]

    build_image_groups = _filter_seed_image_groups(
        image_groups=image_groups,
        keep_refs=set(refresh_before),
    )
    required_images = build_validation_seed_bundle(
        build_image_groups,
        progress_message=f"validation-seed: building bundle for {len(refresh_before)} image(s)",
    )

    if _uses_lab_vm_hostshare(guest_target):
        guest_bundle = PurePosixPath("/mnt/host") / bundle.relative_to(ROOT)
        _progress(f"validation-seed: importing via hostshare bundle={guest_bundle}")
        import_inner = (
            "mkdir -p /mnt/host && "
            "mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host >/dev/null 2>&1 || true; "
            f"bundle={shlex.quote(str(guest_bundle))}; "
            'test -f "$bundle" && ctr -n k8s.io images import --no-unpack "$bundle"'
        )
    else:
        bundle_size_bytes = bundle.stat().st_size if bundle.exists() else 0
        required_free_bytes = _required_guest_seed_space_bytes(bundle_size_bytes)
        cleanup = _cleanup_guest_seed_staging(
            config=config,
            guest_ip=guest_ip,
            before_refs=set(before_images),
            required_images=required_image_refs,
            stale_refs=stale_before,
            min_free_bytes=required_free_bytes,
        )
        if cleanup["status"] != "ok":
            _write_json(
                artifact_path,
                {
                    "status": "failed",
                    "guest_ip": guest_ip,
                    "profile": CRI_SEED_VALIDATION_PROFILE,
                    "missing_before": missing_before,
                    "stale_before": stale_before,
                    "fresh_before": fresh_before,
                    "expected_images": [_seed_image_entry_payload(image) for image in required_images],
                    "guest_images_before": before_images,
                    "bundle": str(bundle),
                    "manifest": str(seed_manifest),
                    "bundle_size_bytes": bundle_size_bytes,
                    "cleanup": cleanup,
                },
            )
            raise SystemExit(f"guest validation seed staging space insufficient -> {artifact_path}")
        if cleanup["purged_namespace"]:
            required_images = build_validation_seed_bundle(
                full_image_groups,
                progress_message=(
                    "validation-seed: namespace purge invalidated fresh cache; "
                    f"rebuilding full bundle for {len(required_image_refs)} image(s)"
                ),
            )
            bundle_size_bytes = bundle.stat().st_size if bundle.exists() else 0
            required_free_bytes = _required_guest_seed_space_bytes(bundle_size_bytes)
            cleanup["required_free_bytes"] = required_free_bytes
            avail_after_bytes = cleanup.get("avail_after_bytes")
            if avail_after_bytes is not None and int(avail_after_bytes) < int(required_free_bytes):
                cleanup["status"] = "failed"
                _write_json(
                    artifact_path,
                    {
                        "status": "failed",
                        "guest_ip": guest_ip,
                        "profile": CRI_SEED_VALIDATION_PROFILE,
                        "missing_before": missing_before,
                        "stale_before": stale_before,
                        "fresh_before": fresh_before,
                        "expected_images": [_seed_image_entry_payload(image) for image in required_images],
                        "guest_images_before": before_images,
                        "bundle": str(bundle),
                        "manifest": str(seed_manifest),
                        "bundle_size_bytes": bundle_size_bytes,
                        "cleanup": cleanup,
                    },
                )
                raise SystemExit(f"guest validation seed staging space insufficient -> {artifact_path}")
        guest_bundle = PurePosixPath("/tmp") / f"{seed_run_id}-cri-seed-images.oci.tar"
        _progress(
            "validation-seed: copying bundle to guest "
            f"bundle_size_bytes={bundle_size_bytes} guest_bundle={guest_bundle}"
        )
        copy_proc = _run_command(
            cmd=[
                *_scp_base_command(config),
                str(bundle),
                f"{config.ssh_user}@{guest_ip}:{guest_bundle}",
            ],
            env=host_env,
        )
        if copy_proc.returncode != 0:
            detail = _capture_text(copy_proc).strip()
            _write_json(
                artifact_path,
                {
                    "status": "failed",
                    "guest_ip": guest_ip,
                    "profile": CRI_SEED_VALIDATION_PROFILE,
                    "missing_before": missing_before,
                    "stale_before": stale_before,
                    "fresh_before": fresh_before,
                    "expected_images": [_seed_image_entry_payload(image) for image in required_images],
                    "guest_images_before": before_images,
                    "bundle": str(bundle),
                    "bundle_size_bytes": bundle_size_bytes,
                    "guest_bundle": str(guest_bundle),
                    "manifest": str(seed_manifest),
                    "cleanup": cleanup,
                    "detail": detail,
                },
            )
            raise SystemExit(f"failed to copy validation seed bundle to guest -> {artifact_path}")
        import_inner = (
            f"bundle={shlex.quote(str(guest_bundle))}; "
            'test -f "$bundle" && ctr -n k8s.io images import --no-unpack "$bundle"; '
            'status=$?; rm -f "$bundle"; exit "$status"'
        )
    import_started_at = time.monotonic()
    _progress("validation-seed: importing guest bundle")
    import_proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote(import_inner),
    )
    import_duration_ms = max(0, int((time.monotonic() - import_started_at) * 1000))
    if import_proc.returncode != 0:
        detail = (import_proc.stderr or import_proc.stdout or "").strip()
        _write_json(
            artifact_path,
            {
                "status": "failed",
                "guest_ip": guest_ip,
                "profile": CRI_SEED_VALIDATION_PROFILE,
                "missing_before": missing_before,
                "stale_before": stale_before,
                "fresh_before": fresh_before,
                "expected_images": [_seed_image_entry_payload(image) for image in required_images],
                "guest_images_before": before_images,
                "bundle": str(bundle),
                "bundle_size_bytes": bundle.stat().st_size if bundle.exists() else 0,
                "guest_bundle": str(guest_bundle),
                "manifest": str(seed_manifest),
                "cleanup": cleanup,
                "import_duration_ms": import_duration_ms,
                "selected_vllm_image": selected_vllm_image,
                "detail": detail,
            },
        )
        raise SystemExit(f"failed to import validation seed bundle on guest -> {artifact_path}")

    after_images = _guest_image_states(
        config=config,
        guest_ip=guest_ip,
        image_refs=required_image_refs,
    )
    missing_after, stale_after, fresh_after = _classify_guest_seed_images(required_images, after_images)
    _progress(
        "validation-seed: import complete "
        f"duration_ms={import_duration_ms} fresh={len(fresh_after)} "
        f"missing={len(missing_after)} stale={len(stale_after)}"
    )
    status = "ready" if not missing_after and not stale_after else "failed"
    _write_json(
        artifact_path,
        {
            "status": status,
            "guest_ip": guest_ip,
            "profile": CRI_SEED_VALIDATION_PROFILE,
            "bundle": str(bundle),
            "bundle_size_bytes": bundle.stat().st_size if bundle.exists() else 0,
            "guest_bundle": str(guest_bundle),
            "manifest": str(seed_manifest),
            "missing_before": missing_before,
            "stale_before": stale_before,
            "fresh_before": fresh_before,
            "missing_after": missing_after,
            "stale_after": stale_after,
            "fresh_after": fresh_after,
            "expected_images": [_seed_image_entry_payload(image) for image in required_images],
            "guest_images_before": before_images,
            "guest_images_after": after_images,
            "cleanup": cleanup,
            "import_duration_ms": import_duration_ms,
            "selected_vllm_image": selected_vllm_image,
            "selected_vllm_image_present_after_import": bool(
                (after_images.get(selected_vllm_image) or {}).get("present")
            ),
            "selected_vllm_image_matches_expected_after_import": (
                selected_vllm_image in expected_by_ref
                and bool((after_images.get(selected_vllm_image) or {}).get("present"))
                and _image_id_matches_expected(
                    guest_image_id=(after_images.get(selected_vllm_image) or {}).get("image_id"),
                    expected_image_id=expected_by_ref[selected_vllm_image].expected_image_id,
                )
            ),
        },
    )
    if missing_after or stale_after:
        raise SystemExit(f"guest validation seed cache still stale or missing required images -> {artifact_path}")


def run_collect(args: argparse.Namespace) -> int:
    plan = build_plan(
        run_id=str(args.run_id),
        runs_dir=Path(args.runs_dir),
        cell_lane_names=list(args.cell_lane or []),
    )
    run_root = Path(plan["run_root"])
    if run_root.exists():
        if not args.force:
            raise SystemExit(f"run root already exists: {run_root} (use --force to overwrite)")
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    _write_json(Path(plan["inventory"]["plan"]), plan)
    _progress(
        f"collect: run_id={plan['run_id']} run_root={run_root} "
        f"cells={len(plan['cells'])}"
    )

    test_model = _selected_test_model(args)
    test_vllm_image = _selected_test_vllm_image(args)
    _progress(f"collect: preparing rendered manifests image={test_vllm_image} model={test_model.model_id}")
    _prepare_rendered_manifests(
        plan=plan,
        test_model=test_model,
        test_vllm_image=test_vllm_image,
    )
    _progress("collect: ensuring guest validation seed cache")
    _ensure_guest_validation_seed_cache(args=args, run_root=run_root, plan=plan)

    phases: list[dict[str, Any]] = []
    egpu_summary: dict[str, Any] | None = None
    if args.skip_egpu_passthrough_validate:
        _progress("collect: skipping egpu_passthrough_validate")
        phases.append(
            {
                "phase": "egpu_passthrough_validate",
                "status": "skipped",
                "detail": "skipped via --skip-egpu-passthrough-validate",
            }
        )
    else:
        _progress("collect: running egpu_passthrough_validate")
        egpu_config = egpu_validate.make_config(args)
        egpu_summary = egpu_validate.run_validation(egpu_config)
        phases.append(
            {
                "phase": "egpu_passthrough_validate",
                "status": egpu_summary["status"],
                "detail": f"guest_ip={egpu_summary['guest']['guest_ip']}",
                "checks": egpu_summary["checks"],
            }
        )
        if egpu_summary["status"] != "passed":
            summary = {
                "run_id": plan["run_id"],
                "run_root": plan["run_root"],
                "status": "failed",
                "phase_status": phases,
                "checks": egpu_summary["checks"],
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_json(Path(plan["inventory"]["summary"]), summary)
            print(json.dumps(summary, indent=2))
            return 1

    env = _ae_env(controller_env=args.controller_env)
    ae = _ae_prefix(str(args.ae_bin))
    join_debug_context = _join_debug_context(args)
    _progress("collect: probing selected vLLM image on guest")
    _ensure_guest_vllm_image_probe(
        args=args,
        run_root=run_root,
        plan=plan,
        test_vllm_image=test_vllm_image,
    )
    _progress("collect: ensuring guest test model")
    _ensure_guest_test_model(args=args, run_root=run_root, test_model=test_model)
    _progress("collect: probing selected vLLM startup on guest")
    _ensure_guest_vllm_startup_probe(
        args=args,
        run_root=run_root,
        plan=plan,
        test_vllm_image=test_vllm_image,
    )
    _progress("collect: capturing ae nodes inventory")
    _run_capture(cmd=[*ae, "nodes", "--json"], path=Path(plan["inventory"]["nodes"]), env=env)

    for cell in plan["cells"]:
        artifacts = cell["artifacts"]
        name = str(cell["name"])
        manifest = str(artifacts["rendered_manifest"])
        _progress(f"collect: validating cell={name} initial apply")
        _best_effort_delete_cell(ae=ae, name=name, env=env)
        initial_status = _wait_for_cell_ready(
            ae=ae,
            manifest=manifest,
            name=name,
            apply_path=Path(artifacts["apply"]),
            status_path=Path(artifacts["status_initial"]),
            env=env,
            timeout_s=float(args.cell_ready_timeout),
            poll_interval_s=float(args.cell_ready_poll_interval),
            join_debug_path=Path(artifacts["join_debug_initial"]),
            join_debug_context=join_debug_context,
        )
        _run_capture(
            cmd=[*ae, "cell", "events", name, "--limit", str(args.limit_events)],
            path=Path(artifacts["events_initial"]),
            env=env,
        )
        _progress(f"collect: probing cell={name} initial api")
        _probe_cell_api(status_payload=initial_status, path=Path(artifacts["api_probe_initial"]))
        _run_capture(cmd=[*ae, "cell", "delete", name], path=Path(artifacts["delete"]), env=env)
        _progress(f"collect: validating cell={name} reapplied")
        reapplied_status = _wait_for_cell_ready(
            ae=ae,
            manifest=manifest,
            name=name,
            apply_path=Path(artifacts["reapply"]),
            status_path=Path(artifacts["status_reapplied"]),
            env=env,
            timeout_s=float(args.cell_ready_timeout),
            poll_interval_s=float(args.cell_ready_poll_interval),
            join_debug_path=Path(artifacts["join_debug_reapplied"]),
            join_debug_context=join_debug_context,
        )
        _run_capture(
            cmd=[*ae, "cell", "events", name, "--limit", str(args.limit_events)],
            path=Path(artifacts["events_reapplied"]),
            env=env,
        )
        _progress(f"collect: probing cell={name} reapplied api")
        _probe_cell_api(
            status_payload=reapplied_status,
            path=Path(artifacts["api_probe_reapplied"]),
        )
        _run_capture(cmd=[*ae, "cell", "delete", name], path=Path(artifacts["teardown"]), env=env)
        _progress(f"collect: completed cell={name}")

    phases.append(
        {
            "phase": "cell_validation",
            "status": "passed",
            "detail": f"validated {len(plan['cells'])} cells",
        }
    )
    summary = {
        "run_id": plan["run_id"],
        "run_root": plan["run_root"],
        "status": "passed",
        "cell_count": len(plan["cells"]),
        "phase_status": phases,
        "checks": egpu_summary["checks"] if egpu_summary else {"egpu_passthrough_validate": "skipped"},
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(Path(plan["inventory"]["summary"]), summary)
    _progress("collect: complete status=passed")
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    args = parse_args()
    if args.cmd == "plan":
        plan = build_plan(
            run_id=str(args.run_id),
            runs_dir=Path(args.runs_dir),
            cell_lane_names=list(args.cell_lane or []),
        )
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            print(f"run_root: {plan['run_root']}")
            print(f"nodes:    {plan['inventory']['nodes']}")
            for cell in plan["cells"]:
                print(f"{cell['name']}: {cell['manifest']}")
        return 0
    if args.cmd == "collect":
        return run_collect(args)
    raise SystemExit(f"unsupported command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
