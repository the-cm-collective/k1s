#!/usr/bin/env python3
"""Start/stop dev stack components as CRI pod sandboxes."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CRI_ENDPOINT = "unix:///run/containerd/containerd.sock"
IMAGE_POLICIES = {"prompt", "pull", "fail"}


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_runtime_handler(runtime_handler: str | None = None) -> str | None:
    value = runtime_handler
    if value is None:
        value = os.getenv("AE_CRI_RUNTIME_HANDLER", "runc")
    value = str(value or "").strip()
    return value or None


def _runp_args(pod_cfg: Path, runtime_handler: str | None = None) -> list[str]:
    handler = _resolve_runtime_handler(runtime_handler)
    if handler:
        return ["runp", "-r", handler, str(pod_cfg)]
    return ["runp", str(pod_cfg)]


def _pod_payload(
    component: str,
    work: Path,
    labels: dict[str, str],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "metadata": {
            "name": component,
            "namespace": "k1s-dev",
            "uid": component,
            "attempt": 0,
        },
        "labels": labels,
        "log_directory": str(work / "logs"),
        "linux": {
            "security_context": {
                # Host network keeps core endpoint mapping deterministic for dev.
                "namespace_options": {"network": 2}
            }
        },
    }
    if _truthy(os.getenv("AE_CRI_SET_HOSTNAME")):
        payload["hostname"] = component
    return payload


def _run(
    cmd: list[str], *, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,  # noqa: S603 - command is assembled from fixed tool names + caller args.
            check=check,
            text=True,
            capture_output=capture,
        )
    except subprocess.CalledProcessError as exc:
        stdout = (exc.stdout or "").strip()
        stderr = (exc.stderr or "").strip()
        detail = stderr or stdout or f"exit {exc.returncode}"
        raise RuntimeError(f"command failed: {' '.join(cmd)} :: {detail}") from exc


def _crictl_base() -> list[str]:
    endpoint = os.getenv("AE_CRI_ENDPOINT", DEFAULT_CRI_ENDPOINT)
    bin_name = os.getenv("CRICTL_BIN", "crictl")
    return [bin_name, "--runtime-endpoint", endpoint]


def _crictl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run([*_crictl_base(), *args], check=check, capture=True)


def _check_ready() -> None:
    info = _crictl(["info"]).stdout
    payload = json.loads(info)
    conds: dict[str, bool] = {}
    for cond in ((payload.get("status") or {}).get("conditions") or []):
        conds[str(cond.get("type") or "")] = bool(cond.get("status"))
    if not conds.get("RuntimeReady", False):
        raise RuntimeError("CRI RuntimeReady is false")


def _list_pods() -> list[dict]:
    out = _crictl(["pods", "-o", "json"]).stdout
    payload = json.loads(out or "{}")
    return list(payload.get("items") or [])


def _list_containers() -> list[dict]:
    out = _crictl(["ps", "-a", "-o", "json"]).stdout
    payload = json.loads(out or "{}")
    return list(payload.get("containers") or [])


def _labels(item: dict) -> dict[str, str]:
    raw = item.get("labels") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _find_pod(profile: str, component: str) -> dict | None:
    for pod in _list_pods():
        labels = _labels(pod)
        if labels.get("ae.stack.profile") != profile:
            continue
        if labels.get("ae.stack.component") != component:
            continue
        return pod
    return None


def _pod_id(item: dict) -> str:
    return str(item.get("id") or item.get("podSandboxId") or item.get("pod_sandbox_id") or "")


def _container_pod_id(item: dict) -> str:
    return str(item.get("podSandboxId") or item.get("pod_sandbox_id") or "")


def _component_running_container(pod_id: str, component: str) -> bool:
    if not pod_id:
        return False
    pod_has_running = False
    for c in _list_containers():
        if _container_pod_id(c) != pod_id:
            continue
        state = str(c.get("state") or "").upper()
        if state == "CONTAINER_RUNNING":
            pod_has_running = True
        labels = _labels(c)
        if labels.get("ae.stack.component") != component:
            continue
        if state == "CONTAINER_RUNNING":
            return True
    # Fallback for transient label/list races: if the pod has a running container,
    # treat the component as running to avoid false container-not-running churn.
    return pod_has_running


def _component_running_elsewhere(profile: str, component: str, exclude_pod_id: str = "") -> str | None:
    for c in _list_containers():
        labels = _labels(c)
        if labels.get("ae.stack.profile") != profile:
            continue
        if labels.get("ae.stack.component") != component:
            continue
        pod = _container_pod_id(c)
        if exclude_pod_id and pod == exclude_pod_id:
            continue
        state = str(c.get("state") or "").upper()
        if state == "CONTAINER_RUNNING":
            return pod or "unknown"
    return None


def _remove_pod(pod_id: str) -> None:
    if not pod_id:
        return
    _crictl(["stopp", pod_id], check=False)
    _crictl(["rmp", pod_id], check=False)


def _events_log_path(profile: str) -> Path:
    return ROOT / "state" / "profiles" / profile / "cri" / "events.log"


def _record_event(profile: str, component: str, action: str, reason: str, details: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} component={component} action={action} reason={reason}"
    if details:
        line = f"{line} {details}"
    path = _events_log_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _stable_hash(parts: list[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _rathole_server_has_services(config_path: Path) -> bool:
    try:
        text = config_path.read_text(encoding="utf-8")
    except Exception:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[server.services."):
            return True
    return False


def _read_int_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return int(raw)
    except Exception:
        return None


def _write_int_file(path: Path, value: int) -> bool:
    try:
        path.write_text(f"{int(value)}\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _count_inotify_watches(top_n: int = 3) -> tuple[int, list[tuple[int, int, str]]]:
    per_pid: dict[int, int] = {}
    proc_root = Path("/proc")
    try:
        proc_entries = list(proc_root.iterdir())
    except Exception:
        return 0, []

    for proc_entry in proc_entries:
        name = proc_entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        fdinfo_dir = proc_entry / "fdinfo"
        if not fdinfo_dir.is_dir():
            continue
        pid_count = 0
        try:
            fd_entries = list(fdinfo_dir.iterdir())
        except Exception:
            continue
        for fdinfo in fd_entries:
            try:
                with fdinfo.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if line.startswith("inotify"):
                            pid_count += 1
            except Exception:
                continue
        if pid_count > 0:
            per_pid[pid] = pid_count

    total = sum(per_pid.values())
    top: list[tuple[int, int, str]] = []
    for pid, count in sorted(per_pid.items(), key=lambda item: item[1], reverse=True)[:top_n]:
        cmd = ""
        try:
            cmd_raw = (Path(f"/proc/{pid}/cmdline")).read_bytes()
            cmd = cmd_raw.replace(b"\x00", b" ").decode("utf-8", "ignore").strip()
        except Exception:
            cmd = ""
        top.append((pid, count, cmd))
    return total, top


def _rathole_inotify_mitigate(component: str) -> str | None:
    warn_pct = int(os.getenv("AE_RATHOLE_INOTIFY_WARN_PCT", "95") or "95")
    auto_tune = _truthy(os.getenv("AE_RATHOLE_INOTIFY_AUTOTUNE", "1"))
    max_cap = int(os.getenv("AE_RATHOLE_INOTIFY_MAX_WATCHES", "4194304") or "4194304")

    max_path = Path("/proc/sys/fs/inotify/max_user_watches")
    max_watches = _read_int_file(max_path)
    if max_watches is None or max_watches <= 0:
        return None

    used, top = _count_inotify_watches(top_n=3)
    pct = int((used * 100) / max_watches) if max_watches > 0 else 0
    if pct < warn_pct:
        return None

    top_summary = ", ".join(
        f"{pid}:{count}:{(cmd[:60] + '...') if len(cmd) > 60 else cmd}"
        for pid, count, cmd in top
    )

    if auto_tune and os.geteuid() == 0:
        target = max(max_watches * 2, used + 131072)
        target = min(target, max_cap)
        if target > max_watches and _write_int_file(max_path, target):
            return (
                f"{component}: high inotify usage detected ({used}/{max_watches}, {pct}%). "
                f"Raised fs.inotify.max_user_watches to {target}. "
                f"top={top_summary or '-'}"
            )

    return (
        f"{component}: high inotify usage detected ({used}/{max_watches}, {pct}%). "
        "Rathole may exit immediately with state=Completed. Free watches "
        "(for example close heavy file-watchers) or raise "
        "fs.inotify.max_user_watches. "
        f"top={top_summary or '-'}"
    )


def _resolve_registry_host() -> str:
    if str(os.getenv("AE_CRI_REGISTRY_MODE", "")).strip().lower() == "off":
        return ""
    return str(os.getenv("AE_CRI_REGISTRY") or os.getenv("AE_REGISTRY_HOST") or "").strip()


def _resolve_registry_namespace() -> str:
    raw = str(os.getenv("AE_CRI_REGISTRY_NAMESPACE", "")).strip().strip("/")
    return raw


def _has_registry_prefix(ref: str) -> bool:
    first = ref.split("/", 1)[0]
    return "." in first or ":" in first or first == "localhost"


def _resolve_image_ref(image: str) -> str:
    ref = image.strip()
    registry = _resolve_registry_host()
    if not registry:
        return ref

    digest = ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)
        digest = f"@{digest}"

    tag = ""
    last_segment = ref.rsplit("/", 1)[-1]
    if ":" in last_segment:
        ref, tag_part = ref.rsplit(":", 1)
        tag = f":{tag_part}"

    if _has_registry_prefix(ref):
        parts = ref.split("/", 1)
        if len(parts) == 2:
            ref = parts[1]

    namespace = _resolve_registry_namespace()
    if namespace:
        ref = f"{namespace}/{ref.lstrip('/')}"

    return f"{registry.rstrip('/')}/{ref.lstrip('/')}{tag}{digest}"


def _is_tty() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _noninteractive() -> bool:
    return not _is_tty() or _truthy(os.getenv("CI"))


def _resolve_image_policy() -> str:
    raw = str(os.getenv("AE_CRI_IMAGE_POLICY", "")).strip().lower()
    if raw in IMAGE_POLICIES:
        return raw
    if _noninteractive():
        return "fail"
    return "prompt"


def _image_exists(image: str) -> bool:
    proc = _crictl(["inspecti", image], check=False)
    return proc.returncode == 0


def _build_command(
    component: str, *, source_image: str, target_image: str
) -> list[str] | None:
    cri_endpoint = os.getenv("AE_CRI_ENDPOINT", DEFAULT_CRI_ENDPOINT)
    if component == "k1s-core-apishim":
        script = ROOT / "scripts" / "build_cri_apishim_image.sh"
        if not script.exists():
            return None
        return [
            "bash",
            str(script),
            "--image",
            target_image,
            "--cri-endpoint",
            cri_endpoint,
            "--push",
            "--pull-cri",
        ]
    if source_image == target_image:
        return None
    script = ROOT / "scripts" / "dev" / "cri_image_mirror.sh"
    if not script.exists():
        return None
    return [
        "bash",
        str(script),
        "--source",
        source_image,
        "--target",
        target_image,
        "--cri-endpoint",
        cri_endpoint,
        "--pull-cri",
    ]


def _build_action_text(component: str) -> str:
    if component == "k1s-core-apishim":
        return "build locally, push to OCI registry, then pull via CRI"
    return "mirror image to OCI registry, then pull via CRI"


def _pull_image(image: str) -> tuple[bool, str]:
    try:
        _crictl(["pull", image])
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _prompt_missing_image(
    *,
    image: str,
    component: str,
    build_cmd: list[str] | None,
    pull_error: str,
) -> str:
    print(f"[cri-stack] missing image for {component}: {image}", file=sys.stderr)
    if pull_error:
        print(f"[cri-stack] pull failed: {pull_error}", file=sys.stderr)
    print("[cri-stack] select image action:", file=sys.stderr)
    options: list[tuple[str, str]] = []
    if build_cmd:
        options.append(("b", _build_action_text(component)))
    options.append(("p", "retry image pull via CRI"))
    options.append(("a", "abort startup"))
    for key, text in options:
        print(f"  [{key}] {text}", file=sys.stderr)
    default = options[0][0]
    while True:
        choice = input(f"[cri-stack] choice [{default}]: ").strip().lower()
        if not choice:
            choice = default
        if any(choice == key for key, _ in options):
            return choice
        print(f"[cri-stack] invalid choice: {choice}", file=sys.stderr)


def _ensure_image(
    image: str, component: str, *, source_image: str | None = None
) -> None:
    if _image_exists(image):
        return

    policy = _resolve_image_policy()
    build_cmd = _build_command(
        component, source_image=source_image or image, target_image=image
    )

    if policy == "fail":
        raise RuntimeError(
            f"required image is missing: {image} "
            f"(set AE_CRI_IMAGE_POLICY=pull or prompt, or build it first)"
        )

    pulled, pull_error = _pull_image(image)
    if pulled or _image_exists(image):
        return

    if policy == "pull":
        raise RuntimeError(f"image pull failed for {image}: {pull_error}")

    if _noninteractive():
        raise RuntimeError(
            f"image pull failed for {image}: {pull_error}. "
            "Non-interactive mode cannot prompt; pre-build image or set AE_CRI_IMAGE_POLICY=pull."
        )

    choice = _prompt_missing_image(
        image=image,
        component=component,
        build_cmd=build_cmd,
        pull_error=pull_error,
    )

    if choice == "a":
        raise RuntimeError(f"user aborted startup for missing image: {image}")
    if choice == "p":
        pulled, pull_error = _pull_image(image)
        if pulled or _image_exists(image):
            return
        raise RuntimeError(f"image pull failed for {image}: {pull_error}")
    if choice == "b":
        if not build_cmd:
            raise RuntimeError(f"no build workflow is available for missing image: {image}")
        _run(build_cmd, check=True, capture=False)
        if _image_exists(image):
            return
        pulled, pull_error = _pull_image(image)
        if pulled or _image_exists(image):
            return
        raise RuntimeError(
            f"image still unavailable after build attempt: {image}; pull error: {pull_error}"
        )
    raise RuntimeError(f"unsupported image action: {choice}")


def _component_labels(profile: str, component: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": "k1s",
        "ae.stack.backend": "cri",
        "ae.stack.profile": profile,
        "ae.stack.component": component,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")




def _component_lock_path(profile: str, component: str) -> Path:
    return ROOT / "state" / "profiles" / profile / "cri" / ".locks" / f"{component}.lock"


@contextlib.contextmanager
def _component_lock(profile: str, component: str):
    path = _component_lock_path(profile, component)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _start_component(
    *,
    profile: str,
    component: str,
    image: str,
    runtime_handler: str | None = None,
    command: list[str] | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    mounts: list[dict[str, object]] | None = None,
    recreate: bool = False,
    resolve_image: bool = True,
    rollout_key: str | None = None,
    stable_seconds: int = 0,
) -> None:
    with _component_lock(profile, component):
        _start_component_unlocked(
            profile=profile,
            component=component,
            image=image,
            runtime_handler=runtime_handler,
            command=command,
            args=args,
            env=env,
            mounts=mounts,
            recreate=recreate,
            resolve_image=resolve_image,
            rollout_key=rollout_key,
            stable_seconds=stable_seconds,
        )

def _start_component_unlocked(
    *,
    profile: str,
    component: str,
    image: str,
    runtime_handler: str | None = None,
    command: list[str] | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    mounts: list[dict[str, object]] | None = None,
    recreate: bool = False,
    resolve_image: bool = True,
    rollout_key: str | None = None,
    stable_seconds: int = 0,
) -> None:
    labels = _component_labels(profile, component)
    if rollout_key:
        labels["ae.stack.rollout"] = rollout_key
    existing = _find_pod(profile, component)
    if existing is not None:
        existing_labels = _labels(existing)
        pod_id = _pod_id(existing)
        running = _component_running_container(pod_id, component)
        if running and not recreate:
            if rollout_key:
                current_key = existing_labels.get("ae.stack.rollout", "")
                if current_key == rollout_key:
                    print(f"[cri-stack] {component}: already running")
                    _record_event(profile, component, "reuse", "rollout-match", f"pod={pod_id}")
                    return
                reason = "rollout-changed" if current_key else "rollout-missing"
                _record_event(
                    profile,
                    component,
                    "recreate",
                    reason,
                    f"pod={pod_id} old={current_key or '-'} new={rollout_key}",
                )
            else:
                print(f"[cri-stack] {component}: already running")
                _record_event(profile, component, "reuse", "already-running", f"pod={pod_id}")
                return
        else:
            reason = "explicit-recreate" if recreate else "container-not-running"
            _record_event(profile, component, "recreate", reason, f"pod={pod_id}")
        _remove_pod(pod_id)

    resolved_image = _resolve_image_ref(image) if resolve_image else image
    _ensure_image(resolved_image, component, source_image=image)

    work = ROOT / "state" / "profiles" / profile / "cri" / component
    work.mkdir(parents=True, exist_ok=True)
    pod_cfg = work / "pod.json"
    ctr_cfg = work / "container.json"

    pod_payload = _pod_payload(component, work, labels)
    _write_json(pod_cfg, pod_payload)

    ctr_payload: dict[str, object] = {
        "metadata": {"name": "main", "attempt": 0},
        "image": {"image": resolved_image},
        "log_path": "main.log",
        "labels": labels,
    }
    if command:
        ctr_payload["command"] = command
    if args:
        ctr_payload["args"] = args
    if env:
        ctr_payload["envs"] = [{"key": k, "value": v} for k, v in sorted(env.items())]
    if mounts:
        ctr_payload["mounts"] = mounts
    _write_json(ctr_cfg, ctr_payload)

    pod_id = _crictl(_runp_args(pod_cfg, runtime_handler)).stdout.strip()
    if not pod_id:
        raise RuntimeError(f"failed to create pod sandbox for {component}")
    container_id = _crictl(["create", pod_id, str(ctr_cfg), str(pod_cfg)]).stdout.strip()
    if not container_id:
        _remove_pod(pod_id)
        raise RuntimeError(f"failed to create container for {component}")
    _crictl(["start", container_id])

    for _ in range(30):
        if _component_running_container(pod_id, component):
            if stable_seconds > 0:
                stable_deadline = time.monotonic() + float(stable_seconds)
                while time.monotonic() < stable_deadline:
                    if not _component_running_container(pod_id, component):
                        # Debounce transient CRI/list races before treating as unstable.
                        time.sleep(0.5)
                        if _component_running_container(pod_id, component):
                            continue

                        inspect_state = ""
                        inspect_reason = ""
                        inspect_message = ""
                        inspect_exit = None
                        try:
                            inspect_proc = _crictl(["inspect", container_id], check=False)
                            if inspect_proc.returncode == 0 and inspect_proc.stdout.strip():
                                payload = json.loads(inspect_proc.stdout)
                                status = payload.get("status") or {}
                                inspect_state = str(status.get("state") or "")
                                inspect_reason = str(status.get("reason") or "")
                                inspect_message = str(status.get("message") or "")
                                inspect_exit = status.get("exitCode")
                        except Exception:
                            inspect_state = "INSPECT_FAILED"

                        # If direct inspect still shows running/starting, keep waiting.
                        if inspect_state in {"CONTAINER_RUNNING", "CONTAINER_CREATED", ""}:
                            time.sleep(0.2)
                            continue

                        handoff_pod = _component_running_elsewhere(profile, component, exclude_pod_id=pod_id)
                        if handoff_pod:
                            _record_event(
                                profile,
                                component,
                                "start",
                                "running-handoff",
                                f"from={pod_id} to={handoff_pod} container={container_id}",
                            )
                            print(f"[cri-stack] {component}: running (handoff)")
                            return

                        inspect_summary = (
                            f"state={inspect_state or '-'} "
                            f"exit={inspect_exit if inspect_exit is not None else '-'} "
                            f"reason={inspect_reason or '-'} "
                            f"message={inspect_message or '-'}"
                        )
                        _remove_pod(pod_id)
                        _record_event(
                            profile,
                            component,
                            "start",
                            "failed-unstable",
                            f"pod={pod_id} container={container_id} {inspect_summary}".strip(),
                        )
                        raise RuntimeError(
                            f"component exited before stability window ({stable_seconds}s): {component}"
                        )
                    time.sleep(0.2)
            print(f"[cri-stack] {component}: running")
            _record_event(profile, component, "start", "running", f"pod={pod_id} container={container_id}")
            return
        time.sleep(0.2)
    _remove_pod(pod_id)
    _record_event(profile, component, "start", "failed-not-running", f"pod={pod_id} container={container_id}")
    raise RuntimeError(f"component did not reach running state: {component}")


def _mount(
    host_path: Path | str, container_path: str, *, readonly: bool = False
) -> dict[str, object]:
    return {
        "host_path": str(host_path),
        "container_path": container_path,
        "readonly": bool(readonly),
    }


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def _apishim_env(
    *,
    profile: str,
    port: int,
    cert_mount: str,
    key_mount: str,
    env_file: Path,
) -> dict[str, str]:
    env: dict[str, str] = {k: v for k, v in os.environ.items() if k.startswith("AE_")}
    env.update(_parse_env_file(env_file))
    env["AE_APISHIM_ENABLE"] = "1"
    env["AE_APISHIM_RUNTIME"] = (
        env.get("AE_APISHIM_RUNTIME") or env.get("AE_RUNTIME_BACKEND") or "cri"
    )
    env["AE_APISHIM_TLS_CERT"] = cert_mount
    env["AE_APISHIM_TLS_KEY"] = key_mount
    env["AE_APISHIM_SERVER"] = f"https://127.0.0.1:{port}"
    env.setdefault(
        "AE_APISHIM_DB",
        str(ROOT / "state" / "profiles" / profile / "apishim.db"),
    )
    return env


def _start_apishim(
    profile: str,
    port: int,
    host: str,
    env_file: Path,
    cert_file: Path,
    key_file: Path,
    *,
    runtime_handler: str | None = None,
    recreate: bool = False,
) -> None:
    cert_mount = "/etc/ae/apishim/tls.crt"
    key_mount = "/etc/ae/apishim/tls.key"
    cri_mounts: list[dict[str, object]] = []
    cri_endpoint = str(os.getenv("AE_CRI_ENDPOINT", DEFAULT_CRI_ENDPOINT) or "").strip()
    if cri_endpoint.startswith("unix://"):
        cri_sock = Path(cri_endpoint[len("unix://") :])
        if cri_sock.exists():
            cri_mounts.append(_mount(cri_sock, str(cri_sock)))
    apishim_env = _apishim_env(
        profile=profile,
        port=port,
        cert_mount=cert_mount,
        key_mount=key_mount,
        env_file=env_file,
    )
    _start_component(
        profile=profile,
        component="k1s-core-apishim",
        image=os.getenv("AE_APISHIM_IMAGE", "localhost/k1s-apishim:dev"),
        runtime_handler=runtime_handler,
        command=[
            "python",
            "-m",
            "ae.apishim",
            "serve",
            "--host",
            host,
            "--port",
            str(port),
            "--tls",
        ],
        env=apishim_env,
        mounts=[
            _mount(ROOT / "state", str(ROOT / "state")),
            _mount(cert_file, cert_mount, readonly=True),
            _mount(key_file, key_mount, readonly=True),
            *cri_mounts,
        ],
        recreate=recreate,
    )


def _start_etcd(
    profile: str, *, runtime_handler: str | None = None, recreate: bool = False
) -> None:
    etcd_data = ROOT / "state" / "etcd"
    etcd_data.mkdir(parents=True, exist_ok=True)
    _start_component(
        profile=profile,
        component="k1s-core-etcd",
        image="quay.io/coreos/etcd:v3.5.13",
        runtime_handler=runtime_handler,
        command=[
            "/usr/local/bin/etcd",
            "--name=etcd0",
            "--data-dir=/etcd-data",
            "--listen-client-urls=http://0.0.0.0:2379",
            "--advertise-client-urls=http://127.0.0.1:2379",
            "--listen-peer-urls=http://0.0.0.0:2380",
            "--initial-advertise-peer-urls=http://127.0.0.1:2380",
            "--initial-cluster=etcd0=http://127.0.0.1:2380",
            "--initial-cluster-state=new",
        ],
        mounts=[_mount(etcd_data, "/etcd-data")],
        recreate=recreate,
    )


def _start_nats_hub(
    profile: str, *, runtime_handler: str | None = None, recreate: bool = False
) -> None:
    nats_data = ROOT / "state" / "nats-hub"
    nats_data.mkdir(parents=True, exist_ok=True)
    _start_component(
        profile=profile,
        component="k1s-core-nats-hub",
        image="docker.io/library/nats:2.10",
        runtime_handler=runtime_handler,
        command=["nats-server", "-c", "/etc/nats/nats-hub.conf"],
        mounts=[
            _mount(
                ROOT / "ops" / "dev" / "nats-hub.conf",
                "/etc/nats/nats-hub.conf",
                readonly=True,
            ),
            _mount(nats_data, "/data"),
        ],
        recreate=recreate,
    )


def _start_postgres(
    profile: str, *, runtime_handler: str | None = None, recreate: bool = False
) -> None:
    pg_data = ROOT / "state" / "postgres"
    pg_data.mkdir(parents=True, exist_ok=True)
    _start_component(
        profile=profile,
        component="k1s-core-postgres",
        image="docker.io/library/postgres:16",
        runtime_handler=runtime_handler,
        env={
            "POSTGRES_USER": "shim",
            "POSTGRES_PASSWORD": "shim",
            "POSTGRES_DB": "shim",
        },
        mounts=[_mount(pg_data, "/var/lib/postgresql/data")],
        recreate=recreate,
    )


def _start_registry(
    profile: str,
    host: str,
    port: int,
    *,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    runtime_handler: str | None = None,
    recreate: bool = False,
) -> None:
    registry_data = ROOT / "state" / "registry"
    registry_data.mkdir(parents=True, exist_ok=True)

    if (tls_cert is None) != (tls_key is None):
        raise ValueError("registry TLS requires both tls_cert and tls_key")

    env = {
        "REGISTRY_HTTP_ADDR": f"{host}:{port}",
        "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY": "/var/lib/registry",
    }
    mounts = [_mount(registry_data, "/var/lib/registry")]

    if tls_cert is not None and tls_key is not None:
        if not tls_cert.is_file():
            raise FileNotFoundError(f"registry TLS cert not found: {tls_cert}")
        if not tls_key.is_file():
            raise FileNotFoundError(f"registry TLS key not found: {tls_key}")
        env["REGISTRY_HTTP_TLS_CERTIFICATE"] = "/etc/registry/tls/registry.crt"
        env["REGISTRY_HTTP_TLS_KEY"] = "/etc/registry/tls/registry.key"
        mounts.append(_mount(tls_cert, "/etc/registry/tls/registry.crt", readonly=True))
        mounts.append(_mount(tls_key, "/etc/registry/tls/registry.key", readonly=True))

    _start_component(
        profile=profile,
        component=f"{profile}-registry",
        image=os.getenv("AE_CRI_MANAGED_REGISTRY_IMAGE", "docker.io/library/registry:2"),
        runtime_handler=runtime_handler,
        env=env,
        mounts=mounts,
        recreate=recreate,
        resolve_image=False,
    )


def _core_base(
    profile: str, *, runtime_handler: str | None = None, recreate: bool = False
) -> None:
    _start_etcd(profile, runtime_handler=runtime_handler, recreate=recreate)
    _start_nats_hub(profile, runtime_handler=runtime_handler, recreate=recreate)
    _start_postgres(profile, runtime_handler=runtime_handler, recreate=recreate)


def _start_caddy(
    profile: str,
    metrics_port: int,
    apishim_port: int,
    *,
    runtime_handler: str | None = None,
    recreate: bool = False,
) -> None:
    caddy_data = ROOT / "state" / "caddy-data"
    caddy_sites = ROOT / "state" / "caddy"
    caddy_data.mkdir(parents=True, exist_ok=True)
    caddy_sites.mkdir(parents=True, exist_ok=True)
    _start_component(
        profile=profile,
        component="k1s-core-caddy",
        image="docker.io/library/caddy:2.8",
        runtime_handler=runtime_handler,
        command=[
            "caddy",
            "run",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        env={
            "CADDY_HOST_ALIAS": "127.0.0.1",
            "APISHIM_UPSTREAM": f"127.0.0.1:{apishim_port}",
            "METRICS_PORT": str(metrics_port),
        },
        mounts=[
            _mount(
                ROOT / "ops" / "dev" / "caddy" / "Caddyfile",
                "/etc/caddy/Caddyfile",
                readonly=True,
            ),
            _mount(ROOT / "state" / "caddy-cri", "/etc/caddy/sites", readonly=True),
            _mount(caddy_data, "/data"),
            _mount(caddy_sites, "/etc/caddy/dynsites", readonly=True),
            _mount(ROOT / "docs" / "site", "/srv/docs", readonly=True),
        ],
        recreate=recreate,
    )


def _start_envoy(
    profile: str,
    config: Path,
    *,
    runtime_handler: str | None = None,
    recreate: bool = False,
) -> None:
    cfg_hash = _file_sha256(config)
    rollout_key = _stable_hash(
        [
            "envoy",
            str(_resolve_runtime_handler(runtime_handler) or ""),
            str(os.getenv("AE_ENVOY_IMAGE", "docker.io/envoyproxy/envoy:v1.29-latest")),
            cfg_hash,
        ]
    )
    state_dir = (ROOT / "state").resolve()
    mounts: list[dict[str, object]] = [
        _mount(config, "/etc/envoy/envoy.yaml", readonly=True),
        # Relative cert paths like state/tls/* resolve to /state/* in the container.
        _mount(state_dir, "/state", readonly=True),
        # Absolute host paths written into config must also exist in-container.
        _mount(state_dir, str(state_dir), readonly=True),
    ]
    tls_root_raw = str(os.getenv("AE_TLS_DIR", "")).strip()
    if tls_root_raw:
        tls_root = Path(tls_root_raw).expanduser()
        if not tls_root.is_absolute():
            tls_root = (Path.cwd() / tls_root).resolve()
        else:
            tls_root = tls_root.resolve()
        if tls_root != state_dir and state_dir not in tls_root.parents:
            mounts.append(_mount(tls_root, str(tls_root), readonly=True))

    deduped_mounts: list[dict[str, object]] = []
    seen_mounts: set[tuple[str, str]] = set()
    for mount in mounts:
        key = (str(mount.get("host_path") or ""), str(mount.get("container_path") or ""))
        if key in seen_mounts:
            continue
        seen_mounts.add(key)
        deduped_mounts.append(mount)

    _start_component(
        profile=profile,
        component="k1s-core-envoy",
        image=os.getenv("AE_ENVOY_IMAGE", "docker.io/envoyproxy/envoy:v1.29-latest"),
        runtime_handler=runtime_handler,
        command=["envoy", "-c", "/etc/envoy/envoy.yaml", "--log-level", "info"],
        mounts=deduped_mounts,
        recreate=recreate,
        rollout_key=rollout_key,
    )


def _start_rathole_server(
    profile: str,
    config: Path,
    *,
    runtime_handler: str | None = None,
    recreate: bool = False,
) -> None:
    cfg_hash = _file_sha256(config)
    rollout_key = _stable_hash(
        [
            "rathole-server",
            str(_resolve_runtime_handler(runtime_handler) or ""),
            str(os.getenv("AE_RATHOLE_IMAGE", "docker.io/rapiz1/rathole:v0.5.0")),
            cfg_hash,
        ]
    )
    stable_seconds = 2 if _rathole_server_has_services(config) else 0
    try:
        _start_component(
            profile=profile,
            component="k1s-core-rathole",
            image=os.getenv("AE_RATHOLE_IMAGE", "docker.io/rapiz1/rathole:v0.5.0"),
            runtime_handler=runtime_handler,
            args=["--server", "/etc/rathole/server.toml"],
            mounts=[_mount(config, "/etc/rathole/server.toml", readonly=True)],
            recreate=recreate,
            rollout_key=rollout_key,
            stable_seconds=stable_seconds,
        )
    except RuntimeError as exc:
        if "stability window" not in str(exc):
            raise
        note = _rathole_inotify_mitigate("k1s-core-rathole")
        if note:
            print(f"[cri-stack] WARNING: {note}")
            _record_event(profile, "k1s-core-rathole", "diagnose", "inotify-pressure", note)
        _record_event(profile, "k1s-core-rathole", "retry", "unstable-first-start")
        time.sleep(1)
        _start_component(
            profile=profile,
            component="k1s-core-rathole",
            image=os.getenv("AE_RATHOLE_IMAGE", "docker.io/rapiz1/rathole:v0.5.0"),
            runtime_handler=runtime_handler,
            args=["--server", "/etc/rathole/server.toml"],
            mounts=[_mount(config, "/etc/rathole/server.toml", readonly=True)],
            recreate=True,
            rollout_key=rollout_key,
            stable_seconds=stable_seconds,
        )


def _start_edge_nats(
    profile: str,
    site_id: str,
    config: Path,
    *,
    runtime_handler: str | None = None,
    recreate: bool = False,
) -> None:
    _start_component(
        profile=profile,
        component=f"k1s-edge-nats-{site_id}",
        image="docker.io/library/nats:2.10",
        runtime_handler=runtime_handler,
        command=["nats-server", "-c", "/etc/nats/nats-edge.conf"],
        mounts=[_mount(config, "/etc/nats/nats-edge.conf", readonly=True)],
        recreate=recreate,
    )


def _start_rathole_client(
    profile: str,
    site_id: str,
    node_id: str,
    config: Path,
    *,
    runtime_handler: str | None = None,
    recreate: bool = False,
) -> None:
    cfg_hash = _file_sha256(config)
    rollout_key = _stable_hash(
        [
            "rathole-client",
            site_id,
            node_id,
            str(_resolve_runtime_handler(runtime_handler) or ""),
            str(os.getenv("AE_RATHOLE_IMAGE", "docker.io/rapiz1/rathole:v0.5.0")),
            cfg_hash,
        ]
    )
    try:
        stable_seconds_raw = os.getenv("AE_RATHOLE_CLIENT_STABLE_SECONDS", "0").strip()
        try:
            stable_seconds = max(0, int(stable_seconds_raw or "0"))
        except Exception:
            stable_seconds = 0
        _start_component(
            profile=profile,
            component=f"k1s-edge-rathole-{site_id}-{node_id}",
            image=os.getenv("AE_RATHOLE_IMAGE", "docker.io/rapiz1/rathole:v0.5.0"),
            runtime_handler=runtime_handler,
            args=["--client", "/etc/rathole/client.toml"],
            mounts=[_mount(config, "/etc/rathole/client.toml", readonly=True)],
            recreate=recreate,
            rollout_key=rollout_key,
            stable_seconds=stable_seconds,
        )
    except RuntimeError as exc:
        if "stability window" not in str(exc):
            raise
        note = _rathole_inotify_mitigate(f"k1s-edge-rathole-{site_id}-{node_id}")
        if note:
            print(f"[cri-stack] WARNING: {note}")
            _record_event(
                profile,
                f"k1s-edge-rathole-{site_id}-{node_id}",
                "diagnose",
                "inotify-pressure",
                note,
            )
        _record_event(profile, f"k1s-edge-rathole-{site_id}-{node_id}", "retry", "unstable-first-start")
        time.sleep(1)
        _start_component(
            profile=profile,
            component=f"k1s-edge-rathole-{site_id}-{node_id}",
            image=os.getenv("AE_RATHOLE_IMAGE", "docker.io/rapiz1/rathole:v0.5.0"),
            runtime_handler=runtime_handler,
            args=["--client", "/etc/rathole/client.toml"],
            mounts=[_mount(config, "/etc/rathole/client.toml", readonly=True)],
            recreate=True,
            rollout_key=rollout_key,
            stable_seconds=stable_seconds,
        )


def _down_profile(profile: str) -> None:
    for pod in _list_pods():
        labels = _labels(pod)
        if labels.get("ae.stack.profile") != profile:
            continue
        _remove_pod(_pod_id(pod))
    print(f"[cri-stack] profile={profile} cleaned")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-handler",
        default=os.getenv("AE_CRI_RUNTIME_HANDLER", "runc"),
        help="CRI runtime handler for runp (default: AE_CRI_RUNTIME_HANDLER or runc)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pre = sub.add_parser("preflight", help="verify CRI endpoint readiness")
    pre.add_argument("--profile", default="k1s-core")

    core = sub.add_parser("up-core-base", help="start etcd+nats-hub+postgres")
    core.add_argument("--profile", default="k1s-core")
    core.add_argument("--recreate", action="store_true")

    registry = sub.add_parser("up-registry", help="start local managed registry")
    registry.add_argument("--profile", default="k1s-core")
    registry.add_argument("--host", default="127.0.0.1")
    registry.add_argument("--port", type=int, default=5001)
    registry.add_argument("--tls-cert")
    registry.add_argument("--tls-key")
    registry.add_argument("--recreate", action="store_true")

    nh = sub.add_parser("up-nats-hub", help="start/reload hub nats")
    nh.add_argument("--profile", default="k1s-core")
    nh.add_argument("--recreate", action="store_true")

    apishim = sub.add_parser("up-apishim", help="start/reload apishim")
    apishim.add_argument("--profile", default="k1s-core")
    apishim.add_argument("--port", type=int, default=8445)
    apishim.add_argument("--host", default="127.0.0.1")
    apishim.add_argument("--env-file", required=True)
    apishim.add_argument("--cert-file", required=True)
    apishim.add_argument("--key-file", required=True)
    apishim.add_argument("--recreate", action="store_true")

    caddy = sub.add_parser("up-caddy", help="start caddy")
    caddy.add_argument("--profile", default="k1s-core")
    caddy.add_argument("--metrics-port", type=int, default=9108)
    caddy.add_argument("--apishim-port", type=int, default=8445)
    caddy.add_argument("--recreate", action="store_true")

    envoy = sub.add_parser("up-envoy", help="start envoy ingress")
    envoy.add_argument("--profile", default="k1s-core")
    envoy.add_argument("--config", required=True)
    envoy.add_argument("--recreate", action="store_true")

    rathole = sub.add_parser("up-rathole-server", help="start rathole server")
    rathole.add_argument("--profile", default="k1s-core")
    rathole.add_argument("--config", required=True)
    rathole.add_argument("--recreate", action="store_true")

    edge = sub.add_parser("up-edge-nats", help="start edge nats")
    edge.add_argument("--profile", default="k1s-edge")
    edge.add_argument("--site-id", required=True)
    edge.add_argument("--config", required=True)
    edge.add_argument("--recreate", action="store_true")

    rc = sub.add_parser("up-rathole-client", help="start edge rathole client")
    rc.add_argument("--profile", default="k1s-edge")
    rc.add_argument("--site-id", required=True)
    rc.add_argument("--node-id", required=True)
    rc.add_argument("--config", required=True)
    rc.add_argument("--recreate", action="store_true")

    down = sub.add_parser("down-profile", help="remove profile components")
    down.add_argument("--profile", required=True)

    args = parser.parse_args(argv)
    try:
        if args.cmd != "down-profile":
            _check_ready()
        if args.cmd == "preflight":
            print("[cri-stack] CRI preflight OK")
            return 0
        if args.cmd == "up-core-base":
            _core_base(
                args.profile,
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "up-registry":
            _start_registry(
                args.profile,
                args.host,
                args.port,
                tls_cert=Path(args.tls_cert).resolve() if args.tls_cert else None,
                tls_key=Path(args.tls_key).resolve() if args.tls_key else None,
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "up-nats-hub":
            _start_nats_hub(
                args.profile,
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "up-apishim":
            _start_apishim(
                args.profile,
                args.port,
                args.host,
                Path(args.env_file).resolve(),
                Path(args.cert_file).resolve(),
                Path(args.key_file).resolve(),
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "up-caddy":
            _start_caddy(
                args.profile,
                args.metrics_port,
                args.apishim_port,
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "up-envoy":
            _start_envoy(
                args.profile,
                Path(args.config).resolve(),
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "up-rathole-server":
            _start_rathole_server(
                args.profile,
                Path(args.config).resolve(),
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "up-edge-nats":
            _start_edge_nats(
                args.profile,
                args.site_id,
                Path(args.config).resolve(),
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "up-rathole-client":
            _start_rathole_client(
                args.profile,
                args.site_id,
                args.node_id,
                Path(args.config).resolve(),
                runtime_handler=args.runtime_handler,
                recreate=bool(args.recreate),
            )
            return 0
        if args.cmd == "down-profile":
            _down_profile(args.profile)
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[cri-stack] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
