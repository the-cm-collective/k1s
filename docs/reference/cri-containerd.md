# CRI containerd

This page documents strict CRI workflows in k1s (containerd + CRI), the
registry-first image flow, and current CRI tooling.

For current dev and docs/lab lanes, CRI is best exercised with `k1s-*` profile
targets. Use raw env exports only when debugging or overriding defaults.

## Recommended entrypoints (k1s-* profile targets)

Use these targets first:

```
make k1s-core-cri
make k1s-edge-cri
make k1s-core-edge-cri
make k1s-edge-core-cri
make edge-site-cri SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```

Notes:
- If your containerd setup requires elevated privileges, run strict CRI profile
  targets with `sudo -E`.
- `k1s-core-cri` and related targets wire `AE_RUNTIME_BACKEND=cri` and
  `AE_INFRA_BACKEND=cri` for you.
- Detailed profile behavior: `docs/guides/runtime-profiles.md`.
- End-to-end core/edge CRI runbook: `docs/guides/multinode-lab.md`.

## Ingress deep validation lanes (recommended)

Use `k1s-*` profile targets and keep ingress matrix runs mode-isolated.

Preflight before deep/deep+perf lanes:

```bash
sudo -v
scripts/dev/etcd_maintenance.sh watchdog
```

Optional guided wrapper with restart checkpoints:

```bash
scripts/dev/run_ingress_mode_lanes.sh --lanes all
```

Core-proxy mini sanity:

```bash
CORE_PROXY_FORCE_RATHOLE_RESTART=0 scripts/dev/test_ingress_matrix_single_host.sh \
  --modes core-proxy --archetypes ws-echo --tier tier2 --validation-profile standard
```

Primary release lane (`core-proxy`, policy + observability):

```bash
CORE_PROXY_FORCE_RATHOLE_RESTART=0 scripts/dev/test_ingress_matrix_single_host.sh \
  --modes core-proxy --archetypes ws-echo,lb-distribution,sticky-cookie \
  --tier tier2 --validation-profile deep+perf --perf-profile sample --lb-proof-scope auto
```

Strict LB proof lane (`edge-local`, separate stack start):

```bash
scripts/dev/test_ingress_matrix_single_host.sh \
  --modes edge-local --archetypes lb-distribution --tier tier2 --validation-profile deep \
  --lb-proof-scope edge-only --lb-sample-requests 5000 --lb-min-backends 2 \
  --lb-max-skew-ratio 0.35 --edge-local-listener-url https://lb-distribution-edge-local.home.arpa:443/
```

Interpretation:
- `core-proxy` LB rows are `assertion_level=policy_switch_only` when strict distribution is not required.
- `edge-local` LB rows are `assertion_level=strict_distribution` and are the strict proof source.
- Full sequence and failure triage: `docs/guides/ingress-capability-test-sequence.md`.

## Runtime selection and advanced overrides

If you need manual runtime overrides outside profile helpers:

```
export AE_RUNTIME_BACKEND=cri
export AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock
```

Defaults:
- CRI endpoint: `AE_CRI_ENDPOINT` (default `unix:///run/containerd/containerd.sock`)
- CRI sandbox image: `AE_CRI_SANDBOX_IMAGE` (pause image)
- Host namespaces gate: `AE_CRI_ALLOW_HOST_NS=1` to enable hostNetwork/hostPID/hostIPC/shareProcessNamespace

## Registry-first image flow (Kubernetes-aligned)

- Build images and push to a registry reachable by every node.
- Reference those images in manifests.
- Let containerd pull on demand (kubelet behavior).
- Optionally pre-warm to reduce first-start latency.

This avoids host-local imports and works across nodes.

## Registry options

### In-cluster registry (NodePort)

Use the in-cluster registry manifest and expose it via NodePort so host
containerd can reach it:

```
openssl req -x509 -newkey rsa:4096 -sha256 -days 365 -nodes \
  -keyout /tmp/registry.key \
  -out /tmp/registry.crt \
  -subj "/CN=registry.k1s.home.arpa" \
  -addext "subjectAltName=DNS:registry.k1s.home.arpa,IP:127.0.0.1"
kubectl -n k1s-registry create secret tls registry-tls \
  --cert=/tmp/registry.crt --key=/tmp/registry.key

htpasswd -Bbc /tmp/htpasswd k1s test123
kubectl -n k1s-registry create secret generic registry-auth \
  --from-file=htpasswd=/tmp/htpasswd

kubectl apply -f ops/dev/registry-k8s.yaml
```

