# MicroK8s Dev Stack

This lane packages `k1s` into a namespace-scoped MicroK8s dev stack on Host B while leaving shared cluster addons alone. It is a dev and failure-simulation surface, not the canonical multi-host HA evidence lane.

## Repo Assets

- Host B core chart: `ops/helm/k1s-core-ha`
- Host B local node chart: `ops/helm/k1s-node-local`
- Host B site gateway chart: `ops/helm/k1s-edge-gateway`
- Checked-in sample values:
  - `ops/helm/examples/k1s-core-ha-values.microk8s.yaml`
  - `ops/helm/examples/k1s-node-local-values.microk8s.yaml`
  - `ops/helm/examples/k1s-edge-gateway-values.microk8s.yaml`
- Host A bootstrap renderer: `scripts/dev/microk8s_stack_bundle.py`
- Host A sample env: `ops/dev/host-a-edge-core.env.sample`

## Cluster Assumptions

- Shared addons already exist and must stay shared:
  - `ingress-nginx` with class `nginx`
  - MetalLB
  - kube-prometheus-stack / prometheus-operator
  - GPU operator
  - reachable OCI registry
- The MicroK8s node remains single-host for this lane.
- Real site-specific values belong under `.local/` so repo-tracked samples stay portable.

## Image Prep

When `global.registryHost` is set, the chart rewrites all image pulls through that registry.
Build the repo-local images there first, then mirror the upstream runtime images the chart also uses.

```bash
export REGISTRY_HOST=registry.k1s.home.arpa:32000
export TAG=dev

podman build -f ops/images/controller.Dockerfile -t ${REGISTRY_HOST}/k1s/k1s-core:${TAG} .
podman build -f ops/images/apishim.Dockerfile -t ${REGISTRY_HOST}/k1s/k1s-apishim:${TAG} .
podman build -f ops/images/node.Dockerfile -t ${REGISTRY_HOST}/k1s/k1s-core-node:${TAG} .
podman build -f ops/images/gateway.Dockerfile -t ${REGISTRY_HOST}/k1s/k1s-edge-core:${TAG} .
podman build -f ops/images/rathole.Dockerfile -t ${REGISTRY_HOST}/k1s/k1s-rathole:${TAG} .

podman push --tls-verify=false ${REGISTRY_HOST}/k1s/k1s-core:${TAG}
podman push --tls-verify=false ${REGISTRY_HOST}/k1s/k1s-apishim:${TAG}
podman push --tls-verify=false ${REGISTRY_HOST}/k1s/k1s-core-node:${TAG}
podman push --tls-verify=false ${REGISTRY_HOST}/k1s/k1s-edge-core:${TAG}
podman push --tls-verify=false ${REGISTRY_HOST}/k1s/k1s-rathole:${TAG}
```

Mirror the upstream runtime images into the same registry if you keep `global.registryHost` enabled:

```bash
for src in \
  quay.io/coreos/etcd:v3.5.14 \
  docker.io/nats:2.10.18-alpine \
  docker.io/natsio/prometheus-nats-exporter:0.19.2 \
  docker.io/library/caddy:2.8 \
  docker.io/envoyproxy/envoy:v1.29-latest
do
  podman pull ${src}
  podman tag ${src} ${REGISTRY_HOST}/${src}
  podman push --tls-verify=false ${REGISTRY_HOST}/${src}
done
```

The `k1s-edge-core` gateway image is required when this lane keeps app ingress in `core-proxy`.

## Install

### 1. Create local overrides

Keep live values under `.local/helm/`.

```bash
mkdir -p .local/helm
cp ops/helm/examples/k1s-core-ha-values.microk8s.yaml .local/helm/k1s-dev-a.core.yaml
cp ops/helm/examples/k1s-node-local-values.microk8s.yaml .local/helm/k1s-dev-a.node.yaml
cp ops/helm/examples/k1s-edge-gateway-values.microk8s.yaml .local/helm/k1s-dev-a.edge.yaml
```

Edit at least these fields before install:

- `.local/helm/k1s-dev-a.core.yaml`
  - `stack.name`
  - `stack.domain`
  - `global.registryHost`
  - `controller.runtimeHostSocketPath`
  - `bootstrap.controller.hostOverride`
  - `bootstrap.natsLeaf.hostOverride`
  - `bootstrap.rathole.hostOverride`
  - `controller.routeBundles.enabled`
  - `controller.routeBundles.replayIntervalSeconds`
  - `auth.*` secrets
