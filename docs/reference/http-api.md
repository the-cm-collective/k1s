# HTTP API

The controller exposes a controller-native HTTP API when started with `--metrics-port PORT`. Use this surface for controller status, operational reads, and opt-in mutations. For Kubernetes-compatible discovery and `kubectl` / Helm flows, see [API Shim](api-shim.html).

## Base URL
- Local demo and docs flows usually expose `http://127.0.0.1:9108`.
- Caddy and published control-plane surfaces usually expose `https://api.home.arpa:<port>`.
- Browser-friendly controller schema surfaces are published at [Swagger](/swagger), [ReDoc](/redoc), and [OpenAPI JSON](/openapi.json).

## Public surfaces
- `GET /metrics` exposes Prometheus metrics.
- `GET /openapi.json`, `GET /docs`, `GET /swagger`, and `GET /redoc` stay public so the controller schema and browser UIs remain reachable even when bearer tokens are configured.
- `GET /dashboard` and `GET /dashboard.js` are public when the simple dashboard is enabled. The dashboard shell is public, but the JSON and log endpoints it calls still follow the token rules below.
- Auxiliary public JSON surfaces include `GET /ui/features` and `GET /__ae/version`.

## Protected read surfaces
- `GET /health`
- `GET /status` and `GET /status/<app>`
- `GET /manifest/<app>`, `GET /events/<app>`, and `GET /history/<app>`
- `GET /nodes` and `GET /system`
- `GET /logs/<app>` and `GET /logs/<app>/stream`
- `GET /tls/verify`
- When any controller token is configured, these read surfaces require at least `AE_API_READ_TOKEN` and may also honor per-app scope filters such as `AE_API_READ_SCOPE`.

## Dev and mutation surfaces
- `POST /plan` and `POST /dashboard/plan` are read-only planner endpoints used by the dashboard and labs UI. They require READ access when tokens are configured.
- `POST /k8s/preview` is a dev-only exporter preview and stays disabled unless `AE_API_DEV_EXPORT=1`.
- `POST /scale/<app>` requires scale or admin access plus `AE_API_MUTATIONS=1`.
- `POST /delete/<app>`, `POST /rollout/pause/<app>`, `POST /rollout/resume/<app>`, `POST /apply`, and `POST /exec/<app>` require admin access plus `AE_API_MUTATIONS=1`.

## Auth model
- Public docs and schema surfaces remain reachable without a bearer token.
- Operational reads are open by default, but become bearer-protected once any of `AE_API_READ_TOKEN`, `AE_API_SCALER_TOKEN`, or `AE_API_ADMIN_TOKEN` is configured.
- Optional scope filters: `AE_API_READ_SCOPE`, `AE_API_SCALER_SCOPE`, and `AE_API_ADMIN_SCOPE`.
- Optional expiry controls: `AE_API_*_TOKEN_EXPIRES`.
- See [API Auth and Mutations](api-auth.html) for token generation, TTL handling, and remote CLI examples.

## Related surfaces
- Controller-native schema: [Swagger](/swagger), [ReDoc](/redoc), and [OpenAPI JSON](/openapi.json)
- Kubernetes-compatible schema: [API Shim](api-shim.html), [API Shim Swagger](/swagger/apishim), [API Shim ReDoc](/redoc/apishim), and [OpenAPI v3](/openapi/v3)
- `/openapi/v3` intentionally returns raw JSON. Use Swagger or ReDoc when you want a browser-oriented shim view.