Defaults:
- Hostname: `registry.k1s.home.arpa`
- NodePort: `32000`
- Full host: `registry.k1s.home.arpa:32000`

The manifest enables TLS + htpasswd auth and deletes:
`REGISTRY_STORAGE_DELETE_ENABLED=true`.
For single-node dev, add `registry.k1s.home.arpa` to `/etc/hosts`.

If you point strict CRI lanes at this in-cluster registry, disable the local
pull-through cache (`AE_USE_REGISTRY_CACHE=0`) so demo/dev image pulls do not
route through a conflicting cache endpoint.

### Local dev registry cache

The dev stack can run a pull-through cache at `AE_REGISTRY_HOST`
(default `localhost:5001`). Containerd can only use it if you configure
trust in `/etc/containerd/certs.d/<host>/hosts.toml`.

### MicroK8s registry (common dev case)

If `microk8s enable registry` is used on the same host, it typically exposes
`localhost:32000`. You can point k1s at it:

```
AE_REGISTRY_HOST=localhost:32000
AE_USE_REGISTRY_CACHE=0
```

Then trust it with:

```
scripts/containerd_registry_trust.sh --host localhost:32000 --scheme http --insecure --restart
```

## Trust and auth (containerd)

Containerd requires explicit trust config for insecure registries or custom CA.
Use the helper to write `/etc/containerd/certs.d/<host>/hosts.toml`:

```
scripts/containerd_registry_trust.sh \
  --host registry.k1s.home.arpa:32000 \
  --ca /tmp/registry.crt \
  --restart
```

Registry auth for CRI pulls is read from:
`~/.config/ae/registries.yaml`.

### Image pull policy and secrets

- `spec.imagePullPolicy` is honored by the CRI runtime:
  - `Always` pulls on each (re)create.
  - `IfNotPresent` pulls only if the image is missing.
  - `Never` refuses to pull and requires the image to exist locally.
  - Default aligns with Kubernetes: `:latest` (or no tag) → `Always`, otherwise `IfNotPresent`.
- `spec.imagePullSecrets` and `spec.registryAuthRef` are treated as registry host keys
  in `~/.config/ae/registries.yaml` and are preferred when pulling images via CRI.
- When apishim storage is available (`AE_APISHIM_DB` or `AE_APISHIM_DSN`), the CRI
  runtime also reads Kubernetes secrets of type `kubernetes.io/dockerconfigjson`
  (or `.dockercfg`) referenced by `imagePullSecrets` and uses those creds for pulls.
- If the pod spec omits `imagePullSecrets`, the CRI runtime will look up the
  ServiceAccount for the originating Kubernetes workload (via apishim storage)
  and use any `imagePullSecrets` defined there.
- If a manifest was converted from Kubernetes (via apishim), the converted spec
  stores `serviceAccountName` and CRI will prefer that ServiceAccount when
  resolving pull secrets.
- Kubernetes conversion defaults `serviceAccountName` to `default` when unset,
  matching kubelet behavior for pull secret resolution.
- Kubernetes conversion also carries `imagePullSecrets` from the pod template into
  the k1s spec so CRI pulls can use them without apishim lookups.
- If exactly one pull secret is present, conversion also sets `registryAuthRef`
  to that secret name for convenience.
- Kubernetes conversion also maps `imagePullPolicy` from the pod template into the
  k1s spec (main container + sidecars), so CRI honors explicit pull policy settings.
- ServiceAccount propagation applies to converted Deployment/StatefulSet/DaemonSet/Job/CronJob
  pod templates (best-effort when templates are present).
- When apishim storage is available, converted workloads can inherit
  ServiceAccount `imagePullSecrets` only if the pod template omits the field
  entirely (an explicit empty list disables the fallback, matching Kubernetes behavior).

## Image prep and prewarm

### Prepare example images for strict CRI (recommended)

For registry-first strict CRI lanes, prebuild/mirror example images into your
registry before running docs/lab manifests:

```
scripts/dev/examples_registry_prepare.sh \
  --registry "${AE_CRI_REGISTRY:-${AE_REGISTRY_HOST}}" \
  --namespace "${AE_CRI_REGISTRY_NAMESPACE:-k1s}" \
  --output-dir state/examples/registry \
  --pull-cri
```