- `.local/helm/k1s-dev-a.node.yaml`
  - `target.namespace`
  - `target.controllerReleaseName`
  - `node.runtimeHostSocketPath`
  - `node.nodeSelector`
- `.local/helm/k1s-dev-a.edge.yaml`
  - `target.namespace`
  - `target.controllerReleaseName`
  - `gateway.siteId`
  - `gateway.nodeId`
  - `global.registryHost`

On MicroK8s, set both runtime socket host paths to:

```yaml
controller:
  runtimeHostSocketPath: /var/snap/microk8s/common/run/containerd.sock

node:
  runtimeHostSocketPath: /var/snap/microk8s/common/run/containerd.sock
```

### 2. Preflight shared addons

Run these checks before touching the namespace:

```bash
kubectl get ingressclass nginx
kubectl -n ingress-nginx get svc
kubectl -n metallb-system get all
kubectl -n monitoring get servicemonitors,prometheusrules
kubectl get nodes -L nvidia.com/gpu.present
kubectl -n container-registry get svc
```

Do not continue until the shared addon namespaces are healthy.

### 3. Install the core stack

```bash
helm upgrade --install k1s-dev-a ops/helm/k1s-core-ha \
  --namespace k1s-dev-a \
  --create-namespace \
  -f .local/helm/k1s-dev-a.core.yaml
```

Wait for the core components:

```bash
kubectl -n k1s-dev-a rollout status deploy/k1s-dev-a-k1s-core-ha-controller
kubectl -n k1s-dev-a rollout status deploy/k1s-dev-a-k1s-core-ha-apishim
kubectl -n k1s-dev-a rollout status sts/k1s-dev-a-k1s-core-ha-etcd
kubectl -n k1s-dev-a rollout status sts/k1s-dev-a-k1s-core-ha-nats
kubectl -n k1s-dev-a get svc
```

Core validation after install:

```bash
kubectl -n k1s-dev-a get pods -o wide
kubectl -n k1s-dev-a get ingress
kubectl -n k1s-dev-a get servicemonitors,prometheusrules
kubectl -n k1s-dev-a get svc | rg 'metrics|nats'
kubectl -n k1s-dev-a port-forward svc/k1s-dev-a-k1s-core-ha-controller 9108:9108 9110:9110 &
curl -fsS http://127.0.0.1:9110/healthz
curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_controller_authority_healthy|ae_controller_is_leader'
```

Expect exactly one leader and three controller pods.

Prometheus validation after install:

```bash
kubectl -n monitoring port-forward svc/kube-prom-prometheus 9090:9090 &
curl -fsS 'http://127.0.0.1:9090/api/v1/targets?state=active' | rg 'k1s-dev-a-k1s-core-ha-(controller|etcd|nats)'
```

Expect controller, etcd, and NATS targets to be `up` without duplicate headless-service scrapes.

### 3a. Validate local ingress for docs and dash

The ingress hosts must be usable on Host B for local testing, not only present in the manifest.
If lab DNS does not already resolve them, add temporary local mappings for the ingress IP:

```bash
echo "192.168.29.15 dash.k1s-dev-a.home.arpa docs.k1s-dev-a.home.arpa" | sudo tee -a /etc/hosts
```

Then validate through `ingress-nginx` using the real hosts:

```bash
curl -fsS -H 'Host: dash.k1s-dev-a.home.arpa' http://192.168.29.15/
curl -fsS -H 'Host: docs.k1s-dev-a.home.arpa' http://192.168.29.15/
curl -fsS http://dash.k1s-dev-a.home.arpa/
curl -fsS http://docs.k1s-dev-a.home.arpa/
```

Treat `dash` and `docs` ingress reachability as a required validation gate for this lane.

### 4. Render the Host A bootstrap bundle

```bash
python scripts/dev/microk8s_stack_bundle.py \
  --release k1s-dev-a \
  --namespace k1s-dev-a \
  --site-id host-a \
  --from-kube \
  --format json \
  --output .local/host-a-bundle.json

python scripts/dev/microk8s_stack_bundle.py \
  --release k1s-dev-a \
  --namespace k1s-dev-a \
  --site-id host-a \
  --from-kube \
  --format env \
  --output .local/host-a-edge-core.env
```

