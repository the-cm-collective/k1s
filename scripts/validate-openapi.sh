#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/docs/openapi"
TMP_DIR="$(mktemp -d)"
PYTHONPATH="$REPO_ROOT/src"

gen() {
  local target="$1"
  local expr="$2"
  PYTHONPATH="$PYTHONPATH" python - <<'PY' "$target" "$expr"
import json
import sys
from importlib import import_module

target = sys.argv[1]
expr = sys.argv[2]
mod = import_module("ae.apishim.server")
doc = getattr(mod, expr)()
with open(target, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, sort_keys=True)
PY
}

gen "$TMP_DIR/openapi-v2.json" "_swagger_doc"
gen "$TMP_DIR/openapi-v3.json" "_openapi_v3_stub"

fail=0
for f in openapi-v2.json openapi-v3.json; do
  ref="$OUT_DIR/$f"
  new="$TMP_DIR/$f"
  if ! cmp -s "$ref" "$new"; then
    echo "::error file=$ref::OpenAPI drift detected for $f" >&2
    diff -u "$ref" "$new" || true
    fail=1
  fi
done

rm -rf "$TMP_DIR"
exit $fail
