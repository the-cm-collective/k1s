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
CRI_SEED_VALIDATION_PROFILE = "all"
HOST_A_CELL_LANES = frozenset({"cell-a-single", "cell-ab-pp2-ray", "cell-ab-pp2-mp"})
DEFAULT_TEST_MODEL_ID = "HuggingFaceTB/SmolLM2-1.7B-Instruct"
FAST_TEST_MODEL_ID = "HuggingFaceTB/SmolLM2-360M-Instruct"
DEFAULT_TEST_VLLM_IMAGE = "docker.io/vllm/vllm-openai:v0.6.2"
DEFAULT_SEEDED_VLLM_IMAGE = "docker.io/vllm/vllm-openai:latest"


@dataclass(frozen=True)
class CellLane:
    name: str
    manifest: Path


@dataclass(frozen=True)
class TestModelSpec:
    model_id: str
    revision: str | None
    local_path: str


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
                    "events_initial": str(cell_root / "events-initial.txt"),
                    "api_probe_initial": str(cell_root / "api-probe-initial.json"),
                    "delete": str(cell_root / "delete.txt"),
                    "reapply": str(cell_root / "reapply.txt"),
                    "status_reapplied": str(cell_root / "status-reapplied.json"),
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
            "Defaults to a CUDA 12.1-era image compatible with the current Host A driver lane."
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
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    attempt = 0
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
        if phase == "READY":
            return payload
        if phase == "FAILED":
            raise SystemExit(f"cell {name} entered FAILED phase -> {status_path}")
        if time.monotonic() >= deadline:
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


def _seed_manifest_payload(manifest: Path = CRI_SEED_MANIFEST) -> dict[str, Any]:
    return json.loads(manifest.read_text(encoding="utf-8"))


def _seed_manifest_image_groups(manifest: Path = CRI_SEED_MANIFEST) -> dict[str, list[str]]:
    payload = _seed_manifest_payload(manifest)
    images = payload.get("images") or {}
    if not isinstance(images, dict):
        raise SystemExit(f"invalid CRI seed manifest images payload in {manifest}")
    return {
        "bootstrap": _ordered_unique(
            [_normalize_image_ref(str(image)) for image in list(images.get("bootstrap") or [])]
        ),
        "core": _ordered_unique(
            [_normalize_image_ref(str(image)) for image in list(images.get("core") or [])]
        ),
        "edge": _ordered_unique(
            [_normalize_image_ref(str(image)) for image in list(images.get("edge") or [])]
        ),
    }


def _seed_manifest_for_images(
    *,
    run_root: Path,
    filename: str,
    image_groups: dict[str, list[str]],
) -> Path:
    payload = _seed_manifest_payload()
    payload["images"] = {
        "bootstrap": _ordered_unique(list(image_groups.get("bootstrap") or [])),
        "core": _ordered_unique(list(image_groups.get("core") or [])),
        "edge": _ordered_unique(list(image_groups.get("edge") or [])),
    }
    manifest_path = run_root / "ae" / filename
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return manifest_path