Inspect the rendered contract:

```bash
cat .local/host-a-bundle.json
sed -n '1,120p' .local/host-a-edge-core.env
```

### 5. Install the exclusive Host B local node

Install this only for the single namespace that currently owns the Host B GPU.

```bash
helm upgrade --install k1s-dev-a-node ops/helm/k1s-node-local \
  --namespace k1s-dev-a \
  -f .local/helm/k1s-dev-a.node.yaml
```

Validate local node registration:

```bash
kubectl -n k1s-dev-a rollout status ds/k1s-dev-a-node-k1s-node-local
kubectl -n k1s-dev-a get ds,pods -o wide
kubectl -n k1s-dev-a logs ds/k1s-dev-a-node-k1s-node-local --tail=100
curl -fsS http://127.0.0.1:9110/v1/nodes | python3 -m json.tool
AGENT_TOKEN="$(kubectl -n k1s-dev-a get secret k1s-dev-a-k1s-core-ha-auth -o jsonpath='{.data.agent-token}' | base64 -d)"
curl -fsS -H "X-Agent-Token: ${AGENT_TOKEN}" http://127.0.0.1:9110/v1/nodes/c3rb3rus/overlay | python3 -m json.tool
curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_nodes_total|ae_nodes_ready|ae_nodes_stale'
```

The chart is cluster-exclusive on purpose. A second `k1s-node-local` install should fail while the first exists.

### 6. Install the Host B edge gateway

Install this chart when the stack is expected to serve app ingress through `core-proxy`.
The gateway pod owns the Host B site tunnel client and the local Caddy listener that receives traffic from Rathole.
For the HA MicroK8s lane, keep `rathole.connectAllControllerPods: true`; the gateway resolves the controller headless service and opens one Rathole client to each controller pod so every core Envoy can reach the same site-local listener.

```bash
helm upgrade --install k1s-dev-a-edge ops/helm/k1s-edge-gateway \
  --namespace k1s-dev-a \
  -f .local/helm/k1s-dev-a.edge.yaml
```

Validate the gateway pod and the core-proxy tunnel:

```bash
kubectl -n k1s-dev-a rollout status deploy/k1s-dev-a-edge-k1s-edge-gateway
kubectl -n k1s-dev-a logs deploy/k1s-dev-a-edge-k1s-edge-gateway -c gateway --tail=100
kubectl -n k1s-dev-a logs deploy/k1s-dev-a-edge-k1s-edge-gateway -c rathole-client --tail=100
kubectl -n k1s-dev-a logs deploy/k1s-dev-a-edge-k1s-edge-gateway -c edge-caddy --tail=100
curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_site_gateway_last_seen_seconds|ae_route_bundle_pending|ae_route_bundle_ack_age_seconds'
```

Expected state:

- the gateway site id matches the local node site, normally `host-b`
- `ae_site_gateway_last_seen_seconds{site="host-b",...}` is present and recent
- route bundle pending returns to `0` after an app route is present
- every controller Rathole server has a per-site service for `host-b`
- the assigned core-proxy port opens only after the Rathole client is connected

Route bundles are replayed periodically even after acknowledgement. Keep `controller.routeBundles.replayIntervalSeconds` nonzero so a restarted gateway receives the last known-good route set without waiting for an app change.

For the first MVP pass, keep `transport.mode: directHub`. Use `transport.mode: edgeNatsLeaf` after the direct-hub path is green and you want the edge NATS leaf topology.

If the controller renders a `core-proxy` route but no `1808x` core-proxy port is reachable, do not expose those dynamic ports as Kubernetes Services. Treat it as an edge-gateway health problem: inspect the gateway, Rathole client, Caddy listener, route bundle ack, and site id alignment.

### 7. Bring up Host A

Preferred path:

- Boot a small dedicated edge-core VM on Host A.
- Feed it `.local/host-a-edge-core.env`.
- Keep the existing GPU VM compute-only.

Fallback path:

- Source `.local/host-a-edge-core.env` inside the existing GPU VM.
- Start the edge-core role under separate systemd units with explicit CPU and memory caps.

## Removal

Remove in dependency order so the cluster does not keep orphaned GPU or site transport state.

