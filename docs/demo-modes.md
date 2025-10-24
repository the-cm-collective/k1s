## Demo Modes

The init script can stand up different demo combinations. Use the flags below
to control which apps are applied. Add `-y` to auto-add hosts and `-d` to attach
logs (Ctrl-C to exit).

### Standard Demo (blue/green)

- Apps: blue and green services behind TLS via Caddy
- Command:
  - `./scripts/init_demo.sh --demo-standard -y`
  - `make demo ARGS="--demo-standard -y -d"`
- Endpoints:
  - `https://blue.home.arpa:8443/`
  - `https://green.home.arpa:8443/`

### Configs & Secrets Demo (echo)

- Shows `configRefs` and `secretRefs` → env and file projections.
- Projections (host): `state/projections/echo-revN/{config,secret}/...`
- Mounted RO (container): `/var/run/ae/config/echo`
- Command:
  - `./scripts/init_demo.sh --demo-configs -y`
  - `make demo` (defaults to `-y --demo-configs`)

### Multi-Replica Echo (echo-mr)

- Shows Caddy load-balancing across multiple replicas on a shared Docker network.
- Command:
  - `./scripts/init_demo.sh --demo-echo-mr -y`
  - `make demo ARGS="--demo-echo-mr -y -d"`
- Endpoint: `https://echo-mr.home.arpa:8443/`

### Docs Only

- Starts the docs server and API; does not apply any apps.
- Command:
  - `./scripts/init_demo.sh --docs-only -y`
  - `make demo ARGS="--docs-only -y -d"`
- Endpoints:
  - Docs: `https://docs.home.arpa:8443/` and `http://127.0.0.1:9109/`
  - API:  `https://api.home.arpa:8443/swagger` and `http://127.0.0.1:9108/swagger`

### Helpful Flags & Targets

- `-d, --debug` — attach logs for controller, caddy, prometheus, and site changes.
- `--down -y` — tear down: `./scripts/init_demo.sh --down -y`
- `make demo-help` — print demo script usage
- `make demo-down` — tear down demo
- `make integ-test` — run integration tests (set `AE_DOCKER_TEST=1`)

### Notes

- Caddy HTTP: `:8888`, HTTPS: `:8443`.
- Hosts entries (added with `-y`): `blue|green|echo-mr|docs|api.home.arpa` → `127.0.0.1`.
- Health checks are disabled by default for compatibility; enable with `AE_CADDY_ACTIVE_HEALTH=1` if your Caddy supports the directive.

