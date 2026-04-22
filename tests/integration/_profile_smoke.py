from __future__ import annotations

# ruff: noqa: S310, S323, S603, S607
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[2]
MAKE_BIN = shutil.which("make") or "make"
CURL_BIN = shutil.which("curl")
_TLS_CONTEXT = ssl._create_unverified_context()


@dataclass
class RunningTarget:
    target: str
    process: subprocess.Popen[str]
    log_path: Path
    log_handle: object


@dataclass(frozen=True)
class TlsProbe:
    host: str
    path: str
    text_fragment: str | None = None


_ISOLATED_ENV_KEYS = {
    "APISHIM_CONTAINER",
    "APISHIM_CONTAINER_PORT",
    "APISHIM_ENV_FILE",
    "APISHIM_HOST_PORT",
    "APISHIM_PORT",
    "APISHIM_PROFILE_DIR",
    "CADDY_HTTP_PORT",
    "CADDY_HTTPS_PORT",
    "CONTROLLER_ENV_FILE",
    "CURL_CA_BUNDLE",
    "DEV_ENV_FILE",
    "DOCS_API_BASE",
    "DOCS_DASHBOARD_URL",
    "GIT_SSL_CAINFO",
    "METRICS_PORT",
    "NODE_EXTRA_CA_CERTS",
    "POSTGRES_PORT",
    "PROFILE_DIR",
    "PROMETHEUS_PORT",
    "REQUESTS_CA_BUNDLE",
    "SPECS_DIR",
}


def tail_log(path: Path, max_chars: int = 8000) -> str:
    if not path.exists():
        return "(log file missing)"
    text = path.read_text(encoding="utf-8", errors="ignore")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def isolated_test_env(*, preserve: tuple[str, ...] = ()) -> dict[str, str]:
    env = os.environ.copy()
    keep = set(preserve)
    for key in list(env):
        if key in keep:
            continue
        if key.startswith("AE_") or key.startswith("APISHIM_") or key in _ISOLATED_ENV_KEYS:
            env.pop(key, None)
    return env