### Remove the edge gateway first

```bash
helm uninstall k1s-dev-a-edge --namespace k1s-dev-a
kubectl -n k1s-dev-a get deploy,pvc | rg 'k1s-dev-a-edge|NAME'
```

Keep the gateway PVC until you decide whether the local gateway spool is useful for diagnostics.

### Remove the Host B local node next

```bash
helm uninstall k1s-dev-a-node --namespace k1s-dev-a
kubectl -n k1s-dev-a get ds
```

Wait until the local node DaemonSet is gone before removing the core stack.

### Stop Host A intake next

- Preferred VM path:
  - stop the edge-core VM
  - keep the generated bundle if you may reuse the release
- Fallback in-guest path:
  - stop the edge-core units
  - remove only the edge-core overlay config, not the GPU node state

### Remove the core stack

```bash
helm uninstall k1s-dev-a --namespace k1s-dev-a
kubectl -n k1s-dev-a get all
kubectl -n k1s-dev-a get pvc
```

Default removal policy:

- keep PVCs for etcd, NATS, and controller state until you intentionally purge them
- remove the namespace only after deciding whether rollback data should stay

Full purge:

```bash
kubectl -n k1s-dev-a delete pvc --all
kubectl delete namespace k1s-dev-a
```

## Rollback

### Roll back the core chart

List revisions:

```bash
helm history k1s-dev-a --namespace k1s-dev-a
```

Rollback:

```bash
helm rollback k1s-dev-a <revision> --namespace k1s-dev-a
kubectl -n k1s-dev-a rollout status deploy/k1s-dev-a-k1s-core-ha-controller
kubectl -n k1s-dev-a rollout status sts/k1s-dev-a-k1s-core-ha-etcd
kubectl -n k1s-dev-a rollout status sts/k1s-dev-a-k1s-core-ha-nats
```

Post-rollback checks:

```bash
kubectl -n k1s-dev-a get pods
kubectl -n k1s-dev-a get svc
kubectl -n k1s-dev-a port-forward svc/k1s-dev-a-k1s-core-ha-controller 9108:9108 &
curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_controller_authority_healthy|ae_controller_is_leader'
```

### Roll back the local node target

If a retarget to another namespace fails, remove the local node and reinstall it against the previous release values.

```bash
helm uninstall k1s-dev-a-node --namespace k1s-dev-a
helm upgrade --install k1s-dev-a-node ops/helm/k1s-node-local \
  --namespace k1s-dev-a \
  -f .local/helm/k1s-dev-a.node.yaml
```

### Roll back Host A bootstrap

- Preferred VM path:
  - restore the previous env bundle or VM snapshot
  - restart only edge-core services
- Fallback in-guest path:
  - restore the prior `.env` content
  - restart edge-core services without touching the GPU compute node

### Roll back the edge gateway

```bash
helm history k1s-dev-a-edge --namespace k1s-dev-a
helm rollback k1s-dev-a-edge <revision> --namespace k1s-dev-a
kubectl -n k1s-dev-a rollout status deploy/k1s-dev-a-edge-k1s-edge-gateway
```

Post-rollback checks:

```bash
curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_site_gateway_last_seen_seconds|ae_route_bundle_pending|ae_route_bundle_ack_age_seconds'
kubectl -n k1s-dev-a logs deploy/k1s-dev-a-edge-k1s-edge-gateway -c rathole-client --tail=100
```

## Host A Topology Guidance

Use a separate small VM if Host A can spare it.

- Recommended envelope:
  - 2 vCPU
  - 2 GiB RAM minimum
  - 4 GiB RAM preferred
  - 16-20 GiB disk
  - virtio NIC only
  - no GPU passthrough
- Why:
  - isolates tunnel and gateway churn from CUDA workloads
  - lets you kill edge-core without destroying the GPU guest
  - keeps rootful WG and Rathole changes inside a guest boundary

If Host A cannot spare that VM, run edge-core inside the existing GPU VM with strict limits:

- separate systemd unit names
- separate state roots
- CPU and memory caps
- no direct deployment on the Host A host OS

## Parallel Namespace Stacks

You can install multiple `k1s-core-ha` releases into separate namespaces at the same time. Only one namespace may own the Host B GPU node because `k1s-node-local` is intentionally exclusive cluster-wide.
