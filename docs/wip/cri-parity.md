# CRI parity tracking (containerd)

Last updated: 2026-02-12

Goal: make CRI/containerd a safe default backend for k1s dev/labs with a
Kubernetes-aligned registry-first image flow and core runtime parity vs Podman.

## Current parity snapshot (done)

- Runtime adapter core: create/update/remove pods, init/sidecars, exec sync, logs.
- Streaming exec/attach/port-forward via node agent + crictl.
- Registry-first flow: in-cluster NodePort registry, TLS+htpasswd, delete enabled.
- CRI image helpers and registry HTTP helpers (`ae cri images`, `ae cri registry`).
- Build pipeline without Podman (buildkit-only pod + crictl-only ops pod).
- HostPath + emptyDir semantics, hostAliases, DNS config, security context mapping.
- CRI smoke + NodePort smoke scripts (gated for CRI nodes).
- NetFS harness: smoke + snapshot/clone + CSI path green (with heartbeat loop in harness).
- Node agent returns Pending pod state when PVC mounts are not ready (avoids hard 500s).
- Strict profile infra lane: when `AE_RUNTIME_BACKEND=cri` profiles can run core/edge
  dev infra as CRI pods (`AE_INFRA_BACKEND=cri`) instead of compose/Podman.
  - strict lane defaults `AE_CRI_RUNTIME_HANDLER=runc`
  - host-network sandboxes omit explicit hostname by default (prevents runc UTS errors)
  - rathole strict-CRI launch uses image entrypoint + args (`--server` / `--client`)
  - strict `k1s-core` now supports `AE_APISHIM_MODE=cri` (`k1s-core-apishim` CRI pod)
  - missing strict-CRI images support interactive build/pull prompt (`AE_CRI_IMAGE_POLICY`)
  - strict-CRI fallback action `b` uses local build + registry push + CRI pull verify
    (backend order: nerdctl, podman, docker, ctr; override via `AE_CRI_LOCAL_BUILD_BACKEND`)
  - strict-CRI image refs can target existing dev registries via `AE_CRI_REGISTRY`

## P0: Required for CRI as default backend (dev/labs)

- [x] CRI runtime adapter core (pods, init/sidecars, logs, exec sync).
  - Evidence: `src/ae/runtime/cri_runtime.py`
- [x] Streaming exec/attach/port-forward via node agent + crictl.
  - Evidence: `src/ae/runtime/remote_runtime.py`, `src/ae/runtime/cri_runtime.py`
  - Tests: `tests/integration/test_agent_streaming_proxy.py`, `tests/integration/test_apishim_agent_streaming.py`
- [x] Registry-first flow with in-cluster NodePort registry + TLS/htpasswd + delete enabled.
  - Evidence: `ops/dev/registry-k8s.yaml`, `CRI-REG.md`, `docs/reference/cri-containerd.md`
- [x] Containerd trust helper for TLS/insecure registries.
  - Evidence: `scripts/containerd_registry_trust.sh`, `ae cri trust` in `src/ae/cli/__main__.py`
- [x] CRI image helpers + registry HTTP helpers (list/pull/rm/tag/delete).
  - Evidence: `src/ae/cli/__main__.py`
- [x] Build pipeline without Podman (buildkit-only pod + crictl-only ops pod), built/pushed on demo/labs.
  - Evidence: `scripts/build_cri_buildkit_image.sh`, `scripts/build_cri_crictl_image.sh`, `scripts/init_demo.sh`
  - Manifests: `specs/examples/cri-buildkit-k8s.yaml`, `specs/examples/cri-crictl-k8s.yaml`
- [x] Socket access helper for non-root dev sessions.
  - Evidence: `scripts/containerd_socket_access.sh`, `docs/reference/cri-containerd.md`
- [x] Service exposure for CRI nodes (NodePort/ClusterIP via iptables proxy, optional).
  - Evidence: `src/ae/network/provider_iptables.py`, `src/ae/controller/__main__.py`, `docs/ops/runbook.md`
- [x] CRI smoke coverage (basic + NodePort) gated.
  - Evidence: `scripts/cri_smoke.sh`, `scripts/cri_nodeport_smoke.sh`, `scripts/multinode_nodeport_smoke.sh`

## P1: Next parity items (close common chart gaps)

