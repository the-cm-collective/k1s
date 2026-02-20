# Ingress Guide (Caddy)

## Overview

- The controller writes one Caddy site per Deployment manifest with `spec.ingress`.
- Multi-path routing is supported; Caddy uses `handle_path` for subpaths.
- TLS defaults to Caddy's internal CA in dev; you can supply real certs.
- In multi-node lanes, ingress is mode-aware (`core-proxy`, `core-to-edge-public`, `edge-local`) and is validated with mode-isolated test lanes.

## Canonical ingress modes

Set mode with `EDGE_INGRESS_MODE`:

- `core-proxy` (default): core ingress serves traffic and proxies to edge through rathole.
- `core-to-edge-public`: core ingress forwards to edge public endpoints declared via `SiteIngressEndpoint`.
- `edge-local`: edge gateway renders local ingress config; core ingress is not in the request path.

Strict edge-local bundle-endpoints lane requirements:

- `AE_ROUTE_BUNDLE_ENABLED=1`
- `AE_ENABLE_SERVICE_PROXY=1`
- `AE_SERVICE_PROVIDER=iptables`
- `AE_EDGE_LOCAL_UPSTREAM_MODE=bundle-endpoints`

See:
- `docs/guides/runtime-profiles.md`
- `docs/guides/multinode-lab.md`

## Environment knobs

- `AE_CADDY_SITES`: directory for rendered site files (default: `ops/dev/caddy/sites`).
- `AE_CADDY_BIN`: Caddy binary (default: `caddy`).
- `AE_CADDY_FILE`: optional path to a Caddyfile (container/host reload target).
- `AE_CADDY_CONTAINER`: name of the Caddy container (enables docker/podman exec reload).
- `AE_CONTAINER_CLI`: podman or docker (default: docker).
- `AE_CADDY_RELOAD_TIMEOUT`: seconds to wait on reload (default: 10).
- `AE_DISABLE_INGRESS=1`: disable ingress rendering/reload entirely.
- `AE_TLS_DIR`: TLS material root (default: `state/tls`).
- `AE_EDGE_LOCAL_INGRESS_CONFIG_DIR`: directory where edge-local rendered config is written.
- `AE_EDGE_LOCAL_INGRESS_CONFIG_FILE`: edge-local rendered Caddyfile path.
- `AE_EDGE_LOCAL_INGRESS_RELOAD_CMD`: command invoked by gateway after edge-local config updates.

## BYO TLS options

1) Direct file paths in your manifest
- Set `spec.ingress.tlsCertPath` and `spec.ingress.tlsKeyPath` to PEM files on the controller host.
- The Caddy site will render: `tls <cert> <key>`.

2) Kubernetes-style Secret material (auto-resolved)
- Set only `spec.ingress.tlsSecretName: <name>` in your manifest.
- Place TLS material in `AE_TLS_DIR` (default: `state/tls`) as either `<name>.crt`/`<name>.key` files or a Secret YAML/JSON `<name>.yaml|yml|json` with base64 fields `data.tls.crt` and `data.tls.key`.
- The controller resolves PEM files and fills `tlsCertPath`/`tlsKeyPath` on the fly.

`tls` helpers:
- `python -m ae.cli tls sync --name mycert --input path/to/mycert.yaml --root state/tls`
- `python -m ae.cli tls verify --name mycert --root state/tls --json`
- `python -m ae.cli tls kubesecret --name mycert --namespace demo --root state/tls -o mycert-secret.yaml`

## Validation and security gates

Use mode-isolated ingress lane runs for release confidence:

```bash
scripts/dev/validate_ingress_env.sh --lane core-proxy --watchdog
CORE_PROXY_FORCE_RATHOLE_RESTART=0 scripts/dev/run_ingress_lanes.sh --lanes core-proxy --yes

scripts/dev/validate_ingress_env.sh --lane core-to-edge-public --watchdog
scripts/dev/run_ingress_lanes.sh --lanes core-to-edge-public --yes

scripts/dev/validate_ingress_env.sh --lane edge-local --watchdog
EDGE_LOCAL_LISTENER_URL="https://lb-distribution-edge-local.home.arpa/" \
  scripts/dev/run_ingress_lanes.sh --lanes edge-local --yes

scripts/dev/security_baseline_check.sh --fail-on high
scripts/dev/security_active_tests.sh --fail-on high
```

Primary references:
- `docs/guides/ingress-capability-test-sequence.md`
- `docs/guides/multinode-lab.md`
- `docs/reference/cri-containerd.md`

## Notes

- In containers, ingress rewrites loopback upstreams to the host alias (`host.docker.internal` on Docker, `host.containers.internal` on Podman).
- To enable active health checks in Caddy, set `AE_CADDY_ACTIVE_HEALTH=1` and configure a readiness probe in the manifest.
- AppArmor & Seccomp: when exporting to K8s, `spec.security.seccompProfile*` maps to container `securityContext.seccompProfile`, and AppArmor is emitted on the Pod template. Ensure your cluster has AppArmor enabled.
- With `AE_SERVICE_PROVIDER=overlay`, Caddy upstreams point to Service VIPs; attach the Caddy host/container to `AE_OVERLAY_NET` or add routes to the Service CIDR.
