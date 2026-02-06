# Runtime Profiles

k1s ships lightweight runtime profiles so you can run the control plane without the demo apps. Each profile uses an empty specs directory under `state/profiles/<profile>/specs`, so nothing is reconciled until you apply a manifest.

## Quickstart

`dev-min` (SQLite + HTTP, local loop)
```
make dev-min
```

`dev-etcd` (etcd + HTTP; auto-starts etcd)
```
make dev-etcd
```

`k1s-core` (etcd + NATS + JetStream; auto-starts etcd + hub NATS)
```
make k1s-core
```

`k1s-edge` (edge gateway + stub worker; NATS core by default)
```
make k1s-edge
```

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

Force the container engine:
```
AE_CONTAINER_CLI=podman make k1s-core
```

## Apply a sample (any profile)

```
python -m ae.cli apply -f specs/examples/echo.yaml
```

## Stop everything

```
make down
```
