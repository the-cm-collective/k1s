#!/usr/bin/env bash
set -euo pipefail

# Remove non-current revision containers for an app (Docker or Podman).
# Heuristic: determine current revision as the highest ae.revision label
# among running containers for the app, then remove containers with lower
# revisions. This avoids requiring CLI access to the controller.
#
# Usage:
#   scripts/bench/prune_old_revisions.sh echo

app="${1:-}"
if [[ -z "$app" ]]; then
  echo "usage: $0 <app>" >&2
  exit 2
fi

bin=""
if command -v docker >/dev/null 2>&1; then
  bin=docker
elif command -v podman >/dev/null 2>&1; then
  bin=podman
else
  echo "no docker/podman found" >&2
  exit 0
fi

# Collect candidate containers with labels
ids=$($bin ps -a -q --filter "label=ae.app=$app" 2>/dev/null || true)
[[ -z "$ids" ]] && exit 0

# Build map of id -> revision
rev_list=$($bin inspect $ids 2>/dev/null | python - << 'PY'
import json,sys
import re
try:
    arr=json.load(sys.stdin)
except Exception:
    arr=[]
maxrev=-1
rows=[]
for c in arr:
    labs=(c.get('Config') or {}).get('Labels') or (c.get('Labels') or {})
    rev=labs.get('ae.revision') or labs.get('ae.revision'.replace('.',':'))
    try:
        r=int(str(rev)) if rev is not None else -1
    except Exception:
        r=-1
    if r>maxrev:
        maxrev=r
    rows.append((c.get('Id','')[:12], r))
print(maxrev)
for cid,r in rows:
    print(f"{cid} {r}")
PY
)
maxrev=$(echo "$rev_list" | head -n1)
[[ -z "$maxrev" ]] && exit 0

# Remove containers with revision < maxrev
while read -r line; do
  [[ -z "$line" ]] && continue
  cid=$(echo "$line" | awk '{print $1}')
  rv=$(echo "$line" | awk '{print $2}')
  if [[ "$rv" != "-1" && "$rv" -lt "$maxrev" ]]; then
    $bin rm -f "$cid" >/dev/null 2>&1 || true
  fi
done < <(echo "$rev_list" | tail -n +2)

exit 0

