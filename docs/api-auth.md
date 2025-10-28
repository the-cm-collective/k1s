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
