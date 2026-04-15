# API Auth

This page is the auth reference for the controller-native HTTP API and the optional Kubernetes API shim. Use [HTTP API](http-api.html) for the controller endpoint catalog, [API Shim](api-shim.html) for the Kubernetes-compatible surface, and [Operations Runbook](runbook.html) for longer operator workflows.

## Controller HTTP API

- Public surfaces stay reachable without a bearer token:
  - `/metrics`
  - `/openapi.json`, `/docs`, `/swagger`, `/redoc`
  - the simple dashboard shell and assets when enabled
- Once any of `AE_API_READ_TOKEN`, `AE_API_SCALER_TOKEN`, or `AE_API_ADMIN_TOKEN` is configured, protected reads require bearer auth.
- Protected reads include controller health/system routes plus app-targeted status, manifest, events, history, and logs.
- Mutations remain disabled unless `AE_API_MUTATIONS=1`.
- The planner endpoints (`POST /plan`, `POST /dashboard/plan`) are read-only, but they still require READ access when controller tokens are configured.

## Token Roles, Scope, and Expiry

- `AE_API_READ_TOKEN`: read-only operational access.
- `AE_API_SCALER_TOKEN`: READ plus `/scale/<app>`.
- `AE_API_ADMIN_TOKEN`: READ plus admin and mutation routes such as `/apply`, `/delete/<app>`, `/rollout/*`, and `/exec/<app>`.
- Optional expiry controls: `AE_API_ADMIN_TOKEN_EXPIRES`, `AE_API_SCALER_TOKEN_EXPIRES`, `AE_API_READ_TOKEN_EXPIRES`.
- Optional scopes: `AE_API_ADMIN_SCOPE`, `AE_API_SCALER_SCOPE`, `AE_API_READ_SCOPE`.
- Scope note: admin and scaler scopes gate app-targeted mutations. Read scope is only enforced on app-targeted read routes today, such as `/status/<app>`, `/events/<app>`, `/manifest/<app>`, `/history/<app>`, and `/logs/<app>`. Controller-wide list and system surfaces remain controller-wide.

## Common Controller Workflows

Generate controller tokens and expiries:

```bash
ae api tokens --generate --ttl-hours 24 -o .env.api
source .env.api
export AE_API_MUTATIONS=1
```

Remote bootstrap helper:

```bash
ae auth remote -o .env.api
source .env.api
ae --server https://api.home.arpa:8443 --token "$AE_API_READ_TOKEN" status
ae --server https://api.home.arpa:8443 --token "$AE_API_SCALER_TOKEN" scale echo --replicas 2
ae --server https://api.home.arpa:8443 --token "$AE_API_ADMIN_TOKEN" delete echo --purge
```

## API Shim Auth

- `AE_APISHIM_ENABLE=1` is required to run the shim.
- `AE_APISHIM_TOKEN` is the full-access admin token and the default kubeconfig token.
- `AE_APISHIM_READ_TOKEN` is the read/list/watch credential for clients that should not mutate shim-backed resources.
- `AE_APISHIM_EXEC_TOKEN` and `AE_APISHIM_PORTFORWARD_TOKEN` gate the interactive exec and port-forward flows.
- `AE_APISHIM_MINT_TOKEN` and `AE_APISHIM_SESSION_SECRET` support short-lived scoped session tokens for non-root CLI usage.
- `AE_APISHIM_ALLOW_ANON=1` or `--allow-anonymous` is dev-only and should not be used on shared hosts.
- For TLS, enable `--tls`, set `AE_APISHIM_TLS_CERT` and `AE_APISHIM_TLS_KEY`, and distribute the CA bundle via `AE_APISHIM_CA_BUNDLE` or a trusted local CA.

Preferred operator flow:

```bash
AE_APISHIM_ENABLE=1 AE_APISHIM_TOKEN=changeme \
python -m ae.apishim serve --host 127.0.0.1 --port 8445

python -m ae.apishim kubeconfig \
  --server http://127.0.0.1:8445 \
  --token "$AE_APISHIM_TOKEN" \
  --insecure-skip-tls-verify > ~/.kube/k1s-apishim.yaml

source <(ae auth local --strict)
ae auth mint --role exec --scope default/echo
```

Use `ae auth local --strict` for shared profile shells and keep `AE_APISHIM_TOKEN` service-only. Prefer mint or role-specific shim tokens for routine operator access.

## Security Notes

- Put the controller API behind TLS or a trusted front-end such as Caddy for shared environments.
- Prefer short-lived controller tokens and the smallest role that satisfies the workflow.
- Keep shim admin credentials service-only; use `AE_APISHIM_MINT_TOKEN`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_EXEC_TOKEN`, and `AE_APISHIM_PORTFORWARD_TOKEN` for user-facing flows.
- Registry credentials are separate from API auth. Use the [Operations Runbook](runbook.html) for `ae registry login` and refresh procedures.

## Related Pages

- [HTTP API](http-api.html)
- [API Shim](api-shim.html)
- [Operations Runbook](runbook.html)