- [~] PVC/PV/StorageClass controller parity (Pending/Bound/Released, default StorageClass).
  - Updates: finalizers + reclaim transitions for PV/PVC; PV delete marks PVC Lost.
  - Added: explicit PVC volumeName now blocks dynamic provisioning when PV is missing/conflicting.
  - Added: PVC resize conditions (`Resizing`, `FileSystemResizePending`) on expansion paths.
  - See: `docs/wip/storage-parity.md`
- [~] NetFS mount lifecycle coverage on CRI nodes (NFS/SMB), PVC->mount reconciliation in apishim.
  - Added: `scripts/netfs_smoke_suite.sh` to run smoke + snapshot/clone + CSI harness.
  - Current state: harness green; production parity still depends on PVC/PV controller work above.
- [~] CSI external provisioner hook + VolumeSnapshot/clone parity.
  - See: `docs/wip/csi.md`
  - Key workstreams (plan after testing):
    1. CSI driver registry + config (provisioner → driver endpoint/secret mapping; CSIDriver/CSINode in apishim).
    2. Controller-side lifecycle (Create/Delete volume, ControllerPublish/Unpublish; keep VolumeAttachment + VolumeSnapshotContent synced).
    3. Node-side publish (NodeStage/NodePublish orchestration + mount records).
    4. Snapshot/restore path (VolumeSnapshot → Content → PVC restore for CSI-backed volumes).
    5. Integration tests (minimal CSI driver fixture + e2e harness).
- [x] StatefulSet volumeClaimTemplates per-ordinal PVC creation + mount naming (adapter + NetFS resolver).
- [ ] Multi-node service proxy with podCIDR auto-discovery (reduce manual AE_POD_CIDR use).

## P2: Longer-term parity (k8s conformance adjacent)

- [ ] NetworkPolicy enforcement.
- [ ] Node/Lease heartbeats + eviction semantics.
- [ ] metrics.k8s.io.
- [ ] Admission webhooks / Pod Security Admission.
- [ ] Server-side apply managedFields parity.

## P0 verification (2026-01-30)

All P0 items are present in code and documented. Smoke and integration tests are
available but remain gated for CRI-capable nodes. The remaining blockers for a
CRI-default experience are storage parity items (PVC/PV/StorageClass) and CSI
integration tracked in P1.

## Recent findings (2026-01-31)

- NetFS snapshot/clone failures traced to node readiness expiring during long snapshot runs.
- Fix: harness now runs a heartbeat loop to keep the node Ready throughout the suite.
- Added: node agent surfaces Pending pod state when PVC mounts are not ready, preventing 500s.

---

# Registry + Image Flow (moved from CRI-REG.md)

This section captures the CRI/containerd registry and image workflows used by
k1s. It is the canonical reference for CRI image flows.

## Scope and goals

- Build images with Podman (rootless or rootful).
- Make those images available to containerd (CRI) for `crictl pull` or direct
  import.
- Keep dev/demo teardown consistent with Podman/Docker: `scripts/stop_all.sh`
  and `make demo-down` stop/remove CRI pods/containers created by k1s.

## Quick recommendation (default path)

**Preferred flow (registry):** Podman build -> push to registry -> CRI pull.

This is the most portable workflow and aligns with production patterns. It also
works well with remote nodes.

## K8s-aligned flow (recommended)

**Principle:** the registry is the source of truth; kubelet/CRI pulls on demand.

**Flow**
1. Build/push images to a registry reachable by every node.
2. Reference those images in manifests (with a stable tag or digest).
3. Let the CRI runtime pull when workloads schedule (standard k8s behavior).
4. Optional pre-warm: pull images at cluster-up via a DaemonSet or a one-shot
   `crictl pull` hook to reduce first-start latency.

**Why this is aligned**
- Matches kubelet image pull semantics and multi-node reality.
- Avoids host-local image imports that don't scale.
- Keeps image references stable across dev, CI, and prod.

**Registry placement rule**
- If the registry runs *inside* the cluster, it must still be **host-reachable**
  by containerd on every node (NodePort/hostNetwork). Cluster-only DNS/Service
  names are not visible to host-level containerd unless you route them explicitly.
- If host reachability is not guaranteed, prefer an external/host-local registry
  and push images there directly.

**In-cluster registry manifest (host-reachable)**
- Example manifest: `ops/dev/registry-k8s.yaml`
- Default hostname: `registry.k1s.home.arpa`
  - NodePort: `registry.k1s.home.arpa:32000` (default nodePort in the manifest)
  - For single-node dev, add it to `/etc/hosts` on the host.
  - For multi-node, use real DNS pointing at the node running the registry.

## Terminology and defaults

