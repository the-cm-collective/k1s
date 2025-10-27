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
- AE_CONTAINER_CLI: docker or podman (default: docker).
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

Multi-path routing
- In your App manifest, set `spec.ingress.paths: ["/", "/api"]` to render multiple routes.

Notes
- For containers, the ingress manager will adapt loopback upstreams to host.docker.internal or host.containers.internal as needed.
- To enable active health checks in Caddy, set `AE_CADDY_ACTIVE_HEALTH=1` and configure a readiness probe in the manifest.