This writes registry-rewritten manifests under `state/examples/registry/` (for
both `specs/examples` and `docs/site/examples`) so examples can be applied
without relying on upstream registries during the lab flow.

### Prewarm (host via crictl)

```
AE_CRI_PREWARM_IMAGES="mendhak/http-https-echo:37" scripts/cri_image_prewarm.sh
```

Or pass images explicitly:

```
scripts/cri_image_prewarm.sh --image registry.k1s.home.arpa:32000/demo-green:latest
```

### Prewarm (Kubernetes)

Apply the DaemonSet that pre-pulls images on every node:

```
kubectl apply -f specs/examples/cri-image-prewarm-k8s.yaml
```

This uses a minimal crictl-only image.
For a hostPath crictl variant, use
`specs/examples/cri-image-prewarm-k8s-hostpath.yaml`.
Note: DaemonSets are emulated by the k1s shim; this is intended for real
Kubernetes clusters.

### Key env vars

- `AE_CRI_ENDPOINT` for CRI endpoint selection.
- `AE_CRI_RUNTIME_HANDLER` for CRI runtime handler selection (default `runc` in strict profile targets).
- `AE_CRI_REGISTRY=<host:port>` to rewrite strict-CRI managed image refs to a registry.
- `AE_CRI_REGISTRY_NAMESPACE=<prefix>` to prepend a registry path segment (for example `k1s`).
- `AE_CRI_REGISTRY_MODE=managed|external|off`
  - `managed`: k1s starts/health-checks a local strict-CRI registry (default endpoint `https://localhost:5001`)
  - `external`: k1s validates external registry reachability and fails fast if unavailable
  - `off`: disable strict-CRI registry rewrite/readiness checks
- `AE_CRI_REGISTRY_PRESET=microk8s|local`
  - `microk8s`: sets `AE_CRI_REGISTRY_MODE=external` + default `AE_CRI_REGISTRY=localhost:32000`
  - `local`: sets `AE_CRI_REGISTRY_MODE=managed`
- `AE_CRI_MANAGED_REGISTRY_TLS=1|0` (default `1`) enables/disables managed-registry TLS.
- Managed TLS material defaults under `state/profiles/<profile>/registry/tls` and is auto-generated when missing.
- `AE_CRI_REGISTRY_INSECURE=1` is a dev-only opt-out for plaintext/insecure registry access.
- `AE_CRI_REGISTRY_TRUST=1` to apply trust configuration during strict CRI startup (auto-enabled for managed TLS).
- `AE_CRI_REGISTRY_TRUST_CA=/path/to/ca.crt` or `AE_CRI_REGISTRY_TRUST_INSECURE=1` (dev).
- `AE_CRI_REGISTRY_TRUST_SCHEME=http` to override scheme (default `https`).
- `AE_CRI_REGISTRY_TRUST_RESTART=1` to restart containerd after writing trust.
- `AE_CRI_IMAGE_POLICY=prompt|pull|fail` for strict-CRI missing-image behavior.
- `AE_CRI_LOCAL_BUILD_BACKEND=nerdctl|podman|docker` to pick strict-CRI local build backend for fallback action `b`.
- `AE_APISHIM_IMAGE=<image-ref>` to override strict-CRI apishim image.
- `AE_CRI_SET_HOSTNAME=1` to request explicit hostname wiring for CRI stack components.
- `AE_CRI_ALLOW_HOST_NS=1` to enable hostNetwork/hostPID/hostIPC/shareProcessNamespace (off by default).
- `AE_CRI_REQUIRE_RUNTIME_READY=1` / `AE_CRI_REQUIRE_NETWORK_READY=1` to make preflight fail hard when CRI conditions are not ready.

### Temporary socket access (dev)

If you want to run CRI operations without sudo, grant ACL access to the
containerd socket for the current user:

```
./scripts/containerd_socket_access.sh --grant
./scripts/containerd_socket_access.sh --revoke
```

The helper records the previous ACLs in `state/containerd.sock.acl` and restores
them on revoke. Note: if containerd restarts, the socket may be recreated and
you may need to re‑grant access.

## Volumes (hostPath + PVC)

