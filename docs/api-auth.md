API Auth and Mutations

Overview
- Read-only endpoints (/status, /events, /metrics, /logs, /health, /openapi.json) are available by default.
- Mutating endpoints (/scale/<app>, /delete/<app>) are disabled unless AE_API_MUTATIONS=1.
- Optional Bearer tokens gate access per role:
  - AE_API_READ_TOKEN   (read)
  - AE_API_SCALER_TOKEN (scale)
  - AE_API_ADMIN_TOKEN  (admin)

Enabling mutations (dev)
1) Export tokens and enable mutations for the controller process:
   - AE_API_MUTATIONS=1
   - AE_API_ADMIN_TOKEN=changeme
2) Start controller with --metrics-port and Caddy fronting the API.

Remote CLI usage
- Scale:
  - ae --server https://api.home.arpa:8443 --token $SCALER scale echo --replicas 2
- Delete:
  - ae --server https://api.home.arpa:8443 --token $ADMIN delete echo --purge
- Logs (requires READ token when any token is configured):
  - ae --server https://api.home.arpa:8443 --token $READ logs echo --tail 100

Security notes
- For production, place the API behind TLS (Caddy) and use client auth or network ACLs.
- Prefer short-lived tokens and minimal roles for automation.

Registry Auth (private images)
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

Refreshing short‑lived tokens
- Some providers issue short‑lived credentials (GCR/ECR). Refresh any saved hosts with:
  - `ae registry refresh` (all supported providers)
  - `ae registry refresh --provider gcr`
  - `ae registry refresh --provider ecr`
- Refresh logic uses local CLIs when available:
  - ghcr: `gh auth token` or `GHCR_TOKEN`.
  - gcr: `gcloud auth print-access-token`.
  - ecr: `aws ecr get-login-password` (region derived from the registry hostname).

Planner hint
- If the image host looks private (e.g., `ghcr.io`, `gcr.io`, `*.ecr.*.amazonaws.com`) and no entry exists in `registries.yaml`, `ae plan` emits a warning with the matching `ae registry login` command suggestion.
