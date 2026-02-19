# Edge Ingress (dev)

## Core-proxy render quickstart

1. Start the dev control plane (NATS + etcd):

```bash
docker compose -f ops/dev/docker-compose.nats-etcd.yaml up
```

2. Run the controller with core-proxy rendering enabled:

```bash
export AE_NATS_URL=nats://127.0.0.1:4222
export AE_EDGE_INGRESS_CORE_PROXY=1
export AE_EDGE_INGRESS_CONFIG_DIR=state/edge-ingress
export AE_EDGE_INGRESS_RELOAD_CMD="echo reload"
python -m ae.controller --loop --specs specs/
```

3. Start a gateway to trigger lease/port allocation:

```bash
export AE_NATS_URL=nats://127.0.0.1:4222
export AE_SITE_ID=sfo-edge-01
export AE_NODE_ID=gateway-01
python -m ae.gateway
```

4. Generated config files:

- `state/edge-ingress/envoy.yaml`
- `state/edge-ingress/rathole-server.toml`
- Optional per-site client configs when `AE_RATHOLE_CLIENT_DIR` is set.

## Optional env knobs

- `AE_EDGE_INGRESS_ENVOY_CONFIG` (override envoy config path)
- `AE_RATHOLE_SERVER_CONFIG` (override rathole server config path)
- `AE_RATHOLE_CLIENT_DIR` (write per-site client configs)
- `AE_RATHOLE_BIND_ADDR` (default `0.0.0.0:2333`)
- `AE_RATHOLE_DEFAULT_TOKEN` (default `dev`)
- `AE_RATHOLE_SERVER_ADDR` (client remote addr; default `127.0.0.1:2333`)
- `AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX` (default `edge.local`)
- `AE_EDGE_INGRESS_LOCAL_ADDR` (default `127.0.0.1:18081`)
