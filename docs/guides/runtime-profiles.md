# Runtime Profiles

k1s ships lightweight runtime profiles so you can run the control plane without the demo apps. Each profile uses an empty specs directory under `state/profiles/<profile>/specs`, so nothing is reconciled until you apply a manifest.

## Quickstart

Strict CRI (recommended path for containerd lanes)
```
make k1s-core-cri
make k1s-edge-cri
make k1s-core-edge-cri
make k1s-edge-core-cri
make edge-site-cri SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```
Notes:
- These aliases set `AE_RUNTIME_BACKEND=cri` and `AE_INFRA_BACKEND=cri`.
- Use `sudo -E` when your containerd environment requires elevated privileges.
- See `docs/reference/cri-containerd.md` for registry-first image prep and trust setup.

`dev-min` (SQLite + HTTP, local loop)
```
make dev-min
```

`dev-etcd` (etcd + HTTP; auto-starts etcd)
```
make dev-etcd
```
Notes:
- `dev-min`/`dev-etcd` default to SQLite for apishim (no `AE_APISHIM_DSN` set unless you provide it).

`k1s-core` (etcd + NATS + JetStream; auto-starts etcd + hub NATS)
```
make k1s-core
```
Notes:
- `k1s-core` starts etcd + hub NATS + Postgres infra and sets `AE_APISHIM_DSN` by default.
- Infra backend auto-selection:
  - `AE_RUNTIME_BACKEND=cri|containerd` -> strict CRI infra (`AE_INFRA_BACKEND=cri`)
  - otherwise -> compose infra (`AE_INFRA_BACKEND=compose`)
  - force legacy compose with `AE_INFRA_BACKEND=compose`
- Strict CRI runtime handler defaults to `runc` (`AE_CRI_RUNTIME_HANDLER=runc`).
  Override only if your containerd CRI runtime handler differs.
- DSN selection is mode-aware:
  - `AE_APISHIM_MODE=host`: `postgresql://shim:shim@127.0.0.1:<port>/shim`
  - `AE_APISHIM_MODE=cri`: `postgresql://shim:shim@127.0.0.1:<port>/shim`
  - container mode: uses the compose service name: `postgresql://shim:shim@postgres:5432/shim`
- In strict CRI `k1s-core`, default `AE_APISHIM_MODE=cri` starts apishim as CRI pod
  `k1s-core-apishim` (controller remains host-process).
- Missing-image behavior in strict CRI:
  - default interactive policy: `AE_CRI_IMAGE_POLICY=prompt`
  - non-interactive/CI default: `AE_CRI_IMAGE_POLICY=fail`
  - explicit pull-only mode: `AE_CRI_IMAGE_POLICY=pull`
  - strict fallback action `b`:
    - apishim image (`k1s-core-apishim`): local build + registry push + CRI pull verify
    - other strict-CRI managed images (etcd/nats/postgres/envoy/rathole/caddy): mirror source image into `AE_CRI_REGISTRY` + CRI pull verify
  - local build backend order: `nerdctl`, then `podman`, then `docker`
  - optional override: `AE_CRI_LOCAL_BUILD_BACKEND=nerdctl|podman|docker`
- Registry mapping in strict CRI:
  - `AE_CRI_REGISTRY=<host:port>` rewrites CRI-managed image refs to that registry
  - optional path prefix: `AE_CRI_REGISTRY_NAMESPACE=<prefix>`
  - if unset, `AE_REGISTRY_HOST` is used when provided
- Strict CRI registry mode:
  - `AE_CRI_REGISTRY_MODE=managed|external|off` (default: `managed` when no registry is set; `external` when `AE_CRI_REGISTRY` is set)
  - `managed` default endpoint: `localhost:5001` (k1s-managed local registry)
  - `external` common microk8s endpoint: `localhost:32000`
  - optional preset: `AE_CRI_REGISTRY_PRESET=microk8s|local`
- Optional containerd trust hook for strict CRI startup:
  - set `AE_CRI_REGISTRY_TRUST=1` (or `AE_CRI_REGISTRY_INSECURE=1`) to call
    `scripts/containerd_registry_trust.sh` before CRI preflight.
- Bind Postgres to the hub WG IP with `POSTGRES_BIND_IP=<hub-wg-ip>` so edge nodes can reach it.
- Override the DSN with `AE_APISHIM_DSN=postgresql://user:pass@host:5432/dbname` if needed.