- CRI endpoint: `AE_CRI_ENDPOINT` (default `unix:///run/containerd/containerd.sock`)
- Containerd namespace: commonly `k8s.io` for CRI
- Registry host: `AE_REGISTRY_HOST` (demo uses `localhost:5001`)
- Registry credentials: `~/.config/ae/registries.yaml` (used by CRIRuntime auth)

## Option A: Podman build -> push -> crictl pull (registry flow)

**Steps**
1. Build with Podman:
   - `podman build -t <registry>/<repo>:<tag> <context>`
2. Push to registry:
   - `podman push <registry>/<repo>:<tag>`
3. Pull from CRI:
   - `crictl --runtime-endpoint "$AE_CRI_ENDPOINT" pull <registry>/<repo>:<tag>`

**Pros**
- Mirrors production CI/CD flow.
- Works across nodes and hosts.
- CRI runtime sees the exact registry reference used by manifests.

**Cons**
- Requires registry reachability and auth config.
- For local HTTP registries, containerd requires explicit trust settings.

**Notes**
- If using the local demo cache (`localhost:5001`), containerd must trust the
  registry (see "Registry trust and mirrors" below).
- k1s CRI adapter uses registry auth from `~/.config/ae/registries.yaml` when
  pulling images.
- `crictl pull` is a **pre-warm** convenience; in k8s the runtime pulls on demand
  when the workload schedules.
- For local tags like `demo-green:latest`, either update manifests to reference
  a registry-qualified image (for example: `<registry>/demo-green:latest`) or
  configure a containerd mirror for Docker Hub to point at your local registry.

## Option B: Podman build -> save -> ctr import (no registry)

**Steps**
1. Build with Podman:
   - `podman build -t demo-green:latest samples/servers/green`
2. Export and import:
   - `podman save --format docker-archive -o /tmp/demo-green.tar demo-green:latest`
   - `sudo ctr -n k8s.io images import /tmp/demo-green.tar`

**Pros**
- No registry required.
- Simple for local dev and air-gapped environments.

**Cons**
- Manual and host-local.
- Does not scale to remote nodes.

## Option C: Podman build -> OCI archive -> ctr import

**Steps**
1. Build with Podman:
   - `podman build -t demo-green:latest samples/servers/green`
2. Export as OCI:
   - `podman save --format oci-archive -o /tmp/demo-green.oci demo-green:latest`
3. Import to containerd:
   - `sudo ctr -n k8s.io images import /tmp/demo-green.oci`

**Pros**
- OCI archive is widely supported.
- Works without a registry.

**Cons**
- Same limitations as Option B (local only).

## Option D: Build directly in containerd (nerdctl + buildkit)

**Steps**
1. Install buildkit (`buildctl` + `buildkitd`).
2. Build directly:
   - `sudo nerdctl -n k8s.io build -t demo-green:latest samples/servers/green`

**Pros**
- Images land directly in containerd’s store.
- No extra import steps.

**Cons**
- Requires buildkitd running.
- Adds buildkit dependencies to dev hosts.

## Registry trust and mirrors (containerd)

Containerd requires explicit config for insecure registries or custom CAs.
Configure per-registry at `/etc/containerd/certs.d/<host>/hosts.toml`.

CLI shortcut (wraps the helper script):
```
ae cri trust --host registry.k1s.home.arpa:32000 --ca /tmp/registry.crt --restart
```

**HTTP / insecure local registry**
```
server = "http://localhost:5001"
[host."http://localhost:5001"]
  capabilities = ["pull", "resolve", "push"]
  skip_verify = true
```

**HTTPS with custom CA**
```
server = "https://registry.example.com"
[host."https://registry.example.com"]
  capabilities = ["pull", "resolve", "push"]
  ca = "/etc/ssl/certs/registry.example.com.crt"
```

After changes, restart containerd:
```
sudo systemctl restart containerd
```

## Registry auth for CRI pulls

k1s CRI adapter reads registry credentials from:
```
~/.config/ae/registries.yaml
```

Example:
```yaml
registry.example.com:
  username: "user"
  password: "token-or-password"
docker.io:
  username: "user"
  password: "token"
```

## k1s registry helpers (HTTP API)

```
ae cri registry list --registry registry.k1s.home.arpa:32000
ae cri registry tags <repo> --registry registry.k1s.home.arpa:32000
ae cri registry manifest <repo>:<tag> --registry registry.k1s.home.arpa:32000
ae cri registry tag <repo>:<tag> <repo>:<new-tag> --registry registry.k1s.home.arpa:32000
ae cri registry delete <repo>:<tag> --registry registry.k1s.home.arpa:32000 --force
ae cri registry push --context /path/to/context --image registry.k1s.home.arpa:32000/app:tag
```

