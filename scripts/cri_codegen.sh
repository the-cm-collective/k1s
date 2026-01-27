#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT/src/ae/runtime/cri/api/runtime/v1"
PROTO_DIR="$ROOT/.cache/cri-api"
PROTO_URL="https://raw.githubusercontent.com/kubernetes/cri-api/release-1.26/pkg/apis/runtime/v1/api.proto"
GOGO_URL="https://raw.githubusercontent.com/gogo/protobuf/master/gogoproto/gogo.proto"

mkdir -p "$PROTO_DIR" "$OUT_DIR"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required to fetch the CRI proto" >&2
  exit 1
fi

curl -fsSL "$PROTO_URL" -o "$PROTO_DIR/api.proto"
mkdir -p "$PROTO_DIR/github.com/gogo/protobuf/gogoproto"
curl -fsSL "$GOGO_URL" -o "$PROTO_DIR/github.com/gogo/protobuf/gogoproto/gogo.proto"

python - <<'PY'
import importlib.util
import sys
spec = importlib.util.find_spec("grpc_tools")
if spec is None:
    print("grpcio-tools is required; run: python -m pip install grpcio-tools", file=sys.stderr)
    sys.exit(1)
PY

python -m grpc_tools.protoc \
  -I"$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_DIR/api.proto"

# Generate gogo_pb2 into src/ so the module path github.com.gogo.protobuf.gogoproto resolves.
python -m grpc_tools.protoc \
  -I"$PROTO_DIR" \
  --python_out="$ROOT/src" \
  "$PROTO_DIR/github.com/gogo/protobuf/gogoproto/gogo.proto"

# Ensure python packages exist for github.com.gogo.protobuf.gogoproto
for pkg in \
  "$ROOT/src/github" \
  "$ROOT/src/github/com" \
  "$ROOT/src/github/com/gogo" \
  "$ROOT/src/github/com/gogo/protobuf" \
  "$ROOT/src/github/com/gogo/protobuf/gogoproto"; do
  mkdir -p "$pkg"
  if [ ! -f "$pkg/__init__.py" ]; then
    : > "$pkg/__init__.py"
  fi
done

# Fix imports to keep package names stable for in-repo usage
sed -i.bak "s/^import api_pb2 as/from . import api_pb2 as/" "$OUT_DIR"/api_pb2_grpc.py
rm -f "$OUT_DIR"/api_pb2_grpc.py.bak

echo "CRI stubs generated in $OUT_DIR"