def runtime_available(candidate: str) -> bool:
    if not shutil.which(candidate):
        return False
    probe = subprocess.run(
        [candidate, "info"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def select_runtime() -> str | None:
    override = os.getenv("AE_PROFILE_SMOKE_RUNTIME", "").strip()
    if override:
        return override if runtime_available(override) else None
    for candidate in ("podman", "docker"):
        if runtime_available(candidate):
            return candidate
    return None


def compose_available(runtime: str) -> bool:
    compose_probe = subprocess.run(
        [runtime, "compose", "version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if compose_probe.returncode == 0:
        return True
    if runtime == "podman":
        return shutil.which("podman-compose") is not None
    if runtime == "docker":
        return shutil.which("docker-compose") is not None
    return False


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _describe_tree(path: Path, *, max_entries: int = 24) -> str:
    if not path.exists():
        return "(path missing)"
    entries: list[str] = []
    for entry in sorted(path.rglob("*")):
        rel = entry.relative_to(path)
        kind = "/" if entry.is_dir() else ""
        try:
            size = entry.stat().st_size
            entries.append(f"{rel}{kind} size={size}")
        except OSError as exc:
            entries.append(f"{rel}{kind} stat_error={exc}")
        if len(entries) >= max_entries:
            break
    if not entries:
        return "(empty directory)"
    if len(entries) >= max_entries:
        entries.append("... truncated ...")
    return "\n".join(entries)


def remove_tree_with_retries(path: Path, *, timeout_s: float = 15.0, interval_s: float = 0.5) -> None:
    if not path.exists():
        return
    deadline = time.time() + timeout_s
    last_error: OSError | None = None
    while time.time() < deadline:
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            time.sleep(interval_s)
    details = _describe_tree(path)
    raise RuntimeError(
        f"failed to remove tree {path} after {timeout_s:.1f}s: {last_error}\n{details}"
    )


def cleanup_dev_state() -> None:
    subprocess.run(
        ["bash", "scripts/stop_all.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        [MAKE_BIN, "down"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def start_make_target(target: str, env: dict[str, str], log_path: Path) -> RunningTarget:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [MAKE_BIN, target],
        cwd=ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return RunningTarget(target=target, process=process, log_path=log_path, log_handle=log_handle)


def stop_target(running: RunningTarget, timeout_s: float = 20.0) -> None:
    if running.process.poll() is None:
        try:
            os.killpg(running.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            running.process.terminate()
        try:
            running.process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(running.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                running.process.kill()
            running.process.wait(timeout=5)
    with suppress(Exception):
        running.log_handle.close()


def _ensure_running(running: RunningTarget) -> None:
    if running.process.poll() is not None:
        raise RuntimeError(
            f"{running.target} exited early with code {running.process.returncode}\n"
            f"{tail_log(running.log_path)}"
        )


def wait_until(
    description: str,
    predicate,
    *,
    running: RunningTarget,
    timeout_s: float = 180.0,
    interval_s: float = 1.0,
):
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        _ensure_running(running)
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:  # pragma: no cover - exercised in live smoke mode
            last_error = exc
        time.sleep(interval_s)
    details = f"{description} did not become ready within {timeout_s:.0f}s"
    if last_error is not None:
        details += f": {last_error}"
    details += f"\n{tail_log(running.log_path)}"
    raise RuntimeError(details)


def read_env_var_file(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def wait_profile_api_token(
    profile_dir: Path,
    *,
    running: RunningTarget,
    timeout_s: float = 120.0,
    prefer_admin: bool = False,
) -> str | None:
    env_paths = (profile_dir / "controller.env", profile_dir / "apishim.env")

    def _probe() -> str | None:
        for env_path in env_paths:
            token = None
            if prefer_admin:
                token = read_env_var_file(env_path, "AE_API_ADMIN_TOKEN")
            else:
                token = read_env_var_file(env_path, "AE_API_READ_TOKEN") or read_env_var_file(
                    env_path, "AE_API_ADMIN_TOKEN"
                )
            if token:
                return token
        for env_path in env_paths:
            if env_path.exists():
                return ""
        return None

    token = wait_until(
        f"profile auth env under {profile_dir}",
        _probe,
        running=running,
        timeout_s=timeout_s,
        interval_s=1.0,
    )
    return token or None


def _http_request(url: str, *, bearer_token: str | None = None) -> str:
    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    context = _TLS_CONTEXT if url.startswith("https://") else None
    with urllib.request.urlopen(request, timeout=3, context=context) as response:
        return response.read().decode("utf-8", errors="replace")


def wait_http_ok(
    url: str,
    *,
    running: RunningTarget,
    bearer_token: str | None = None,
    text_fragment: str | None = None,
    timeout_s: float = 180.0,
) -> str:
    def _probe() -> str | None:
        body = _http_request(url, bearer_token=bearer_token)
        if text_fragment and text_fragment not in body:
            return None
        return body

    return wait_until(
        f"HTTP readiness for {url}",
        _probe,
        running=running,
        timeout_s=timeout_s,
        interval_s=1.0,
    )


def wait_status_ready(
    metrics_port: int,
    app_name: str,
    *,
    running: RunningTarget,
    bearer_token: str | None = None,
    desired_replicas: int = 1,
    timeout_s: float = 180.0,
) -> dict[str, object]:
    url = f"http://127.0.0.1:{metrics_port}/status/{app_name}"

    def _probe() -> dict[str, object] | None:
        try:
            payload = json.loads(_http_request(url, bearer_token=bearer_token))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        ready = int(payload.get("ready_replicas", 0) or 0)
        desired = int(payload.get("desired_replicas", 0) or 0)
        if ready >= desired_replicas and desired >= desired_replicas:
            return payload
        return None

    return wait_until(
        f"ready status for app {app_name}",
        _probe,
        running=running,
        timeout_s=timeout_s,
        interval_s=2.0,
    )


def wait_file(path: Path, *, running: RunningTarget, timeout_s: float = 60.0) -> Path:
    return wait_until(
        f"file {path}",
        lambda: path if path.exists() else None,
        running=running,
        timeout_s=timeout_s,
        interval_s=1.0,
    )


def wait_tls_probe(
    probe: TlsProbe,
    *,
    https_port: int,
    running: RunningTarget,
    timeout_s: float = 180.0,
) -> str:
    if not CURL_BIN:
        raise RuntimeError("curl is required for TLS host probes")

    url = f"https://{probe.host}:{https_port}{probe.path}"

    def _probe() -> str | None:
        result = subprocess.run(
            [
                CURL_BIN,
                "-skL",
                "--resolve",
                f"{probe.host}:{https_port}:127.0.0.1",
                url,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        if probe.text_fragment and probe.text_fragment not in result.stdout:
            return None
        return result.stdout

    return wait_until(
        f"TLS probe for {url}",
        _probe,
        running=running,
        timeout_s=timeout_s,
        interval_s=2.0,
    )


def apply_manifest(
    manifest: Path,
    *,
    server_base: str,
    bearer_token: str | None,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    token_args = [f"--token={bearer_token}"] if bearer_token else []
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ae.cli",
            "--server",
            server_base,
            *token_args,
            "apply",
            "-f",
            str(manifest),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _normalize_registry_host(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("http://"):
        text = text[len("http://") :]
    elif text.startswith("https://"):
        text = text[len("https://") :]
    return text.split("/", 1)[0].strip()


def resolve_http_smoke_image(*, strict_cri: bool = False) -> str:
    image = "docker.io/library/python:3.11-alpine"
    if not strict_cri:
        return image

    mode = str(os.getenv("AE_CRI_REGISTRY_MODE", "")).strip().lower()
    if mode == "off":
        return image

    default_registry = f"localhost:{os.getenv('AE_CRI_MANAGED_REGISTRY_PORT', '5001')}"
    registry = _normalize_registry_host(
        str(os.getenv("AE_CRI_REGISTRY") or os.getenv("AE_REGISTRY_HOST") or default_registry)
    )
    if not registry:
        return image

    ref = image
    digest = ""
    if "@" in ref:
        ref, digest_part = ref.split("@", 1)
        digest = f"@{digest_part}"

    tag = ""
    last_segment = ref.rsplit("/", 1)[-1]
    if ":" in last_segment:
        ref, tag_part = ref.rsplit(":", 1)
        tag = f":{tag_part}"

    first = ref.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        parts = ref.split("/", 1)
        if len(parts) == 2:
            ref = parts[1]

    namespace = str(os.getenv("AE_CRI_REGISTRY_NAMESPACE", "")).strip().strip("/")
    if namespace:
        ref = f"{namespace}/{ref.lstrip('/')}"

    return f"{registry.rstrip('/')}/{ref.lstrip('/')}{tag}{digest}"


def write_http_smoke_manifest(
    path: Path,
    *,
    app_name: str,
    port: int,
    strict_cri: bool = False,
) -> Path:
    image = resolve_http_smoke_image(strict_cri=strict_cri)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dedent(
            f"""\
            apiVersion: ae.dev/v1alpha1
            kind: Deployment
            metadata:
              name: {app_name}
            spec:
              image: {image}
              replicas: 1
              command:
                - sh
                - -lc
                - |
                  python - <<'PY'
                  from http.server import BaseHTTPRequestHandler, HTTPServer
                  import os

                  port = int(os.environ["PORT"])

                  class Handler(BaseHTTPRequestHandler):
                      def do_GET(self):
                          if self.path == "/healthz":
                              body = b"ok\\n"
                          else:
                              body = b"profile smoke\\n"
                          self.send_response(200)
                          self.send_header("Content-Type", "text/plain; charset=utf-8")
                          self.send_header("Content-Length", str(len(body)))
                          self.end_headers()
                          self.wfile.write(body)

                      def log_message(self, fmt, *args):
                          return

                  HTTPServer(("0.0.0.0", port), Handler).serve_forever()
                  PY
              env:
                - name: PORT
                  value: "{port}"
              ports:
                - name: http
                  containerPort: {port}
              service:
                port: {port}
                targetPort: {port}
              health:
                readiness:
                  httpGet:
                    path: /healthz
                    port: {port}
                  initialDelaySeconds: 1
                  timeoutSeconds: 1
                  periodSeconds: 2
                  successThreshold: 1
                  failureThreshold: 10
                liveness:
                  httpGet:
                    path: /healthz
                    port: {port}
                  initialDelaySeconds: 3
                  timeoutSeconds: 1
                  periodSeconds: 5
                  successThreshold: 1
                  failureThreshold: 3
                startup:
                  httpGet:
                    path: /healthz
                    port: {port}
                  failureThreshold: 15
                  periodSeconds: 2
            """
        ),
        encoding="utf-8",
    )
    return path