def _rewrite_seed_manifest_images(
    *,
    manifest_path: Path,
    image_groups: dict[str, list[str]],
) -> None:
    payload = _seed_manifest_payload(manifest_path)
    payload["images"] = {
        "bootstrap": _ordered_unique(list(image_groups.get("bootstrap") or [])),
        "core": _ordered_unique(list(image_groups.get("core") or [])),
        "edge": _ordered_unique(list(image_groups.get("edge") or [])),
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


def _validation_seed_image_groups(*, plan: dict[str, Any], args: argparse.Namespace) -> dict[str, list[str]]:
    base_groups = _seed_manifest_image_groups()
    egpu_config = egpu_validate.make_config(args)
    workload_images = _rendered_manifest_workload_images(plan)
    core_images = _ordered_unique([_normalize_image_ref(egpu_config.compute_image), *workload_images])
    return {
        "bootstrap": list(base_groups["bootstrap"]),
        "core": core_images,
        "edge": [],
    }


def _required_seed_images(image_groups: dict[str, list[str]]) -> list[str]:
    return _ordered_unique(
        [
            *list(image_groups.get("bootstrap") or []),
            *list(image_groups.get("core") or []),
            *list(image_groups.get("edge") or []),
        ]
    )


def _filter_seed_image_groups(
    *,
    image_groups: dict[str, list[str]],
    keep_refs: set[str],
) -> dict[str, list[str]]:
    return {
        section: [image for image in refs if image in keep_refs]
        for section, refs in image_groups.items()
    }


def _cleanup_guest_seed_staging(
    *,
    config: egpu_validate.ValidationConfig,
    guest_ip: str,
    before_refs: set[str],
    required_images: list[str],
) -> dict[str, Any]:
    obsolete_vllm = sorted(
        ref
        for ref in before_refs
        if ref.startswith("docker.io/vllm/vllm-openai:") and ref not in required_images
    )
    cleanup_parts = [
        "rm -f /tmp/*-cri-seed-images.oci.tar",
    ]
    for image in obsolete_vllm:
        cleanup_parts.append(
            f"ids=$(crictl ps -a --image {shlex.quote(image)} -q 2>/dev/null || true); "
            'if [[ -n "$ids" ]]; then crictl rm -f $ids >/dev/null 2>&1 || true; fi'
        )
        cleanup_parts.append(
            f"ctr -n k8s.io images rm --sync {shlex.quote(image)} >/dev/null 2>&1 || true"
        )
    cleanup_parts.append("df -h / /tmp /var/lib/containerd || true")
    proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote("set -euo pipefail; " + "; ".join(cleanup_parts)),
    )
    return {
        "removed_images": obsolete_vllm,
        "detail": (proc.stdout or proc.stderr or "").strip(),
        "status": "ok" if proc.returncode == 0 else "failed",
    }


def _seed_bundle_paths(run_id: str, *, label: str = "core-seed") -> tuple[str, Path]:
    seed_run_id = f"{run_id}-{label}"
    bundle = ROOT / "state" / "lab-vm" / seed_run_id / "seeds" / "cri-seed-images.oci.tar"
    return seed_run_id, bundle


