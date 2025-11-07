#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." &>/dev/null && pwd)
cd "$ROOT_DIR"

export AE_RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-docker}"

echo "[e2e-plan] Bootstrapping demo (controller + API)"
./scripts/init_demo.sh --docs-only -y

API_BASE="http://127.0.0.1:${API_PORT:-9108}"
echo "[e2e-plan] Waiting for API at ${API_BASE}"
for i in {1..40}; do
  if curl -fsS "${API_BASE}/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
curl -fsS "${API_BASE}/health" >/dev/null

echo "[e2e-plan] Validating sample manifests with /plan"
SAMPLES=(
  "specs/examples/echo.yaml"
  "specs/examples/multi-replica-echo.yaml"
  "specs/examples/echo-multiport.yaml"
  "specs/examples/echo-sec-adv.yaml"
)

python - <<'PY'
import json, sys, yaml, urllib.request
API = f"http://127.0.0.1:{int(__import__('os').environ.get('API_PORT', '9108'))}/plan"
samples = sys.argv[1:]
failed = 0
for path in samples:
    data = yaml.safe_load(open(path, 'r', encoding='utf-8'))
    req = urllib.request.Request(API, data=json.dumps(data).encode('utf-8'), headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        print(f"[e2e-plan] ERROR: request failed for {path}: {exc}")
        failed += 1
        continue
    ok = bool(out.get('ok'))
    warns = out.get('warnings') or []
    print(f"[e2e-plan] {path}: ok={ok} warnings={len(warns)}")
    if not ok:
        print(json.dumps(out, indent=2))
        failed += 1
sys.exit(1 if failed else 0)
PY
"${SAMPLES[@]}"

echo "[e2e-plan] OK: all plans returned ok=true"