`k1s-edge` (edge gateway + stub worker; NATS core by default)
```
make k1s-edge
```
Notes:
- `k1s-edge` does not start Postgres by default; edge nodes typically point at the core Postgres over WG/LAN.
- Start a local Postgres on the edge only if needed with `EDGE_START_POSTGRES=1` (also set `POSTGRES_PORT` to avoid conflicts when core and edge share a host).
- With strict CRI infra (`AE_INFRA_BACKEND=cri`), edge NATS and rathole components run as CRI pods.

## Ingress modes (k1s-core / k1s-edge)

Set the ingress mode with `EDGE_INGRESS_MODE`:
- `core-proxy` (default): core ingress proxies to edge over rathole.
- `core-to-edge-public`: core ingress routes directly to edge public endpoints.
- `edge-local`: edge ingress only; core ingress is not in the request path.

Examples:
```
EDGE_INGRESS_MODE=core-to-edge-public make k1s-core
EDGE_INGRESS_MODE=edge-local make k1s-edge
```

By default, `make k1s-core` starts Envoy (core ingress) and rathole server, and
`make k1s-edge` starts a rathole client when `EDGE_INGRESS_MODE=core-proxy`.
Override images with `AE_ENVOY_IMAGE` / `AE_RATHOLE_IMAGE`, or disable auto-start
with `EDGE_INGRESS_START=0`.

Mode behavior:
- `core-proxy`: Envoy + rathole server/client.
- `core-to-edge-public`: Envoy only (no rathole).
- `edge-local`: no core ingress; the gateway renders an edge-local Caddyfile if enabled.
- For canonical WG/Rosenpass multi-site patterns (production + single-host), see Option D in `docs/guides/multinode-lab.md`.

Ingress deep validation defaults (CRI lanes):
- Primary lane: `core-proxy` deep/deep+perf (`lb-proof-scope=auto`) for policy + observability checks.
- Strict LB proof lane: `edge-local` deep (`lb-proof-scope=edge-only`) for distribution proof.
- Canonical command sequence: `docs/guides/ingress-capability-test-sequence.md`.

## Core + Edge pairings

JetStream path (k1s-core + edge running JetStream work queue):
```
# Terminal 1
make k1s-core

# Terminal 2
make k1s-edge-core
```

Core-NATS path (k1s-edge work.pull without JetStream):
```
# Terminal 1
make k1s-core-edge

# Terminal 2
make k1s-edge
```

## Multiple edges

Run additional edges in new terminals with unique node IDs:
```
AE_NODE_ID=edge-node-2 make k1s-edge
```

For multi-site tests, also override site IDs:
```
AE_SITE_ID=sea-edge-02 AE_NODE_ID=edge-node-2 make k1s-edge
```

## Optional overrides

Disable the stub worker (gateway only):
```
EDGE_START_WORKER=0 make k1s-edge
```

Bring up the docs server alongside k1s-core:
```
CORE_DOCS=1 make k1s-core
```

Docs will serve on `http://127.0.0.1:9109` (override with `AE_DOCS_PORT` / `DOCS_BIND`),
and the dashboard remains on `http://127.0.0.1:9108/dashboard`.

Start Caddy for TLS hostnames (docs/api/dashboard) on any core profile:
```
CORE_CADDY=1 make k1s-core
CORE_CADDY=1 make dev-min
CORE_CADDY=1 make dev-etcd
```

This uses the dev Caddy config with `docs.home.arpa`, `api.home.arpa`, and
`dash.home.arpa` on port `8443` (override with `CADDY_HTTPS_PORT`).

Dev-min/dev-etcd defaults (can be disabled explicitly):
- `AE_REGISTER_LOCAL_NODE=1` (single-node scheduling)
- `AE_LABS=1` + apishim autostart
- Disable with `AE_REGISTER_LOCAL_NODE=0 AE_LABS=0 AE_APISHIM_AUTOSTART=0`

Local DNS + TLS trust helper (optional):
- `AE_DEV_LOCAL=1 make dev-min` (or `dev-etcd` / `k1s-core`) will:
  - add `docs.home.arpa`, `api.home.arpa`, `dash.home.arpa`, `echo.home.arpa` to `/etc/hosts`
  - install local Caddy/Apishim/Envoy certs into system trust (requires `update-ca-certificates`)
