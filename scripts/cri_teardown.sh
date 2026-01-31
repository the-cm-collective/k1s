#!/usr/bin/env bash
set -euo pipefail

log() { printf '\033[1;31m[cri-teardown]\033[0m %s\n' "$1"; }

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
label_key="${AE_CRI_APP_LABEL:-ae.app}"
crictl_bin="${CRICTL_BIN:-crictl}"

if ! command -v "$crictl_bin" >/dev/null 2>&1; then
  log "crictl not found; skipping CRI cleanup"
  exit 0
fi

crictl_cmd=("$crictl_bin" --runtime-endpoint "$endpoint")
if ! "${crictl_cmd[@]}" info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    crictl_cmd=(sudo -n "$crictl_bin" --runtime-endpoint "$endpoint")
    if ! "${crictl_cmd[@]}" info >/dev/null 2>&1; then
      log "cannot access CRI endpoint $endpoint; skipping"
      exit 0
    fi
  else
    log "cannot access CRI endpoint $endpoint; skipping"
    exit 0
  fi
fi

pods_json="$("${crictl_cmd[@]}" pods -o json 2>/dev/null || true)"
if [[ -z "${pods_json}" ]]; then
  log "no CRI pods output; skipping"
  exit 0
fi

pod_ids=$(
  printf '%s' "$pods_json" | LABEL_KEY="$label_key" python - <<'PY'
import json
import os
import sys

label_key = os.environ.get("LABEL_KEY", "ae.app")
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)

items = data.get("items") or []
ids = []
for item in items:
    if not isinstance(item, dict):
        continue
    labels = item.get("labels") or {}
    meta = item.get("metadata") or {}
    if isinstance(meta, dict) and isinstance(meta.get("labels"), dict):
        labels = {**labels, **meta.get("labels")}
    if label_key in labels:
        pod_id = item.get("id") or item.get("podSandboxId") or item.get("pod_sandbox_id")
        if pod_id:
            ids.append(str(pod_id))
print(" ".join(ids))
PY
)

if [[ -z "${pod_ids}" ]]; then
  log "no CRI pods with label ${label_key}"
  exit 0
fi

log "Removing CRI pods with label ${label_key} (${pod_ids})"
for pod_id in ${pod_ids}; do
  "${crictl_cmd[@]}" stopp "${pod_id}" >/dev/null 2>&1 || true
  "${crictl_cmd[@]}" rmp "${pod_id}" >/dev/null 2>&1 || true
done

log "CRI cleanup complete"
