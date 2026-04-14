# API Auth and Mutations

## Overview
- Public docs and schema surfaces (`/metrics`, `/openapi.json`, `/docs`, `/swagger`, `/redoc`, and the dashboard shell when enabled) remain reachable without a bearer token.
- Protected read endpoints (`/status`, `/events`, `/nodes`, `/logs`, `/health`, `/system`, `/manifest`, `/history`, `/tls/verify`) are available by default.
- If any token is configured, the protected read endpoints require at least the READ token.
- Mutating endpoints (/scale/<app>, /delete/<app>, /rollout/*, /apply) are disabled unless AE_API_MUTATIONS=1.
- Optional Bearer tokens gate access per role:
  - AE_API_READ_TOKEN   (read)
  - AE_API_SCALER_TOKEN (scale)
  - AE_API_ADMIN_TOKEN  (admin)
- Optional scoping/expiry: AE_API_ADMIN_SCOPE / AE_API_SCALER_SCOPE / AE_API_READ_SCOPE (glob patterns) and AE_API_*_TOKEN_EXPIRES (UTC ISO8601).

## Enabling mutations (dev)
1) Generate tokens and enable mutations for the controller process:
```
ae api tokens --generate --ttl-hours 24 -o .env.api
source .env.api
export AE_API_MUTATIONS=1
```
2) Start controller with --metrics-port and (optional) Caddy fronting the API.

Tip: `ae auth remote -o .env.api` also emits apishim tokens and `AE_API_MUTATIONS=1`.

## Remote CLI usage
- Scale:
  - `ae --server https://api.home.arpa:8443 --token $AE_API_SCALER_TOKEN scale echo --replicas 2`
- Delete:
  - `ae --server https://api.home.arpa:8443 --token $AE_API_ADMIN_TOKEN delete echo --purge`
- Logs (requires READ token when any token is configured):
  - `ae --server https://api.home.arpa:8443 --token $AE_API_READ_TOKEN logs echo --tail 100`

## Security notes
- For production, place the API behind TLS (Caddy) and use client auth or network ACLs.
- Prefer short-lived tokens and minimal roles for automation.
- Kubernetes API shim: set `AE_APISHIM_ENABLE=1` and `AE_APISHIM_TOKEN` for `python -m ae.apishim serve`; use `AE_APISHIM_ALLOW_ANON=1` only for local labs. Shim RBAC evaluates Role/ClusterRole bindings and exposes a SubjectAccessReview-compatible endpoint.

## Registry Auth (private images)
- Credentials are stored at `~/.config/ae/registries.yaml` and used by the runtime before pulls.
- List configured hosts:
  - `ae registry list`
- Docker Hub:
  - Short image names (e.g., `caddy:2.8`, `python:3.12-slim`) are treated as `docker.io`.
  - Add Docker Hub creds by host key `docker.io` (also accepts `index.docker.io`):
    - `ae registry login custom --registry docker.io --username <you> --password <token>`
    - Or run `docker login` once; the runtime will reuse your local Docker credentials.
- GHCR (GitHub Container Registry):
  - `ae registry login ghcr --username <you> --token <PAT>`
  - Or rely on `gh` CLI: `ae registry login ghcr --username <you>` (uses `gh auth token`)
- GCR (Google Container Registry/Artifact Registry):
  - `ae registry login gcr --use-gcloud --gcr-host us.gcr.io`
  - Or provide a token: `ae registry login gcr --gcr-host us.gcr.io --token $(gcloud auth print-access-token)`
  - Username defaults to `oauth2accesstoken`.
- ECR (AWS Elastic Container Registry):
  - `ae registry login ecr --use-aws --region us-east-1 --account-id 123456789012`
  - Or specify: `ae registry login ecr --registry 123456789012.dkr.ecr.us-east-1.amazonaws.com --password $(aws ecr get-login-password)`
- Custom registry:
  - `ae registry login custom --registry registry.example.com --username user --password secret`

## Refreshing short‑lived tokens
- Some providers issue short‑lived credentials (GCR/ECR). Refresh any saved hosts with:
  - `ae registry refresh` (all supported providers)
  - `ae registry refresh --provider gcr`
  - `ae registry refresh --provider ecr`
- Refresh logic uses local CLIs when available:
  - ghcr: `gh auth token` or `GHCR_TOKEN`.
  - gcr: `gcloud auth print-access-token`.
  - ecr: `aws ecr get-login-password` (region derived from the registry hostname).

## Planner hint
- If the image host looks private (e.g., `ghcr.io`, `gcr.io`, `*.ecr.*.amazonaws.com`) and no entry exists in `registries.yaml`, `ae plan` emits a warning with the matching `ae registry login` command suggestion.
