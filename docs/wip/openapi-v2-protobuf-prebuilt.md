# Prebuilt OpenAPI v2 Protobuf (Option 2)

Status: WIP (design note)

## Problem
`kubectl apply` fetches `/openapi/v2` and prefers protobuf (`Accept: application/com.github.proto-openapi.spec.v2@v1.0+protobuf`).
Our shim currently serves JSON for `/openapi/v2`, which causes:
`proto: cannot parse invalid wire-format data` during schema validation.

## Option 2: Pre-generate a static protobuf blob
Instead of converting on every request, we can pre-generate a protobuf representation
of the OpenAPI v2 document and serve the binary for `/openapi/v2` when a protobuf
`Accept` header is present.

### High-level plan
1) Generate OpenAPI v2 JSON from the shim implementation.
2) Convert that JSON to protobuf once during build/CI.
3) Store the protobuf blob in-repo (or embed as a base64 string).
4) Serve the protobuf bytes for `/openapi/v2` with the correct content type.
5) Keep `/swagger.json` JSON-only for human inspection.

### Proposed file layout
- `docs/openapi/openapi-v2.json` (generated, optional; can be gitignored)
- `src/ae/apishim/openapi_v2.pb` (binary, checked in)
- `scripts/openapi/build_openapi_v2_proto.sh` (optional helper script)

### Generation path (example)
1) Export JSON:
   - Add a tiny helper script or CLI flag that dumps `_swagger_doc()`.
   - Example Python snippet for CI:
     ```python
     import json
     from ae.apishim.server import _swagger_doc
     print(json.dumps(_swagger_doc()))
     ```
2) Convert JSON to protobuf:
   - Use a tool that outputs the Kubernetes OpenAPI v2 protobuf.
   - Candidates (examples):
     - `gnostic` (OpenAPI -> protobuf)
     - `kube-openapi/cmd/openapi2proto`
   - Pin tool versions to avoid drift.
   - Prefer running the tool in CI to avoid local developer deps.
3) Commit the resulting `openapi_v2.pb`.

### Runtime behavior
- On `/openapi/v2`:
  - If client `Accept` includes the protobuf media type, return the raw bytes.
  - Otherwise return JSON (current behavior).
- Set:
  - `Content-Type: application/com.github.proto-openapi.spec.v2@v1.0+protobuf`
  - `Vary: Accept`
  - Optional: `Content-Encoding: gzip` if we store gzipped bytes.

### Implementation sketch (server)
- Add a helper to read the protobuf bytes:
  - `src/ae/apishim/openapi_v2.pb` or an embedded base64 string.
- In the request handler for `/openapi/v2`:
  - Inspect `Accept` header for `application/com.github.proto-openapi.spec.v2@v1.0+protobuf`.
  - If present, send protobuf bytes with the content type above.
  - Else return `_swagger_doc()` JSON (current behavior).

### Pros
- Matches kube-apiserver behavior for kubectl validation.
- No runtime conversion overhead.
- Deterministic (stable bytes per release).

### Cons
- Requires a generation step and pinned tooling.
- Must keep blob in sync with swagger JSON.
- Slightly larger repo footprint (binary artifact).

### Guardrails to prevent drift
- Add a CI check that regenerates the protobuf and fails on diff.
- Store a digest (sha256) and compare at runtime or in tests.
- Keep a small README next to the blob explaining regeneration.

### Notes
- This keeps validation behavior consistent with real kube-apiserver.
- It is OK to keep JSON at `/swagger.json` for debugging; kubectl uses `/openapi/v2`.

## Short-term mitigation
Until protobuf support lands, use `kubectl apply --validate=false` in shim-backed
workflows to avoid the parse error.
