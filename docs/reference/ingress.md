Ingress Guide (Caddy)

Overview
- The controller writes one Caddy site per App manifest with spec.ingress.
- Multi-path routing is supported; Caddy uses handle_path for subpaths.
- TLS defaults to Caddy's internal CA for dev; you can supply real certs.

Environment knobs
- AE_CADDY_SITES: directory for rendered site files (default: ops/dev/caddy/sites).
- AE_CADDY_BIN: Caddy binary (default: caddy).
- AE_CADDY_FILE: optional path to a Caddyfile (container/host reload target).
- AE_CADDY_CONTAINER: name of the Caddy container (enables docker/podman exec reload).
- AE_CONTAINER_CLI: podman or docker (default: docker).
- AE_CADDY_RELOAD_TIMEOUT: seconds to wait on reload (default: 10).

BYO TLS options
1) Direct file paths in your manifest
   - Set `spec.ingress.tlsCertPath` and `spec.ingress.tlsKeyPath` to PEM files on the controller host.
   - The Caddy site will render: `tls <cert> <key>`.

2) Kubernetes-style Secret material (auto-resolved)
   - Set only `spec.ingress.tlsSecretName: <name>` in your manifest.
   - Place TLS material in `AE_TLS_DIR` (default: state/tls):
     - Either files `<name>.crt` and `<name>.key`, or
     - A Secret YAML/JSON `<name>.yaml|yml|json` with base64 fields data.tls.crt, data.tls.key.
   - The controller resolves PEM files and fills tlsCertPath/Key on the fly.

CLI helper: tls sync
- Render PEMs from a Secret file (or confirm direct files):
  - `python -m ae.cli tls sync --name mycert --input path/to/mycert.yaml --root state/tls`
  - Output shows where cert/key were written (under state/tls/rendered/).

CLI helper: tls verify
- Check if TLS material is resolvable for a given name (without copying):
  - `python -m ae.cli tls verify --name mycert --root state/tls`
  - Returns non-zero if not found; use `--json` for machine-readable results.

CLI helper: tls kubesecret
- Generate a Kubernetes TLS Secret YAML (`kubernetes.io/tls`) from PEMs under `AE_TLS_DIR`:
  - `python -m ae.cli tls kubesecret --name mycert --namespace demo --root state/tls -o mycert-secret.yaml`
- Apply it to your cluster and reference it from the exporter/manifest via `ingress.tlsSecretName: mycert`.

Multi-path routing
- In your App manifest, set `spec.ingress.paths: ["/", "/api"]` to render multiple routes.

Notes
- For containers, the ingress manager will adapt loopback upstreams to the correct host alias: `host.docker.internal` (Docker) or `host.containers.internal` (Podman).
- To enable active health checks in Caddy, set `AE_CADDY_ACTIVE_HEALTH=1` and configure a readiness probe in the manifest.
- AppArmor & Seccomp: When exporting to K8s, the engine maps `spec.security.seccompProfile*` to the container `securityContext.seccompProfile` and sets an AppArmor annotation on the Pod template. Ensure your cluster supports AppArmor (e.g., apparmor_parser present and profiles loaded). On some local clusters (Kind/MicroK8s), you may need to enable AppArmor and allow custom profiles.
- Multi-node overlay: When `AE_SERVICE_PROVIDER=overlay` is enabled, Caddy upstreams point at Service VIPs on the overlay network. Attach the Caddy host/container to `AE_OVERLAY_NET` or add routes so VIPs are reachable.
