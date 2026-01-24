#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

APISHIM_HOST="${APISHIM_HOST:-127.0.0.1}"
APISHIM_PORT="${APISHIM_PORT:-8445}"
APISHIM_BASE="${APISHIM_BASE:-http://${APISHIM_HOST}:${APISHIM_PORT}}"
APISHIM_LOG="${APISHIM_LOG:-/tmp/apishim-ws-smoke.log}"
APISHIM_SKIP_START="${APISHIM_SKIP_START:-0}"
PF_RAW_DUMP="${PF_RAW_DUMP:-0}"
PF_RAW_PATH="${PF_RAW_PATH:-/tmp/apishim-pf-raw.bin}"
PF_JS="${PF_JS:-0}"
PF_JS_TIMEOUT_MS="${PF_JS_TIMEOUT_MS:-5000}"

TOKEN_ADMIN="${AE_APISHIM_TOKEN:-admin}"
TOKEN_EXEC="${AE_APISHIM_EXEC_TOKEN:-exec}"
TOKEN_PF="${AE_APISHIM_PORTFORWARD_TOKEN:-pf}"
RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-docker}"

APP_NAME=""
APISHIM_PID=""
POD_NAME=""

resolve_pod_name() {
  local cid=""
  local rid=""
  if [[ "${RUNTIME_BACKEND}" == "docker" ]] && command -v docker >/dev/null 2>&1; then
    cid=$(docker ps -q --filter "label=ae.app=${APP_NAME}" | head -n 1 || true)
    if [[ -n "${cid}" ]]; then
      rid=$(docker inspect -f '{{ index .Config.Labels "ae.replica_id" }}' "${cid}" 2>/dev/null || true)
    fi
  elif [[ "${RUNTIME_BACKEND}" == "podman" ]] && command -v podman >/dev/null 2>&1; then
    cid=$(podman ps -q --filter "label=ae.app=${APP_NAME}" | head -n 1 || true)
    if [[ -n "${cid}" ]]; then
      rid=$(podman inspect -f '{{ index .Config.Labels "ae.replica_id" }}' "${cid}" 2>/dev/null || true)
    fi
  fi
  if [[ -n "${rid}" ]]; then
    POD_NAME="${rid}"
    return 0
  fi
  return 1
}