def _guest_image_refs(*, config: egpu_validate.ValidationConfig, guest_ip: str) -> set[str]:
    runner = egpu_validate.SubprocessRunner()
    proc = egpu_validate.run_guest_command(
        runner,
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote("ctr -n k8s.io images ls -q 2>/dev/null || true"),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"failed to list guest CRI images for {guest_ip}: {detail}")
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


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
    if payload["status"] != "ready":
        raise SystemExit(f"guest test model bootstrap reported failure -> {artifact_path}")


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
    image_groups = _validation_seed_image_groups(plan=plan, args=args)
    seed_manifest = _seed_manifest_for_images(
        run_root=run_root,
        filename="validation-seed-manifest.json",
        image_groups=image_groups,
    )
    required_images = _required_seed_images(image_groups)
    before_refs = _guest_image_refs(config=config, guest_ip=guest_ip)
    missing_before = [image for image in required_images if image not in before_refs]
    artifact_path = run_root / "ae" / "validation-seed-cache.json"
    if not missing_before:
        _write_json(
            artifact_path,
            {
                "status": "ready",
                "guest_ip": guest_ip,
                "profile": CRI_SEED_VALIDATION_PROFILE,
                "manifest": str(seed_manifest),
                "missing_before": [],
            },
        )
        return

    _rewrite_seed_manifest_images(
        manifest_path=seed_manifest,
        image_groups=_filter_seed_image_groups(
            image_groups=image_groups,
            keep_refs=set(missing_before),
        ),
    )
    cleanup = {
        "status": "skipped",
        "detail": "",
        "removed_images": [],
    }
    if not _uses_lab_vm_hostshare(guest_target):
        cleanup = _cleanup_guest_seed_staging(
            config=config,
            guest_ip=guest_ip,
            before_refs=before_refs,
            required_images=required_images,
        )

    seed_run_id, bundle = _seed_bundle_paths(str(args.run_id), label="validation-seed")
    host_env = os.environ.copy()
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
                "bundle": str(bundle),
                "manifest": str(seed_manifest),
                "cleanup": cleanup,
                "detail": detail,
            },
        )
        raise SystemExit(f"failed to build validation seed bundle -> {artifact_path}")

    if _uses_lab_vm_hostshare(guest_target):
        guest_bundle = PurePosixPath("/mnt/host") / bundle.relative_to(ROOT)
        import_inner = (
            "mkdir -p /mnt/host && "
            "mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host >/dev/null 2>&1 || true; "
            f"bundle={shlex.quote(str(guest_bundle))}; "
            'test -f "$bundle" && ctr -n k8s.io images import --no-unpack "$bundle"'
        )
    else:
        guest_bundle = PurePosixPath("/tmp") / f"{seed_run_id}-cri-seed-images.oci.tar"
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
                    "bundle": str(bundle),
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
    import_proc = egpu_validate.run_guest_command(
        egpu_validate.SubprocessRunner(),
        config=config,
        guest_ip=guest_ip,
        command="sudo bash -lc " + shlex.quote(import_inner),
    )
    if import_proc.returncode != 0:
        detail = (import_proc.stderr or import_proc.stdout or "").strip()
        _write_json(
            artifact_path,
            {
                "status": "failed",
                "guest_ip": guest_ip,
                "profile": CRI_SEED_VALIDATION_PROFILE,
                "missing_before": missing_before,
                "bundle": str(bundle),
                "guest_bundle": str(guest_bundle),
                "manifest": str(seed_manifest),
                "cleanup": cleanup,
                "detail": detail,
            },
        )
        raise SystemExit(f"failed to import validation seed bundle on guest -> {artifact_path}")

    after_refs = _guest_image_refs(config=config, guest_ip=guest_ip)
    missing_after = [image for image in required_images if image not in after_refs]
    status = "ready" if not missing_after else "failed"
    _write_json(
        artifact_path,
        {
            "status": status,
            "guest_ip": guest_ip,
            "profile": CRI_SEED_VALIDATION_PROFILE,
            "bundle": str(bundle),
            "guest_bundle": str(guest_bundle),
            "manifest": str(seed_manifest),
            "missing_before": missing_before,
            "missing_after": missing_after,
            "cleanup": cleanup,
        },
    )
    if missing_after:
        raise SystemExit(f"guest validation seed cache still missing required images -> {artifact_path}")


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

    test_model = _selected_test_model(args)
    test_vllm_image = _selected_test_vllm_image(args)
    _prepare_rendered_manifests(
        plan=plan,
        test_model=test_model,
        test_vllm_image=test_vllm_image,
    )
    _ensure_guest_validation_seed_cache(args=args, run_root=run_root, plan=plan)

    phases: list[dict[str, Any]] = []
    egpu_summary: dict[str, Any] | None = None
    if args.skip_egpu_passthrough_validate:
        phases.append(
            {
                "phase": "egpu_passthrough_validate",
                "status": "skipped",
                "detail": "skipped via --skip-egpu-passthrough-validate",
            }
        )
    else:
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
    _ensure_guest_test_model(args=args, run_root=run_root, test_model=test_model)
    _run_capture(cmd=[*ae, "nodes", "--json"], path=Path(plan["inventory"]["nodes"]), env=env)

    for cell in plan["cells"]:
        artifacts = cell["artifacts"]
        name = str(cell["name"])
        manifest = str(artifacts["rendered_manifest"])
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
        )
        _run_capture(
            cmd=[*ae, "cell", "events", name, "--limit", str(args.limit_events)],
            path=Path(artifacts["events_initial"]),
            env=env,
        )
        _probe_cell_api(status_payload=initial_status, path=Path(artifacts["api_probe_initial"]))
        _run_capture(cmd=[*ae, "cell", "delete", name], path=Path(artifacts["delete"]), env=env)
        reapplied_status = _wait_for_cell_ready(
            ae=ae,
            manifest=manifest,
            name=name,
            apply_path=Path(artifacts["reapply"]),
            status_path=Path(artifacts["status_reapplied"]),
            env=env,
            timeout_s=float(args.cell_ready_timeout),
            poll_interval_s=float(args.cell_ready_poll_interval),
        )
        _run_capture(
            cmd=[*ae, "cell", "events", name, "--limit", str(args.limit_events)],
            path=Path(artifacts["events_reapplied"]),
            env=env,
        )
        _probe_cell_api(
            status_payload=reapplied_status,
            path=Path(artifacts["api_probe_reapplied"]),
        )
        _run_capture(cmd=[*ae, "cell", "delete", name], path=Path(artifacts["teardown"]), env=env)

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
