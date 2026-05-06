# MicroK8s Dev Stack

This lane packages `k1s` into a namespace-scoped MicroK8s dev stack on Host B while leaving shared cluster addons alone. It is a dev and failure-simulation surface, not the canonical multi-host HA evidence lane.

## Repo Assets

- Host B core chart: `ops/helm/k1s-core-ha`
- Host B local node chart: `ops/helm/k1s-node-local`
- Checked-in sample values:
  - `ops/helm/examples/k1s-core-ha-values.microk8s.yaml`
  - `ops/helm/examples/k1s-node-local-values.microk8s.yaml`
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

Build and push the four images the charts expect before installing the stack.

```bash
export REGISTRY_HOST=registry.k1s.home.arpa:32000
export TAG=dev

docker build -f ops/images/controller.Dockerfile -t ${REGISTRY_HOST}/k1s/k1s-core:${TAG} .
docker build -f ops/images/apishim.Dockerfile -t ${REGISTRY_HOST}/k1s/k1s-apishim:${TAG} .
docker build -f ops/images/node.Dockerfile -t ${REGISTRY_HOST}/k1s/k1s-core-node:${TAG} .

docker push ${REGISTRY_HOST}/k1s/k1s-core:${TAG}
docker push ${REGISTRY_HOST}/k1s/k1s-apishim:${TAG}
docker push ${REGISTRY_HOST}/k1s/k1s-core-node:${TAG}
```

If you need the Host A edge gateway image in the same registry for other flows, build and push `ops/images/gateway.Dockerfile` separately.

## Install

### 1. Create local overrides

Keep live values under `.local/helm/`.

```bash
mkdir -p .local/helm
cp ops/helm/examples/k1s-core-ha-values.microk8s.yaml .local/helm/k1s-dev-a.core.yaml
cp ops/helm/examples/k1s-node-local-values.microk8s.yaml .local/helm/k1s-dev-a.node.yaml
```

Edit at least these fields before install:

- `.local/helm/k1s-dev-a.core.yaml`
  - `stack.name`
  - `stack.domain`
  - `global.registryHost`
  - `bootstrap.controller.hostOverride`
  - `bootstrap.natsLeaf.hostOverride`
  - `bootstrap.rathole.hostOverride`
  - `auth.*` secrets
- `.local/helm/k1s-dev-a.node.yaml`
  - `target.namespace`
  - `target.controllerReleaseName`
  - `node.nodeSelector`

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
kubectl -n k1s-dev-a port-forward svc/k1s-dev-a-k1s-core-ha-controller 9108:9108 &
curl -fsS http://127.0.0.1:9108/healthz
curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_controller_authority_healthy|ae_controller_is_leader'
```

Expect exactly one leader and three controller pods.

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
kubectl -n k1s-dev-a get pods -o wide
kubectl -n k1s-dev-a logs ds/k1s-dev-a-node-k1s-node-local --tail=100
```

The chart is cluster-exclusive on purpose. A second `k1s-node-local` install should fail while the first exists.

### 6. Bring up Host A

Preferred path:

- Boot a small dedicated edge-core VM on Host A.
- Feed it `.local/host-a-edge-core.env`.
- Keep the existing GPU VM compute-only.

Fallback path:

- Source `.local/host-a-edge-core.env` inside the existing GPU VM.
- Start the edge-core role under separate systemd units with explicit CPU and memory caps.

## Removal

Remove in dependency order so the cluster does not keep orphaned GPU or site transport state.

### Remove the Host B local node first

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