- `spec.volumes` (hostPath) works the same as Podman/Docker for CRI nodes.
- Kubernetes workloads applied via apishim map:
  - `volumes[].hostPath` → `spec.volumes` (mountPath + readOnly preserved).
  - `volumes[].persistentVolumeClaim` → `spec.pvcMounts` (requires NetFS on nodes).
  - `volumeDevices` → `spec.pvcMounts[].devicePath` for block volumes.
- StatefulSet `volumeClaimTemplates` create per‑ordinal PVCs (`<tpl>-<sts>-<ordinal>`) and
  mount via `claimTemplate` entries.
- `spec.emptyDirs` map to per‑pod host directories on CRI nodes (ephemeral).
  - Defaults to `/var/lib/ae/emptydirs/<app>/<pod>/<name>`.
  - `medium: Memory` uses `/dev/shm/ae-emptydir` when available.
  - Override roots with `AE_CRI_EMPTYDIR_ROOT` / `AE_CRI_EMPTYDIR_TMPFS_ROOT`.
  - `sizeLimit` is best‑effort: enforced for `medium: Memory` when tmpfs mounts are available;
    disk‑backed emptyDirs do not enforce size limits.

Enable NetFS PVC resolution on the node agent:

```
export AE_ENABLE_NETFS=1
export AE_APISHIM_DB=state/apishim.db
```

If the apishim DB is not shared with nodes, point at the apishim API instead:

```
export AE_ENABLE_NETFS=1
export AE_APISHIM_URL=http://<controller-host>:8445
export AE_APISHIM_READ_TOKEN=<read-token>
```

## DNS + host aliases (CRI)

- `spec.dnsConfig` is mapped into the CRI sandbox `dns_config` (nameservers, searches, options).
- `spec.dnsPolicy` follows Kubernetes defaults (ClusterFirst / ClusterFirstWithHostNet) when
  `AE_CRI_CLUSTER_DNS` is set. Use `AE_CRI_CLUSTER_DNS` (space or comma delimited) and
  `AE_CRI_CLUSTER_DOMAIN` (default `cluster.local`) to enable cluster-style DNS search paths.
  `dnsPolicy=None` requires `dnsConfig` to be set.
- `spec.hostname`, `spec.subdomain`, and `spec.setHostnameAsFQDN` are mapped into the sandbox
  hostname (best-effort; FQDN requires `AE_CRI_CLUSTER_DOMAIN` to be meaningful).
- `spec.hostAliases` are applied by writing a per-pod `/etc/hosts` file under
  `/var/lib/ae/hosts` (override with `AE_CRI_HOSTS_ROOT`) and mounting it into
  each container. This is a best-effort substitute for kubelet's hostAlias behavior.

## Security context (CRI)

- Container security: `runAsUser`, `runAsGroup`, `readOnlyRootFilesystem`,
  `dropCapabilities`, `seccompProfileType` (+ `seccompLocalhostProfile`), and
  `apparmorProfile` map into CRI container security context.
- Pod security: `podSecurity.fsGroup` maps to supplemental groups, and
  `podSecurity.seccompProfileType`/`podSecurity.seLinux*` map into the sandbox
  security context.
- Host namespaces are gated by `AE_CRI_ALLOW_HOST_NS=1` as described above.

## Crictl-only image (default)

Build and push the crictl-only image:

```
AE_REGISTRY_HOST=registry.k1s.home.arpa:32000 \
  scripts/build_cri_crictl_image.sh --push
```

The prewarm DaemonSet uses this image by default. For a short-lived ops pod,
apply `specs/examples/cri-crictl-k8s.yaml`.

## Buildkit-only image (manual build pods)

The buildkit-only image runs buildkitd/buildctl without mounting the host
containerd socket. Use it for manual in-cluster build pods.

Build and push:

```
AE_REGISTRY_HOST=registry.k1s.home.arpa:32000 \
  scripts/build_cri_buildkit_image.sh --push
```

Manifest: `specs/examples/cri-buildkit-k8s.yaml`.

## Toolbox image (advanced)

The toolbox bundles crictl + nerdctl + buildkitd/buildctl. Use it for advanced
manual workflows (direct containerd builds).
Note: this is dev-only and effectively node-root when the containerd socket is mounted; use only on trusted lab nodes. Prefer the buildkit-only pod or CI builds for production pipelines.

Build and push:

```
AE_REGISTRY_HOST=registry.k1s.home.arpa:32000 \
  scripts/build_cri_toolbox_image.sh --push
```