## Secure local registry cache (TLS + auth)

If you want the dev registry cache to work with CRI without an insecure registry
entry, enable TLS (and optional htpasswd auth).

**Generate certs (dev)**
```
mkdir -p state/registry-certs
openssl req -x509 -newkey rsa:4096 -sha256 -days 365 -nodes \
  -keyout state/registry-certs/registry.key \
  -out state/registry-certs/registry.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```

**Create htpasswd (optional)**
```
mkdir -p state/registry-auth
htpasswd -Bbc state/registry-auth/htpasswd <user> <pass>
```

**Enable in dev**
```
AE_REGISTRY_TLS=1 AE_REGISTRY_AUTH=1 ./scripts/init_demo.sh
```

If auth is enabled, log in with your container engine so the dev stack can pull
through the cache (for example: `podman login <host>` or `docker login <host>`).
Use `AE_REGISTRY_AUTH_REALM` to override the htpasswd realm if needed.

## Secure in-cluster registry (TLS + auth)

The `ops/dev/registry-k8s.yaml` manifest expects two secrets:

**Create TLS secret (dev)**
```
openssl req -x509 -newkey rsa:4096 -sha256 -days 365 -nodes \
  -keyout /tmp/registry.key \
  -out /tmp/registry.crt \
  -subj "/CN=registry.k1s.home.arpa" \
  -addext "subjectAltName=DNS:registry.k1s.home.arpa,IP:127.0.0.1"
kubectl -n k1s-registry create secret tls registry-tls \
  --cert=/tmp/registry.crt --key=/tmp/registry.key
```

**Create htpasswd secret (dev)**
```
htpasswd -Bbc /tmp/htpasswd k1s test123
kubectl -n k1s-registry create secret generic registry-auth \
  --from-file=htpasswd=/tmp/htpasswd
```

**Apply the manifest**
```
kubectl apply -f ops/dev/registry-k8s.yaml
```

Then ensure containerd trusts the CA and uses the registry hostname in
`/etc/containerd/certs.d/registry.k1s.home.arpa:32000/hosts.toml`. You can use:
```
scripts/containerd_registry_trust.sh \
  --host registry.k1s.home.arpa:32000 \
  --ca /tmp/registry.crt \
  --restart
```

**Trust the CA (host runtimes)**
- Podman: `~/.config/containers/certs.d/<host>/ca.crt`
- Docker: `/etc/docker/certs.d/<host>/ca.crt`
- containerd: `/etc/containerd/certs.d/<host>/ca.crt`

Then configure `/etc/containerd/certs.d/<host>/hosts.toml` to use the HTTPS
endpoint and CA (see "Registry trust and mirrors" above). If auth is enabled,
ensure `~/.config/ae/registries.yaml` has credentials for the registry.

Note: the k8s registry manifest enables deletes by default
(`REGISTRY_STORAGE_DELETE_ENABLED=true`) so remote helpers can delete manifests.

## Local demo registry cache

The demo stack can run a pull-through cache (`registry:2`) at
`AE_REGISTRY_HOST` (default `localhost:5001`). Podman/Docker can use this
directly, but containerd will only pull from it if configured via
`/etc/containerd/certs.d/<host>/hosts.toml`.

**Recommended dev settings**
- `AE_REGISTRY_HOST=localhost:5001`
- containerd hosts config for `localhost:5001` (see above)

**Important:** the dev cache is a host-local service. If you run a registry
*inside* the k1s cluster, containerd on the host cannot reach it via
`localhost` or cluster DNS. Expose it via NodePort/hostNetwork and set
`AE_REGISTRY_HOST` to that host-reachable endpoint, or disable the in-cluster
cache and push directly to a host-reachable registry instead.

## Teardown parity for CRI

**Goal:** when `scripts/stop_all.sh` or `make demo-down` runs, CRI pods/containers
created by k1s should be stopped/removed (like Docker/Podman).

**Implementation**
- `scripts/cri_teardown.sh` lists CRI pods via `crictl pods -o json`, filters
  by the `ae.app` label, then stops/removes only those pods.
- `scripts/stop_all.sh` and `scripts/init_demo.sh --down` call the helper
  (best‑effort) so CRI cleanup happens automatically in dev flows.

