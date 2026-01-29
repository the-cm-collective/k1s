L4 Services (TCP/UDP) on a Single Host

Scope: How to expose non-HTTP services when running multiple pods on one node. This complements the HTTP ingress (Caddy) guide.

Key constraints
- Caddy is an L7 HTTP/TLS proxy; it does not provide generic L4 TCP/UDP load-balancing for app ports.
- The controller assigns per-pod host ports for runtime health and connectivity; these ports are ephemeral for multi-replica apps without `spec.ingress`.

Patterns that work
- Single-replica stable port (simplest)
  - Declare `spec.service.port: <host-port>` and keep `replicas: 1`.
  - Works for any TCP protocol (e.g., raw TCP, gRPC on non-HTTP/2, custom binary).

- External L4 proxy (recommended for multi-replica)
  - Run HAProxy or Traefik TCP on the host (or in the dev compose stack) and target the per-pod host ports that the controller publishes.
  - Pros: simple, explicit; Cons: extra process to manage.
  - Dev fixture (HAProxy): `docker compose -f ops/dev/docker-compose.yaml up -d haproxy`
    - Config path: `ops/dev/haproxy/haproxy.cfg`
    - Public port: `${HAPROXY_TCP_PORT:-9000}` → HAProxy `:9000`
    - Auto-populate servers from the controller API:
      - `make haproxy-update APP=tcp-echo` (one-shot; uses `http://127.0.0.1:9108/system`)
      - `make haproxy-watch APP=tcp-echo` (continuous; validates config and restarts HAProxy on change)
      - Or run the script directly:
        - `python scripts/dev/update_haproxy_from_api.py --app tcp-echo --server http://127.0.0.1:9108 --cfg ops/dev/haproxy/haproxy.cfg --port 9000`
        - `python scripts/dev/watch_haproxy.py --app tcp-echo --server http://127.0.0.1:9108 --cfg ops/dev/haproxy/haproxy.cfg --port 9000 --compose ops/dev/docker-compose.yaml --service haproxy`
  - Example config (if editing manually):
    ```
    frontend tcp_app
      bind *:9000
      default_backend app_be
    backend app_be
      # enumerate per-pod host ports (see `ae status` or API /status)
      server r1 127.0.0.1:49152 check
      server r2 127.0.0.1:49153 check
    ```

- Kubernetes parity
  - Export: prefer `Service` + `Ingress` for HTTP paths.
  - For generic TCP: use `Service` type `NodePort` and an external L4 (Metallb/cloud LB) or a headless `Service` plus a DaemonSet sidecar proxy (advanced; out of scope here).

Planner hints
- When `replicas > 1` and no `spec.ingress` is present, the planner warns that per-pod host ports are ephemeral.
- When the exposed ports are not named `http`/`https`, the planner suggests adopting an external L4 proxy and links to this guide.

FAQ
- UDP? Not supported by the HTTP ingress path. Use an external L4 proxy that supports UDP (e.g., Traefik UDP, NGINX Stream), or keep to a single replica and a fixed host port.
- Can the controller do L4 load-balancing? Not today. Keeping the controller small and delegating L4 is intentional. This may evolve later.
- Why a watcher? During rollouts or scale operations, per-pod host ports can change as containers are replaced. The watcher keeps HAProxy’s backend list current so your L4 entrypoint remains stable without manual edits. It validates the config (haproxy -c) inside the container before restart and reverts on failure.