Manifest: `specs/examples/cri-toolbox-k8s.yaml`.

No automatic image builds are performed by strict CRI profile targets; build
and push explicitly as part of your lane setup.

## Streaming exec/port-forward via node agent

For CRI setups, keep the containerd socket on the node and let the node agent
handle streaming exec/port-forward. Point apishim at the agent endpoint so the
API layer does not need direct CRI access:

```
export AE_APISHIM_AGENT_URL=http://<node>:9109
# or reuse the controller agent URL:
export AE_AGENT_URL=http://<node>:9109
```

If apishim cannot reach pod IPs (common in multi-node overlays), enable the
agent-backed port-forward path:

```
export AE_APISHIM_CRI_PORTFORWARD=1
# force it even when pod IPs are present:
export AE_APISHIM_CRI_PORTFORWARD_FORCE=1
```

## Service VIP + NodePort (iptables)

For Kubernetes-style ClusterIP/NodePort behavior on CRI nodes, enable the
iptables service proxy (requires root and `iptables` on PATH):

```
export AE_ENABLE_SERVICE_PROXY=1
export AE_SERVICE_PROVIDER=iptables
```

When `spec.service.ports[].nodePort` is set (Service type NodePort or
LoadBalancer), k1s programs NodePort DNAT rules to the same ready backends.

### NodePort smoke test

Run the NodePort smoke helper (requires root + iptables):

```
sudo scripts/cri_nodeport_smoke.sh
```

Defaults:
- host: `AE_NODE_ADVERTISE_IP` or `127.0.0.1`
- nodePort: `32080`
- path: `/healthz`

Override with `AE_CRI_NODEPORT_HOST`, `AE_CRI_NODEPORT_PORT`, and
`AE_CRI_NODEPORT_PATH`.

### Multi-node agent proxy

For Kubernetes-aligned NodePort/ClusterIP on *each node*, enable the agent
service proxy loop. This pulls service + endpoint snapshots from the controller
and programs iptables on the node (kube-proxy style):

```
export AE_AGENT_SERVICE_PROXY=1
export AE_AGENT_SERVICE_PROXY_URL=http://<controller>:9108
export AE_AGENT_SERVICE_PROXY_DB=/var/lib/ae/agent-services.db
# If API auth is enabled:
export AE_AGENT_SERVICE_PROXY_TOKEN=$AE_API_READ_TOKEN
```

Requires root or NET_ADMIN on the agent container, and `iptables` on PATH.

## Node agent DaemonSet (k8s-aligned)

For Kubernetes-aligned deployments, run the node agent as a DaemonSet with
host networking and a containerd socket mount:

```
scripts/build_node_image.sh --push \
  --registry registry.k1s.home.arpa:32000

kubectl apply -f specs/examples/k1s-node-daemonset-k8s.yaml
```

Update the manifest to set `AE_CONTROLLER_URL` (controller agent API) and
`AE_AGENT_TOKEN` (shared secret). The manifest defaults to CRI containerd; for
Podman/Docker nodes, change `AE_RUNTIME_BACKEND` and remove the socket mount.

## CRI command helpers

Profile and stack orchestration:
- `make k1s-core-cri`, `make k1s-edge-cri`, `make k1s-core-edge-cri`, `make k1s-edge-core-cri`
- `make edge-site-cri SITE_ID=<site> EDGE_PORT=<port> EDGE_HTTP_PORT=<port>`
- `python scripts/dev/cri_stack.py preflight`

Host helpers:
- `scripts/cri_preflight.sh`
- `scripts/cri_teardown.sh`
- `scripts/containerd_registry_trust.sh --host <host:port> ...`
- `scripts/dev/examples_registry_prepare.sh --registry <host:port> --pull-cri`
- `scripts/cri_image_prewarm.sh --image <ref>`

`crictl` quick checks:
- `crictl --runtime-endpoint "${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}" pods`
- `crictl --runtime-endpoint "${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}" ps`
- `crictl --runtime-endpoint "${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}" images`

Registry credentials for pulls are managed with `ae registry` and stored in
`~/.config/ae/registries.yaml`.

## Security notes

Mounting the host containerd socket is effectively node-root. Restrict exec
access, keep pods short-lived, and avoid extra hostPath mounts. For tighter
security, use the buildkit-only pod (no containerd socket) or build in CI and
push to the registry, then let CRI pull on demand.
