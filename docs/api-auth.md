API Auth and Mutations

Overview
- Read-only endpoints (/status, /events, /metrics, /logs) are available by default.
- Mutating endpoints (/apply, /scale/<app>, /delete/<app>) are disabled unless AE_API_MUTATIONS=1.
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
- Apply:
  - ae --server https://api.home.arpa:8443 --token changeme apply -f specs/examples/echo-sec.yaml
- Scale:
  - ae --server https://api.home.arpa:8443 --token scaler scale echo --replicas 2
- Delete:
  - ae --server https://api.home.arpa:8443 --token changeme delete echo --purge

Security notes
- For production, place the API behind TLS (Caddy) and use client auth or network ACLs.
- Prefer short-lived tokens and minimal roles for automation.

