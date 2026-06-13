# Ingress

## Overview

- k1s uses different ingress surfaces depending on topology and profile.
- Local app ingress and gateway-local `edge-local` output are Caddy-rendered surfaces.
- Multi-site `core-proxy` and `core-to-edge-public` flows put core Envoy in front of edge workloads.
- This page is the durable reference for ingress mode ownership, env knobs, and TLS material resolution. For exact test and lane commands, use [Ingress Validation](ingress-capability-test-sequence.html).

## Ingress modes

Set the multi-site ingress mode with `EDGE_INGRESS_MODE`:

- `core-proxy` (default): core Envoy serves traffic and proxies to the edge through Rathole.
- `core-to-edge-public`: core Envoy routes directly to edge public endpoints declared through site ingress state.
- `edge-local`: the controller publishes route bundles and the gateway renders a local Caddyfile; core ingress is not in the request path.

For the current startup shapes and profile defaults, see [Runtime Profiles](runtime-profiles.html) and [Multi-Node Lab](multinode-lab.html).

## Renderer ownership

- Controller-managed app ingress: local and single-host app ingress still render Caddy site files from `spec.ingress`. Multi-path routing remains supported, and Caddy uses `handle_path` for subpaths.
- Core multi-site ingress: `core-proxy` and `core-to-edge-public` use core Envoy as the request-facing ingress surface. The exact long-form validation and restart sequences live on [Ingress Validation](ingress-capability-test-sequence.html).
- Edge-local ingress: the gateway renders a local Caddyfile from route bundles. The strict `bundle-endpoints` lane is about route-bundle endpoint fanout plus avoiding DNS fallback, not about turning on the optional Service VIP proxy.
- Control-plane/dev hostnames: docs, API, and dashboard hostnames on local profiles are adjacent dev surfaces and are documented on [Runtime Profiles](runtime-profiles.html) and [Operations Runbook](runbook.html); they are not the same thing as app ingress mode selection.

## Environment knobs

General ingress controls:

- `AE_DISABLE_INGRESS=1`: disable ingress rendering and reload handling.
- `AE_TLS_DIR`: TLS material root for `tlsSecretName` lookup (default: `state/tls`).

Caddy-rendered app ingress:

- `AE_CADDY_SITES`: directory for rendered site files (default: `ops/dev/caddy/sites`).
- `AE_CADDY_BIN`: Caddy binary (default: `caddy`).
- `AE_CADDY_FILE`: optional path to a Caddyfile reload target.
- `AE_CADDY_CONTAINER`: Caddy container name for docker/podman exec reload.
- `AE_CONTAINER_CLI`: `podman` or `docker`.
- `AE_CADDY_RELOAD_TIMEOUT`: seconds to wait on reload (default: `10`).
- `AE_CADDY_ACTIVE_HEALTH=1`: enable active Caddy health checks when the manifest exposes a readiness probe.
- `AE_CADDY_PREFER_HOST_PORT_UPSTREAMS=1`: prefer published host ports for
  app upstreams when Caddy runs outside the workload runtime network.

Edge-local gateway rendering:

- `AE_ROUTE_BUNDLE_ENABLED=1`: enable controller route-bundle publication for edge-rendered routes. This includes `edge-local` and the edge listener behind `core-proxy`.
- `AE_EDGE_LOCAL_UPSTREAM_MODE`: upstream selection mode; `bundle-endpoints` is the strict route-bundle lane and `auto` may fall back to DNS.
- `AE_EDGE_LOCAL_INGRESS_CONFIG_DIR`: directory for rendered edge-local config output.
- `AE_EDGE_LOCAL_INGRESS_CONFIG_FILE`: rendered edge-local Caddyfile path.
- `AE_EDGE_LOCAL_INGRESS_RELOAD_CMD`: gateway-side reload command run after edge-local config updates.
- `AE_EDGE_LOCAL_INGRESS_SCHEME`: Caddy site scheme for rendered routes, normally `https`; use `http` for the local listener behind a `core-proxy` Rathole tunnel.
- `AE_EDGE_LOCAL_INGRESS_LISTEN_PORT`: optional explicit Caddy site port for rendered routes, for example `18081` when `AE_EDGE_INGRESS_LOCAL_ADDR=127.0.0.1:18081`.

Optional Service VIP plumbing:

- `AE_ENABLE_SERVICE_PROXY=1` enables the controller-managed Service VIP path.
- On CRI/containerd, `AE_SERVICE_PROVIDER=iptables` is the normal provider choice.
- With `AE_SERVICE_PROVIDER=overlay`, Caddy upstreams point to Service VIPs; attach the Caddy host/container to `AE_OVERLAY_NET` or add routes to the Service CIDR.
- This Service VIP path is optional and separate from the strict `edge-local` route-bundle validation gate.

## TLS material

Direct file paths:

- Set `spec.ingress.tlsCertPath` and `spec.ingress.tlsKeyPath` to PEM files on the controller host.
- Caddy-rendered surfaces emit `tls <cert> <key>` from those resolved paths.

Kubernetes-style Secret material:

- Set only `spec.ingress.tlsSecretName: <name>` in the manifest.
- Place TLS material in `AE_TLS_DIR` as either `<name>.crt` / `<name>.key` files or a Secret YAML / JSON document with base64 `data.tls.crt` and `data.tls.key`.
- The controller resolves PEM files and fills `tlsCertPath` / `tlsKeyPath` on the fly. If material is missing, Caddy-rendered surfaces fall back to `tls internal` in dev.

`tls` helpers:

- `python -m ae.cli tls sync --name mycert --input path/to/mycert.yaml --root state/tls`
- `python -m ae.cli tls verify --name mycert --root state/tls --json`
- `python -m ae.cli tls kubesecret --name mycert --namespace demo --root state/tls -o mycert-secret.yaml`

## Validation Surfaces

Use [Ingress Validation](ingress-capability-test-sequence.html) as the canonical command-heavy ingress runbook. It owns:

- ingress preflight checks with `scripts/dev/validate_ingress_env.sh`
- guided lane execution through `scripts/dev/run_ingress_lanes.sh`
- single-host and multi-host ingress matrix coverage
- security baseline and active auth probes

Companion references:

- [Runtime Profiles](runtime-profiles.html) for profile defaults and local TLS/docs surfaces
- [Multi-Node Lab](multinode-lab.html) for long-form topology walkthroughs
- [CRI containerd](cri-containerd.html) for CRI-specific ingress framing
- [Operations Runbook](runbook.html) for ops-side ingress/TLS helpers

## Notes

- In containers, Caddy-rendered ingress rewrites loopback upstreams to the host alias (`host.docker.internal` on Docker, `host.containers.internal` on Podman).
- The strict `edge-local` lane is green when gateway-local Caddy renders bundle endpoint fanout instead of DNS fallback upstreams.
- Core Envoy listener and trust bootstrapping for local and CRI validation lanes is documented on [Runtime Profiles](runtime-profiles.html) and [Ingress Validation](ingress-capability-test-sequence.html).
