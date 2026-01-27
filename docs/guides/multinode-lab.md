# Multi-node Lab Walkthrough

Use this quickstart to exercise the multi-node path on two Linux hosts (one controller + one worker). It mirrors the multi-node smoke the CI job will run.

## Prereqs
- Two hosts (or VMs) with Python 3.11+, Podman or Docker, WireGuard tools (`wg`, `wg-quick`), and passwordless SSH between them for convenience.
- Rootful networking on both nodes (rootless overlay is not supported); a small privileged helper is required to attach WireGuard.
- Controller host exposes TCP 9110 (agent API) and UDP 51820 (or your chosen WireGuard port).
- Clone this repo on the controller; worker only needs `ae` installed plus the `ae-node` entrypoint.

## Bring up the controller
```bash
AE_ENABLE_SERVICE_PROXY=1 \
AE_SERVICE_PROVIDER=overlay \
AE_OVERLAY_NET=${AE_OVERLAY_NET:-ae-overlay} \
AE_SERVICE_IP_POOL=${AE_SERVICE_IP_POOL:-10.241.0.0/16} \
AE_POD_CIDR_POOL=${AE_POD_CIDR_POOL:-10.42.0.0/16} \
AE_POD_CIDR_MASK=${AE_POD_CIDR_MASK:-24} \
AE_AGENT_API_PORT=${AE_AGENT_API_PORT:-9110} \
AE_AGENT_API_TOKEN=REDACTED"$(cat /etc/wireguard/wg0.conf)" \
python -m ae.node --runtime-backend podman --port 9109 --ensure-pod-net
```
- Leave `AE_POD_CIDR` empty to let the controller assign one on first heartbeat.
- If WireGuard is disabled, drop `--ensure-pod-net` and set `AE_SERVICE_PROVIDER=bridge` on the controller (no cross-node routing).

## Deploy the demo app
Use the multi-node friendly sample (`specs/examples/echo-multinode.yaml`):
```bash
python -m ae.cli apply -f specs/examples/echo-multinode.yaml
ae nodes list
ae status echo-mn --watch
```
Expected: one replica on each node, Service VIP allocated from `AE_SERVICE_IP_POOL`, Caddy (or your ingress) targets the Service VIP instead of hostPorts.

## Validate routing and failover
- Verify Service VIP reachability from the controller host: `curl http://$AE_SERVICE_IP_POOL_FIRST_IP/healthz` (replace with the allocated ClusterIP from `ae services list`).
- Simulate node loss: `ae nodes --cordon <node>` then stop the agent; reconcile should reschedule replicas to the surviving node (storage-bound apps will remain pending by design).
- Exec/logs proxied via agent: `ae exec echo-mn -- id` and `ae logs echo-mn --tail 5` (add `-n <ns>` if the app is not in `default`).

## Fast smoke commands
- Unit + integration (includes remote agent stub test): `pytest tests/unit/test_scheduler.py tests/integration/test_multinode_agent_flow.py`
- Planner checks: `ae plan -f specs/examples/echo-multinode.yaml --strict`
- End-to-end lab helper: `./ops/dev/multinode-lab.sh -h` for environment hints.

## Cleanup
- `ae delete echo-mn`
- Stop agents and controller; remove the overlay network `docker network rm $AE_OVERLAY_NET` (or Podman equivalent) if created.