cleanup() {
  if [[ -n "${APP_NAME}" ]]; then
    curl -s -o /dev/null -X DELETE "${APISHIM_BASE}/apis/apps/v1/namespaces/default/deployments/${APP_NAME}" \
      -H "Authorization: Bearer ${TOKEN_ADMIN}" || true
    curl -s -o /dev/null -X DELETE "${APISHIM_BASE}/api/v1/namespaces/default/services/${APP_NAME}" \
      -H "Authorization: Bearer ${TOKEN_ADMIN}" || true
    if command -v docker >/dev/null 2>&1; then
      docker ps -aq --filter "label=ae.app=${APP_NAME}" | xargs -r docker rm -f >/dev/null 2>&1 || true
    fi
    if command -v podman >/dev/null 2>&1; then
      podman ps -aq --filter "label=ae.app=${APP_NAME}" | xargs -r podman rm -f >/dev/null 2>&1 || true
    fi
  fi
  if [[ -n "${APISHIM_PID}" ]]; then
    kill "${APISHIM_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "${APISHIM_SKIP_START}" != "1" ]]; then
  echo "Starting apishim at ${APISHIM_BASE} (log: ${APISHIM_LOG})"
  PYTHONPATH="${ROOT_DIR}/src" \
    AE_APISHIM_ENABLE=1 \
    AE_APISHIM_TOKEN="${TOKEN_ADMIN}" \
    AE_APISHIM_EXEC_TOKEN="${TOKEN_EXEC}" \
    AE_APISHIM_PORTFORWARD_TOKEN="${TOKEN_PF}" \
    AE_RUNTIME_BACKEND="${RUNTIME_BACKEND}" \
    python -m ae.apishim serve --host "${APISHIM_HOST}" --port "${APISHIM_PORT}" \
      >"${APISHIM_LOG}" 2>&1 &
  APISHIM_PID=$!
fi

ready=0
for _ in $(seq 1 40); do
  if curl -s -H "Authorization: Bearer ${TOKEN_ADMIN}" "${APISHIM_BASE}/healthz" >/dev/null; then
    ready=1
    break
  fi
  sleep 0.25
done
if [[ "${ready}" != "1" ]]; then
  echo "apishim not ready at ${APISHIM_BASE}" >&2
  exit 1
fi

APP_NAME="ws-smoke-$(date +%s)"

cat <<EOF_JSON | curl -s -o /dev/null -w "%{http_code}" -X POST "${APISHIM_BASE}/apis/apps/v1/namespaces/default/deployments" \
  -H "Authorization: Bearer ${TOKEN_ADMIN}" -H "Content-Type: application/json" --data-binary @- >/tmp/apishim_ws_dep_status
{
  "apiVersion": "apps/v1",
  "kind": "Deployment",
  "metadata": {"name": "${APP_NAME}", "namespace": "default"},
  "spec": {
    "replicas": 1,
    "selector": {"matchLabels": {"app": "${APP_NAME}"}},
    "template": {
      "metadata": {"labels": {"app": "${APP_NAME}"}},
      "spec": {
        "containers": [
          {
            "name": "echo",
            "image": "mendhak/http-https-echo:37",
            "ports": [{"containerPort": 8080}]
          }
        ]
      }
    }
  }
}
EOF_JSON

cat <<EOF_JSON | curl -s -o /dev/null -w "%{http_code}" -X POST "${APISHIM_BASE}/api/v1/namespaces/default/services" \
  -H "Authorization: Bearer ${TOKEN_ADMIN}" -H "Content-Type: application/json" --data-binary @- >/tmp/apishim_ws_svc_status
{
  "apiVersion": "v1",
  "kind": "Service",
  "metadata": {"name": "${APP_NAME}", "namespace": "default"},
  "spec": {
    "selector": {"app": "${APP_NAME}"},
    "ports": [{"name": "http", "port": 8080, "targetPort": 8080}]
  }
}
EOF_JSON

POD_NAME=$(PYTHONPATH="${ROOT_DIR}/src" APP_NAME="${APP_NAME}" APISHIM_BASE="${APISHIM_BASE}" TOKEN_ADMIN="${TOKEN_ADMIN}" \
  python - <<'PY'
import json
import os
import time
import urllib.request

app = os.environ["APP_NAME"]
base = os.environ["APISHIM_BASE"].rstrip("/")
token = os.environ["TOKEN_ADMIN"]

pod_name = None
for _ in range(40):
    req = urllib.request.Request(
        f"{base}/api/v1/namespaces/default/pods",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as f:
        data = json.load(f)
    for item in data.get("items", []):
        labels = (item.get("metadata") or {}).get("labels") or {}
        if labels.get("ae.app") == app:
            phase = (item.get("status") or {}).get("phase")
            if phase == "Running":
                pod_name = item["metadata"]["name"]
                break
    if pod_name:
        break
    time.sleep(0.5)

if not pod_name:
    raise SystemExit("pod not ready")
print(pod_name)
PY
)

runtime_ready=0
for _ in $(seq 1 80); do
  if resolve_pod_name; then
    runtime_ready=1
    break
  fi
  sleep 0.25
done
if [[ "${runtime_ready}" != "1" ]]; then
  echo "runtime container not ready for ${APP_NAME}" >&2
  exit 1
fi

echo "Pod: ${POD_NAME}"

ready=0
if [[ "${RUNTIME_BACKEND}" == "docker" ]] && command -v docker >/dev/null 2>&1; then
  for _ in $(seq 1 20); do
    if docker ps -q --filter "label=ae.replica_id=${POD_NAME}" | grep -q .; then
      ready=1
      break
    fi
    sleep 0.25
  done
elif [[ "${RUNTIME_BACKEND}" == "podman" ]] && command -v podman >/dev/null 2>&1; then
  for _ in $(seq 1 20); do
    if podman ps -q --filter "label=ae.replica_id=${POD_NAME}" | grep -q .; then
      ready=1
      break
    fi
    sleep 0.25
  done
fi
if [[ "${ready}" != "1" ]]; then
  echo "runtime container not ready for ${POD_NAME}" >&2
  exit 1
fi

sleep 0.5

exec_rc=0
echo "WS exec test..."
for _ in $(seq 1 3); do
  resolve_pod_name || true
  if PYTHONPATH="${ROOT_DIR}/src" APISHIM_BASE="${APISHIM_BASE}" POD_NAME="${POD_NAME}" TOKEN_EXEC="${TOKEN_EXEC}" \
    python - <<'PY'
import os
from ae.cli.__main__ import _exec_over_ws

base = os.environ["APISHIM_BASE"]
pod_name = os.environ["POD_NAME"]
token = os.environ["TOKEN_EXEC"]

code = _exec_over_ws(
    base,
    namespace="default",
    pod_name=pod_name,
    command=["/bin/sh", "-c", "echo ws-exec-ok"],
    container=None,
    stdin=False,
    stdout=True,
    stderr=True,
    tty=False,
    token=token,
    timeout=10,
)
print(f"exit={code}")
PY
  then
    exec_rc=0
    break
  else
    exec_rc=1
    sleep 0.5
  fi
done

pf_rc=0
echo "WS port-forward test..."
if ! PYTHONPATH="${ROOT_DIR}/src" APISHIM_BASE="${APISHIM_BASE}" POD_NAME="${POD_NAME}" TOKEN_PF="${TOKEN_PF}" PF_RAW_DUMP="${PF_RAW_DUMP}" PF_RAW_PATH="${PF_RAW_PATH}" \
  python - <<'PY'
import base64
import os
import socket
import urllib.parse

base = os.environ["APISHIM_BASE"]
namespace = "default"
pod_name = os.environ["POD_NAME"]
port = 8080

token = os.environ["TOKEN_PF"]
raw_dump = os.environ.get("PF_RAW_DUMP", "0") == "1"
raw_path = os.environ.get("PF_RAW_PATH", "/tmp/apishim-pf-raw.bin")
raw = bytearray()

parsed = urllib.parse.urlparse(base)
host = parsed.hostname or ""
port_num = parsed.port or 80
path = f"/api/v1/namespaces/{namespace}/pods/{pod_name}/portforward"
query = urllib.parse.urlencode({"ports": str(port)})
full_path = path + ("?" + query if query else "")

sock = socket.create_connection((host, port_num), timeout=10)
ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
req_lines = [
    f"GET {full_path} HTTP/1.1",
    f"Host: {host}:{port_num}",
    "Upgrade: websocket",
    "Connection: Upgrade",
    "Sec-WebSocket-Version: 13",
    f"Sec-WebSocket-Key: {ws_key}",
    "Sec-WebSocket-Protocol: portforward.k8s.io",
]
if token:
    req_lines.append(f"Authorization: Bearer {token}")
req_lines.append("\r\n")
sock.sendall(("\r\n".join(req_lines)).encode("utf-8"))

try:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        if raw_dump:
            raw.extend(chunk)
        buf += chunk
    header, _, extra = buf.partition(b"\r\n\r\n")
    status_line = header.split(b"\r\n", 1)[0]
    status_code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
    if status_code != 101:
        raise SystemExit(f"handshake failed: {status_code}\n{header.decode('utf-8','ignore')}")

    req = b"GET / HTTP/1.1\r\nHost: echo\r\nConnection: close\r\n\r\n"
    payload = bytes([0]) + req
    mask_key = os.urandom(4)
    header = bytearray()
    header.append(0x82)
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask_key + masked)

    sock.settimeout(5)
    left = bytearray(extra)

    def recv_exact(n):
        while len(left) < n:
            try:
                chunk = sock.recv(n - len(left))
            except TimeoutError:
                return None
            if not chunk:
                return None
            if raw_dump:
                raw.extend(chunk)
            left.extend(chunk)
        out = bytes(left[:n])
        del left[:n]
        return out

    resp = b""
    for _ in range(10):
        hdr = recv_exact(2)
        if not hdr:
            break
        opcode = hdr[0] & 0x0F
        masked = bool(hdr[1] & 0x80)
        length = hdr[1] & 0x7F
        if length == 126:
            ext = recv_exact(2)
            if ext is None:
                break
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = recv_exact(8)
            if ext is None:
                break
            length = int.from_bytes(ext, "big")
        mask = recv_exact(4) if masked else b""
        payload = recv_exact(length) if length else b""
        if payload is None:
            break
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            break
        if payload and payload[0] == 0:
            resp += payload[1:]

    sock.close()
    if not resp:
        raise SystemExit("no response data received over port-forward")
    print(resp[:200].decode("utf-8", "ignore"))
finally:
    if raw_dump:
        try:
            with open(raw_path, "wb") as fh:
                fh.write(raw)
            print(f"raw-bytes={len(raw)} path={raw_path}")
        except Exception as exc:
            print(f"raw-dump-failed: {exc}")
PY
then
  pf_rc=1
fi

js_rc=0
if [[ "${PF_JS}" == "1" ]]; then
  echo "JS port-forward test..."
  if ! APISHIM_BASE="${APISHIM_BASE}" POD_NAME="${POD_NAME}" TOKEN_PF="${TOKEN_PF}" PF_TIMEOUT_MS="${PF_JS_TIMEOUT_MS}" \
    node "${ROOT_DIR}/scripts/dev/apishim_pf_ws_client.js"; then
    js_rc=1
  fi
fi

if [[ "${exec_rc}" != "0" || "${pf_rc}" != "0" || "${js_rc}" != "0" ]]; then
  exit 1
fi