## Suggested integration hooks

Helpers and follow-ups to reduce manual steps:

- `AE_CRI_IMAGE_SYNC=1` (opt-in) in `scripts/init_demo.sh`:
  - Build/collect demo images,
  - Push to registry,
  - `crictl pull` into containerd.
  - Rewrite demo specs for `demo-green`/`demo-shell` to use the registry host.
  - Defaults `AE_REGISTRY_HOST` to `registry.k1s.home.arpa:32000` for CRI runs if unset.
- `AE_CRI_PREWARM=1` (opt-in) in `scripts/init_demo.sh`:
  - Pre-pull images via CRI using `AE_CRI_PREWARM_IMAGES` (space-separated list).
- `AE_CRI_BUILDKIT_BUILD=1` and `AE_CRI_CRICTL_BUILD=1` (default for CRI runs):
  - Build and push the buildkit-only + crictl-only images to `AE_REGISTRY_HOST`.
- `AE_CRI_TOOLBOX_BUILD=1` (optional):
  - Build and push the toolbox image for advanced/manual workflows.

Example:
```
AE_RUNTIME_BACKEND=cri \
AE_USE_REGISTRY_CACHE=0 \
AE_REGISTRY_HOST=registry.k1s.home.arpa:32000 \
AE_CRI_IMAGE_SYNC=1 \
./scripts/init_demo.sh
```
- `scripts/cri_image_sync.sh` helper:
  - Accepts image list and registry host.
  - Handles push + CRI pull.
- Update `scripts/stop_all.sh` and `scripts/init_demo.sh --down`:
  - Stop/remove CRI pods labeled `ae.app`.

## Pre-warm options

- Host-level (single node): `scripts/cri_image_prewarm.sh` to `crictl pull` a list of images.
- Host-level (with registry sync): `scripts/cri_image_sync.sh` to push and `crictl pull`.
- K8s-aligned (multi-node): apply `specs/examples/cri-image-prewarm-k8s.yaml`
  to run a DaemonSet that pre-pulls images via an initContainer and keeps a
  minimal pause container running. This default uses a bundled crictl-only image.
  For a hostPath `crictl` variant, use `specs/examples/cri-image-prewarm-k8s-hostpath.yaml`.
  Note: DaemonSets are emulated by the k1s shim; this pre-warm manifest is
  intended for real Kubernetes clusters.

## CRI crictl-only image (prewarm and ops)

The default pre-warm DaemonSet uses a minimal image that only contains `crictl`.
Build and push it to your registry:

```
AE_REGISTRY_HOST=registry.k1s.home.arpa:32000 \
scripts/build_cri_crictl_image.sh --push
```

You can also run a short-lived crictl pod for manual ops:
`specs/examples/cri-crictl-k8s.yaml`.

## CRI buildkit-only image (builds)

The default `ae cri build` flow uses a buildkit-only image that does not mount
the host containerd socket. Build and push it:

```
AE_REGISTRY_HOST=registry.k1s.home.arpa:32000 \
scripts/build_cri_buildkit_image.sh --push
```

Manifest for the build pod: `specs/examples/cri-buildkit-k8s.yaml`.

## CRI toolbox image (crictl + nerdctl + buildkit)

The toolbox image bundles `crictl`, `nerdctl`, `buildctl`, and `buildkitd`.
Use it for advanced/manual workflows (direct containerd builds). Build and push:

```
AE_REGISTRY_HOST=registry.k1s.home.arpa:32000 \
scripts/build_cri_toolbox_image.sh --push
```

You can also override versions via `CRICTL_VERSION`, `NERDCTL_VERSION`,
`BUILDKIT_VERSION` when building the image.

## Security considerations (toolbox/buildkit)

Using the toolbox pod mounts the host containerd socket. That is effectively
**node-root**: anyone who can `exec` into the pod can run privileged containers,
access host images, or manipulate workloads via containerd.

BuildKit itself is not the risky part; the **socket mount and exec access** are.
Keep it locked down, especially if build contexts or Dockerfiles are untrusted.
The crictl-only image reduces tooling but still mounts the same socket.

**Recommended guardrails**
- Run in a dedicated namespace (`k1s-tools`) with tight RBAC.
- Do not expose the pod via Service; use short-lived pods and delete after use.
- Keep `automountServiceAccountToken: false` (already set).
- Avoid `hostNetwork` / `hostPID` / extra hostPath mounts.
- Prefer a dedicated, tainted node for the toolbox if multi-tenant.
