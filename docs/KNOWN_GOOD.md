# Known-Good Dev Configuration (2025-10-24)

This snapshot captures a stable, working demo configuration.

- Caddy (dev)
  - HTTP: 8888 (host) → 80 (container)
  - HTTPS: 8443 (host) → 443 (container)
  - Static sites: `ops/dev/caddy/sites/` (kept minimal; do not remove Caddyfile)
  - Dynamic sites: `state/caddy/` mounted into container at `/etc/caddy/dynsites`
  - Health checks: disabled by default (set `AE_CADDY_ACTIVE_HEALTH=1` to opt-in)
  - Reloads validated with `caddy adapt` before `reload`

- Hosts entries (optional, added by `init_demo.sh -y`)
  - `blue.home.arpa`, `green.home.arpa`, `echo-mr.home.arpa`, `docs.home.arpa`, `api.home.arpa` → `127.0.0.1`

- Controller + API
  - Supervisor auto-start enabled
  - API on `:9108` (Direct: `http://127.0.0.1:9108/`)
  - API via Caddy: `https://api.home.arpa:8443/` (Swagger `/swagger`, ReDoc `/redoc`)

- Docs
  - Built to `docs/site/`, served by `python -m http.server` on `:9109`
  - Dynsite seeds ensure `https://docs.home.arpa:8443/` works via Caddy

- Service networking
  - Shared Docker network: `dev_default` (export `AE_DOCKER_NETWORK=dev_default`)
  - Multi-replica routing via container DNS on shared network
  - Single-replica stable port via `spec.service.port` when needed

- Environment exports written to `state/env.sh`
  - `AE_CADDY_SITES=state/caddy`
  - `AE_CADDY_FILE=/etc/caddy/Caddyfile`
  - `AE_CADDY_CONTAINER=dev-caddy-1`
  - `AE_DOCKER_NETWORK=dev_default`

- Troubleshooting helpers
  - `./scripts/init_demo.sh -d` attaches logs: controller, caddy, prometheus, and site changes
  - Sanity checks validate upstream DNS/ports from appropriate context

Use `./scripts/init_demo.sh --down` to tear down and clean hosts entries.