- `make dev-local` runs the helper on demand (useful after Caddy/Envoy have minted certs)

Aliases:
- `make k1s-core-caddy`
- `make dev-min-caddy`
- `make dev-etcd-caddy`

Force the container engine:
```
AE_CONTAINER_CLI=podman make k1s-core
```

Profiles default to Podman; override with:
```
AE_RUNTIME_BACKEND=docker make k1s-core
```

Strict CRI aliases:
```
make k1s-core-cri
make k1s-core-edge-cri
make edge-site-cri SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
make k1s-edge-cri
make k1s-edge-core-cri
```
Equivalent explicit override:
`AE_CRI_RUNTIME_HANDLER=runc make k1s-core-cri`

Notes:
- `run_profile.sh` now auto-selects CRI infra when a running `k1s-core` CRI stack is detected, and fails fast if a compose edge/core profile is invoked against that CRI core.
- `edge-site-cri` defaults the CRI edge NATS profile to `k1s-core` (JetStream path). Set `EDGE_PROFILE=k1s-edge` when pairing with `make k1s-edge-cri`.
- Strict CRI edge startup port precedence is: `EDGE_PORT`, then explicit port from `AE_NATS_URL`, then fallback `4223`.

## Apply a sample (any profile)

```
python -m ae.cli apply -f specs/examples/echo.yaml
```

## Manual smoke test (profiles)

**dev-min**
1. `CORE_DOCS=1 make dev-min` (optional TLS: `CORE_CADDY=1 make dev-min`)
2. `python -m ae.cli apply -f specs/examples/echo.yaml`
3. `python -m ae.cli status --verbose` (expect 1 ready app)
4. Open `http://127.0.0.1:9108/dashboard` and `http://127.0.0.1:9109/playground/`
5. `make down`

**dev-etcd**
1. `CORE_DOCS=1 make dev-etcd` (optional TLS: `CORE_CADDY=1 make dev-etcd`)
2. `python -m ae.cli apply -f specs/examples/echo.yaml`
3. `python -m ae.cli status --verbose` and `python -m ae.cli metrics`
4. Open `http://127.0.0.1:9108/dashboard` and `http://127.0.0.1:9109/playground/`
5. `make down`

**k1s-core + multiple edges (same host)**
1. Terminal A: `AE_DEV_LOCAL=1 make k1s-core` (optional TLS only: `CORE_CADDY=1 make k1s-core`)
2. Terminal B: `make k1s-edge` (site `sfo-edge-01`, node `edge-node-1`)
3. Terminal C: `AE_SITE_ID=sfo-edge-01 AE_NODE_ID=edge-node-2 make k1s-edge` (second node, same site)
4. Terminal D (second site on same host):
   - `make edge-site SITE_ID=sfo-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224`
   - `AE_SITE_ID=sfo-edge-02 AE_NODE_ID=edge-node-1 AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224 make k1s-edge`
   - Note: `AE_NODE_ID` must be globally unique across sites. Prefer `AE_NODE_ID=<site_id>--<node_id>` for multi-site tests.
5. In the core terminal: `source <(ae auth local)` then `ae nodes` (expect 3 nodes across 2 sites)
6. `python -m ae.cli apply -f specs/examples/echo.yaml`
7. `python -m ae.cli status --verbose` and `python -m ae.cli events echo`
8. Open `http://127.0.0.1:9108/dashboard` and `http://127.0.0.1:9109/playground/`
9. `make down`

## Stop everything

```
make down
```

<details>
<summary><strong>Ingress Envoy Test Coverage</strong></summary>

Unit test (config render only):
- `tests/unit/test_envoy_core_local_ingress.py`
- `PYTHONPATH=src pytest -q tests/unit/test_envoy_core_local_ingress.py`

Opt‑in integration test (TLS handshake):
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `AE_E2E_ENVOY_TLS=1 PYTHONPATH=src pytest -q tests/integration/test_envoy_core_local_ingress_tls.py`
- Requires `docker`/`podman` and `openssl`
- Uses the local Caddy CA if present (`state/caddy-data/.../root.crt`); otherwise generates a temporary CA.
- Envoy image can be overridden with `AE_ENVOY_IMAGE`.
</details>
