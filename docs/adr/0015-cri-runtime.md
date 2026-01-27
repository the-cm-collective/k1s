# ADR 0015 — CRI runtime (containerd)

Date: 2026-01-27
Status: Accepted
Owners: runtime/controller/ingress/ops

## Context
- k1s needs a CRI backend to run on containerd while preserving Kubernetes semantics.
- Existing runtime paths (Docker/Podman) assume host ports and local networks.
- CI and ops need a repeatable way to validate CRI readiness and integration tests.

## Decision
- Implement CRIRuntime with Kubernetes-aligned command/args, PodSandbox metadata, and pod IP endpoints.
- Provide CRI exec/attach/logs via crictl (node dependency) and optional port-forward proxy.
- Add CI workflow and preflight scripts to validate CRI readiness.

## Consequences
- CRI is a supported backend with CI coverage on containerd nodes.
- Streaming uses crictl today; a native CRI streaming proxy remains optional.

## Details

This doc covers:
- A CRI runtime skeleton and how to wire it into runtime selection.
- A field map from AppManifest to CRI PodSandbox/ContainerConfig.
- Storage + registry auth changes for containerd nodes.

It is intentionally minimal and matches the current code structure.

---

## Current status (2026-01-27)

Implemented:
- CRIRuntime adapter with CRI gRPC stubs (api_pb2/api_pb2_grpc) and codegen script.
- Backend wiring for `AE_RUNTIME_BACKEND=cri|containerd`.
- PodSandbox + main container lifecycle, image pull + auth, ExecSync, log reading via log_path.
- Command/args mapping follows Kubernetes semantics (command overrides entrypoint; args passed to entrypoint).
- PodSandbox UID included in log directory for kube-compatible log tooling.
- ReplicaState endpoint prefers pod IP + container port; hostPort only for replicas==1.
- Init containers (sequential, ephemeral sandbox) and sidecars in the same PodSandbox.
- HostPath storage manager for `spec.storage`.
- CNI preflight/smoke scripts and runbook updates.
- Reconciler endpoint selection (ingress + preStop) prefers pod IP endpoints when available.
- Pod port-forward now prefers pod IP + requested container ports, falling back to host ports only when pod IP is unavailable.
- CRI Service VIP provider using iptables NAT (single-node; requires root).
- Exec/attach streaming via crictl (requires crictl on node).
- Added CRI smoke pull test (gated by AE_CRI_SMOKE_PULL).
- Ingress reload inside container falls back to `crictl exec` on CRI backends.
- Added CRI lifecycle integration test (gated by AE_CRI_IT).
- CRI preflight now validates `RuntimeReady`/`NetworkReady`, and CI setup waits for readiness.
- CRI-native pod port-forward proxy via crictl (opt-in: `AE_APISHIM_CRI_PORTFORWARD=1`).

Remaining (next focus):
- None (optional future: CRI streaming proxy to replace crictl).

---

## 1) CRI runtime skeleton + wiring

### New module: `src/ae/runtime/cri_runtime.py`

Goal: implement the existing `RuntimeAdapter` interface against CRI gRPC.

High-level responsibilities:
- Translate `AppManifest` into PodSandbox/ContainerConfig.
- Reconcile replicas by creating/stopping/removing CRI pods/containers.
- Provide minimal exec (ExecSync) for probes and hooks.
- Provide best-effort logs and list_containers_info for status/ingress.

Suggested skeleton (pseudocode; not complete):

```python
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterable

from ae.controller.spec import AppManifest, app_key_for_manifest, runtime_labels_for_manifest
from ae.runtime.base import ReplicaState, RuntimeAdapter, RuntimeResult
from ae.runtime.ports import choose_host_port
from ae.runtime.registry import RegistryAuthProvider

# NOTE: you will need generated CRI gRPC stubs.
# Example layout:
#   src/ae/runtime/cri/api/runtime/v1/api_pb2.py
#   src/ae/runtime/cri/api/runtime/v1/api_pb2_grpc.py
#   src/ae/runtime/cri/api/runtime/v1/api.proto (vendored)
# See "Dependencies" below.

class CRIRuntime(RuntimeAdapter):
    APP_LABEL = "ae.app"
    REPLICA_LABEL = "ae.replica_id"
    REVISION_LABEL = "ae.revision"
    CONTAINER_LABEL = "ae.container"
    JOB_ATTEMPT_LABEL = "ae.job_attempt"

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        registry_auth: RegistryAuthProvider | None = None,
        sandbox_image: str | None = None,
        node_id: str | None = None,
    ) -> None:
        self._endpoint = endpoint or os.getenv(
            "AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock"
        )
        self._sandbox_image = sandbox_image or os.getenv(
            "AE_CRI_SANDBOX_IMAGE", "registry.k8s.io/pause:3.9"
        )
        self._registry = registry_auth or RegistryAuthProvider()
        self._current_node_id = node_id
        # TODO: create runtime + image service stubs
        # self._runtime = RuntimeServiceStub(channel)
        # self._images = ImageServiceStub(channel)

    # --- RuntimeAdapter API ---
    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        replica_ids: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
        app = app_key_for_manifest(manifest)
        desired = (
            list(replica_ids)
            if replica_ids is not None
            else [f"{app}-rev{revision}-{i}" for i in range(manifest.spec.replicas)]
        )
        self._current_node_id = node_id

        # 1) List existing sandboxes/containers by label
        # existing = self._list_app_pods(app)
        # by_replica = {labels[REPLICA_LABEL]: pod for pod in existing}
        # old = [pod for pod in existing if labels[REVISION_LABEL] != str(revision)]

        created = updated = removed = 0

        # 2) Pull image if missing
        # self._ensure_image(manifest.spec.image)

        # 3) Create / start missing pods
        for rid in desired:
            # if rid not in by_replica:
            #   self._run_pod(manifest, rid, revision)
            #   created += 1
            # else: ensure main container running; if job retry needed, recreate
            pass

        # 4) Remove old revisions unless keep_old
        if not keep_old:
            # for pod in old: self._stop_and_remove_pod(pod)
            pass

        # 5) Build replica state list
        # states = self._build_states(manifest, revision)
        states: list[ReplicaState] = []

        return RuntimeResult(
            revision=revision,
            created=created,
            updated=updated,
            removed=removed,
            replica_states=states,
        )

    def read_logs(self, replica_id: str, *, follow: bool = False, tail: int | None = None, since: int | None = None):
        # CRI log path is in ContainerStatus or well-known pod log directory.
        # Read and yield lines; no remote streaming for now.
        return iter(())

    def remove_app(self, app_name: str) -> int:
        # Stop + remove all sandboxes for app label.
        return 0

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        return 0

    def list_containers_info(self) -> list[dict]:
        # Return one entry per replica (main container), with host_ports + port_map if mapped.
        return []

    def exec(self, replica_id: str, command: list[str], *, timeout: int | None = None) -> int:
        # Use ExecSyncRequest and return exit_code.
        return 1

    # Optional streaming exec; can be wired later through a CRI streaming endpoint.
    def exec_attach(self, replica_id: str, command: list[str], *, container: str | None = None, tty: bool = False):
        raise NotImplementedError

    def exec_resize(self, exec_id: str, *, height: int | None = None, width: int | None = None) -> None:
        return

    def exec_exit_code(self, exec_id: str) -> int:
        return 0

    # Storage lifecycle is handled elsewhere for CRI (see section 3)
    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:
        return

    def remove_storage_volumes(self, app_name: str, names: list[str]) -> int:
        return 0

    def list_storage_volumes(self, app_name: str | None = None) -> list[dict]:
        return []
```

### Wiring runtime selection

Update runtime selection in these places to accept `cri` (or `containerd`) and construct `CRIRuntime`:
- `src/ae/cli/__main__.py:runtime_factory`
- `src/ae/node/server.py:main` (runtime choices)
- `src/ae/apishim/adapter.py:_runtime_from_env`
- `src/ae/apishim/server.py:_runtime_from_env`

Suggested environment knobs:
- `AE_RUNTIME_BACKEND=cri` (or `containerd`)
- `AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock`
- `AE_CRI_SANDBOX_IMAGE=...` (pause image)
- `AE_NODE_ADVERTISE_IP` (already used) for host-port endpoint fallback

### Dependencies and layout

You will need CRI gRPC stubs. A minimal approach:
- Vendor `cri-api` proto and generate Python stubs into `src/ae/runtime/cri/api/runtime/v1/`.
- Add Python deps: `grpcio`, `protobuf`, `typing-extensions` (if needed).

Keep it internal to avoid pulling large Kubernetes deps.

---

## 2) AppManifest -> CRI mapping

This is a direct mapping of current `AppManifest` fields (see `src/ae/controller/spec.py`) to CRI PodSandbox/ContainerConfig fields.

### 2.1 PodSandboxConfig

Use one PodSandbox per replica (replica_id is unique).

- `metadata.name`: `replica_id` (ex: `app-rev3-0`)
- `metadata.namespace`: `manifest.metadata.namespace` (default `default`)
- `metadata.uid`: stable hash of `replica_id` (required for kube-compatible log paths)
- `metadata.attempt`: job attempt (from labels or reconciler if tracked)
- `labels`:
  - `runtime_labels_for_manifest(manifest)`
  - `ae.replica_id`, `ae.revision`
  - `ae.node` if `node_id` known
- `annotations`: optional; keep empty for now
- `log_directory`: `/var/log/pods/<ns>_<replica_id>_<uid>`
- `linux.security_context`: only if you need SELinux/AppArmor; for now, leave empty
- `port_mappings`: publish host ports only when replicas==1 and `service.port`/`service.ports` are defined

Port mappings:
- Use current `choose_host_port` logic (same as DockerRuntime) for stable host ports.
- For each `manifest.spec.ports` entry, map a host port if required:
  - Single service port: map `service.port -> targetPort` (same logic as DockerRuntime)
  - Multi-port service: map each service port to its targetPort
- `protocol`: `TCP`

### 2.2 ContainerConfig (main container)

- `metadata.name`: `"main"`
- `image.image`: `manifest.spec.image`
- `command` / `args`: align with Kubernetes semantics
  - If both are set: set `command` and `args` separately (do not concatenate)
  - If only args: set `args` only (entrypoint receives args)
- `envs`: list of key/value pairs from `manifest.spec.env`
- `working_dir`: `manifest.spec.working_dir`
- `labels`:
  - `runtime_labels_for_manifest(manifest)`
  - `ae.replica_id`, `ae.revision`, `ae.container=main`
  - `ae.job_attempt` for jobs
- `mounts`:
  - `manifest.spec.volumes` -> hostPath mounts (absolute host path)
  - `spec.storage` -> host path under `/var/lib/ae/volumes/<app>/<name>` (see section 3)
  - projection volume: `/var/run/ae/config/<app>` host path -> same mount path
- `resources.linux`: map requests/limits to CRI fields
  - `cpu_quota` / `cpu_period` or `cpu_shares`
  - `memory_limit_in_bytes`
- `security_context`:
  - `runAsUser`, `runAsGroup`
  - `readOnlyRootFilesystem`
  - `capabilities.drop` from `security.drop_caps`
  - seccomp/AppArmor:
    - RuntimeDefault -> CRI `seccomp_profile` runtime_default
    - Localhost -> path to profile
    - AppArmor profile name (strip `localhost/` if present)

### 2.3 Init containers

- One container per `manifest.spec.init_containers`.
- Run sequentially before main container.
- Use `CreateContainer + StartContainer`, then `WaitContainer` / `ContainerStatus` for exit code.
- Respect `timeoutSeconds` if provided.

### 2.4 Sidecars (spec.containers)

- One container per sidecar.
- Use same PodSandbox as main container.
- `metadata.name` = sidecar name.
- Mount projection mounts for sidecars.

### 2.5 ReplicaState + endpoint selection

CRI does not expose a health check by default; keep current behavior:
- `ready`: true if container is running (or exit_code == 0 for jobs)
- `status`: running / exited / unknown
- `exit_code`, `started_at`, `finished_at` from `ContainerStatus`

Endpoint selection should prefer pod IP + container port:
- `pod_ip` from PodSandboxStatus
- `preferred_port` from readiness probe (httpGet/tcpSocket)
- If host ports were published, keep host-port as fallback for local access

This is important because current health/ingress logic assumes host ports.

---

## 3) Storage + registry auth changes for containerd nodes

### 3.1 Storage (`spec.storage`)

Current Docker/Podman runtimes use named volumes. For CRI, containerd does not manage named volumes, so storage must be handled outside the runtime.

Suggested approach (MVP): hostPath-backed storage manager
- Define a per-node root: `/var/lib/ae/volumes/<app>/<volume>`
- `ensure_storage_volumes` creates directories with correct ownership and permissions
- `remove_storage_volumes` deletes directories when retention=Delete
- `list_storage_volumes` returns directory metadata + labels
- Keep current `ae.node` labeling to avoid cross-node confusion

Optional future:
- Provide a CSI-backed implementation for multi-node or cloud storage.
- Track PV/PVC metadata in the state DB if you need portable volumes.

### 3.2 Registry auth (PullImage)

For CRI, registry auth should be passed to `PullImageRequest` or configured in containerd hosts config.

MVP approach: AuthConfig on PullImage
- Reuse `RegistryAuthProvider` to resolve `{registry: {username, password}}`.
- Provide `_image_pull_auth(image)` that returns CRI `AuthConfig` if available.
- Call `PullImage(image, auth)` before container creation when image not present.

Optional: containerd `hosts.toml` support
- Translate `~/.config/ae/registries.yaml` into `/etc/containerd/certs.d/<registry>/hosts.toml`.
- Enable mirrors, TLS roots, and auth at node bootstrap.

### 3.3 Air-gapped / offline

- Support pre-pulled images (`ctr images import` or `nerdctl load`).
- `CRIRuntime` should detect existing images before pulling.

---

## Implementation checklist (summary)

Done:
- Add `CRIRuntime` adapter + gRPC stubs/codegen.
- Wire runtime selection for `AE_RUNTIME_BACKEND=cri`.
- Implement PodSandbox/ContainerConfig mapping from `AppManifest` (K8s command/args semantics).
- Use PodSandbox UID in log directory for kube-compatible log paths.
- Prefer pod IP + container port endpoints; hostPort only for replicas==1.
- Add hostPath storage manager for `spec.storage`.
- Add CRI PullImage auth path.
- Add minimal tests for mapping + runtime selection.
- Add CRI Service VIP provider (iptables NAT).
- Add exec/attach streaming via crictl.

Remaining:
- None (optional future: CRI streaming proxy to replace crictl).


---

## Backend compatibility matrix (current vs CRI)

This shows which subsystems are runtime‑agnostic today and which are Docker/Podman‑specific.

Legend:
- OK: works with current backend
- Needs work: requires CRI‑aware implementation or conditional disable

| Subsystem | Docker | Podman | CRI (containerd) | Notes |
| --- | --- | --- | --- | --- |
| RuntimeAdapter core (ensure_app, remove, list, exec sync) | OK | OK | OK | CRI runtime adapter implemented |
| Init containers | OK | OK | OK | implemented in CRI adapter |
| Sidecars | OK | OK | OK | implemented in CRI adapter |
| Logs (read_logs) | OK | OK | OK | CRI log path / ContainerStatus |
| Exec sync (probes/hooks) | OK | OK | OK | CRI ExecSync |
| Exec attach/streaming | OK | OK | OK (crictl) | requires crictl on node |
| Service VIP provider (HAProxy + docker network) | OK | OK | OK (iptables) | iptables NAT for CRI |
| Ingress reload inside container | OK | OK | OK (crictl fallback) | requires crictl + container name |
| Storage volumes (named volume API) | OK | OK | OK | hostPath manager for CRI |
| Registry auth | OK | OK | OK | CRI PullImage auth (hosts.toml optional) |
| Host‑port ingress/health endpoints | OK | OK | OK | prefer pod IP + container port |

Summary:
- Keeping Docker/Podman while adding CRI is straightforward at the adapter layer.
- The design impact is on **networking/ingress/storage/exec streaming**, which must become backend‑aware.
- These can be guarded by runtime type checks or by backend‑specific providers.


---

## Phased rollout plan (safe adoption)

### Phase 0 — Design + scaffolding (1–2 days)

- Add `CRIRuntime` skeleton with stubs for CRI runtime/image service.
- Wire `AE_RUNTIME_BACKEND=cri` in CLI/node/apishim (behind feature flag).
- Add config knobs: `AE_CRI_ENDPOINT`, `AE_CRI_SANDBOX_IMAGE`.
- Add docs for node prerequisites (containerd + CNI + runc, CRI enabled).

Exit criteria:
- `ae`/`ae-node` accept `AE_RUNTIME_BACKEND=cri` and start without crashing.
- Unit tests cover runtime selection path.

### Phase 1 — Core lifecycle (MVP, single‑node) (3–7 days)

- Implement PodSandbox + main container create/start/stop/remove.
- Implement image pull + basic auth in CRI adapter.
- Implement `list_containers_info` (one record per replica), `exec` via ExecSync.
- Implement `ReplicaState` population (status, exit_code, started/finished times).

Exit criteria:
- Single‑node reconcile works for 1‑replica app using containerd.
- `ae logs`, readiness/liveness probes, and rollout hooks work (exec + HTTP/TCP).

### Phase 2 — Networking + endpoints (5–10 days)

- Done: prefer pod IP + container port for endpoints; host ports only when explicitly mapped.
- Done: implement pod port mappings (CRI `PortMapping`) for `service.port` / `service.ports`.
- Done: update health/ingress selection to tolerate pod IP endpoints.
- Done: add a CRI‑compatible Service provider (iptables NAT).

Exit criteria:
- Ingress can route to pod IP endpoints (or documented limitations).
- Port‑forward works for CRI nodes using pod IP.

### Phase 3 — Storage + sidecars + init (5–10 days)

- Done: add hostPath‑based storage manager for `spec.storage`.
- Done: implement init containers sequential execution.
- Done: implement sidecar container creation within the same PodSandbox.
- Done: add cleanup logic (old revisions, job backoff).

Exit criteria:
- Storage volumes behave similarly to Docker/Podman runtimes.
- Init containers and sidecars operate correctly under CRI.

### Phase 4 — Streaming + multi-node polish (optional) (7–14 days)

- Done: exec/attach streaming via crictl (node dependency).
- Optional: proxy CRI streaming server directly (avoid crictl dependency).
- Add node‑level smoke checks (crictl info, runtime endpoints).
- Add integration tests for CRI path (optionally via kind/containerd in CI).

Exit criteria:
- `kubectl exec`/attach works for CRI nodes.
- Port-forward works for CRI nodes (direct pod IP or CRI proxy).
- Multi‑node scheduling with CRI nodes is stable.

---

## Rollout strategy (mixed backends)

- Keep Docker/Podman as default until CRI CI tests are complete.
- Allow `AE_RUNTIME_BACKEND=cri` only on nodes explicitly labeled (e.g., `runtime=cri`).
- Gate Service VIP provider usage on backend type to avoid docker‑only assumptions.
- Gradually move dev/test nodes to CRI, then production nodes.


---

## Concrete task breakdown

### Phase 0 — Design + scaffolding
- Add module: `src/ae/runtime/cri_runtime.py` (skeleton class + TODOs)
  - Align naming with Kubernetes/CRI: PodSandbox == Pod, ContainerConfig == Container, RuntimeService == `runtime.v1.RuntimeService`.
  - Preserve existing label keys (`ae.*`) but ensure `app.kubernetes.io/*` labels still flow from `runtime_labels_for_manifest`.
  - Use PodSandbox `metadata.name` as the replica id and Container `metadata.name` as the container name (`main`/sidecar).
- Add CRI deps in `requirements.in` (grpcio, protobuf)
  - Target CRI API v1 (Kubernetes 1.26+); avoid `v1alpha2` to keep names aligned with current kubelet/CRIs.
  - Ensure generated stubs expose `RuntimeService` and `ImageService` under `runtime.v1` (nomenclature must match CRI).
- Vendor CRI proto into `src/ae/runtime/cri/api/runtime/v1/` (or scripts to generate stubs)
  - Use upstream `k8s.io/cri-api/pkg/apis/runtime/v1/api.proto` without renaming package or services.
  - Keep package name `runtime.v1` and service names `RuntimeService`/`ImageService` to preserve CRI method names.
- Wire backend selection in:
  - `src/ae/cli/__main__.py` (runtime_factory)
  - `src/ae/node/server.py` (arg choices + constructor)
  - `src/ae/apishim/adapter.py` and `src/ae/apishim/server.py`
  - Use backend values aligned with Kubernetes terminology: `cri` or `containerd`, not `kubelet`.
  - Persist node backend as `cri` to reflect the CRI runtime (Kubernetes uses CRI as the contract).
- Doc: `docs/wip/cri-runtime.md` (this file) + `docs/ops/runbook.md` snippet for node preflight
  - Use Kubernetes terms: CRI endpoint, sandbox image, CNI bin/conf dirs, and cgroup driver alignment.
  - Match kubelet endpoint format: `unix:///run/containerd/containerd.sock` (note triple slash).

### Phase 1 — Core lifecycle
- Implement CRI channel creation (unix socket support)
  - Support kubelet-style endpoints: `unix://` or `unix:///` (containerd uses the triple-slash format).
  - Create separate clients for `RuntimeService` and `ImageService` (Kubernetes treats them as distinct gRPC services).
- Implement:
  - `PullImage`, `ImageStatus` helper
    - Use `ImageSpec.image` as the canonical image reference (same as Kubernetes PodSpec image).
    - Use `AuthConfig` fields (`username`, `password`, `auth`, `server_address`, `identity_token`, `registry_token`) to match kubelet behavior.
    - Pass `sandbox_config` to `PullImageRequest` when available to align with kubelet's auth scoping.
  - `RunPodSandbox` + `CreateContainer` + `StartContainer`
    - Populate `PodSandboxConfig.metadata` with `name`, `namespace`, `uid`, `attempt` (Kubernetes uses these fields to correlate Pods).
    - `PodSandboxConfig.labels/annotations` map to Pod labels/annotations (do not mix Container labels here).
    - `ContainerConfig.metadata.name` must match Kubernetes container name (main/sidecar/init) exactly.
    - `ContainerConfig.labels` should mirror Pod labels plus `ae.container` for disambiguation.
    - `ContainerConfig.command/args` semantics must mirror K8s (command overrides entrypoint; args passed to entrypoint).
    - `LinuxContainerResources` maps CPU/mem to `cpu_shares`, `cpu_quota`, `cpu_period`, `memory_limit_in_bytes` per CRI spec.
    - `SecurityContext` maps runAsUser/group, readOnlyRootFilesystem, capabilities drop, seccomp/AppArmor via `SecurityProfile`.
  - `StopPodSandbox` + `RemovePodSandbox`
    - Call `StopContainer` with `timeout` derived from `terminationGracePeriodSeconds` (Kubernetes uses this for graceful termination).
    - Stop all containers before `StopPodSandbox` to align with kubelet stop order.
- Implement container lookup by labels (list sandboxes + containers)
  - Use `ListPodSandboxRequest.filter.label_selector` to select Pods by label (PodSandbox labels).
  - Use `ListContainersRequest.filter.label_selector` and `pod_sandbox_id` for container scoping.
  - Use `ae.container=main` to identify the primary container, consistent with K8s "containers[0]".
- Implement `RuntimeResult` + `ReplicaState` from CRI `ContainerStatus` + `PodSandboxStatus`
  - Map CRI state enum to strings (`CONTAINER_RUNNING` -> `running`, `CONTAINER_EXITED` -> `exited`).
  - Use `started_at`, `finished_at`, `exit_code` fields from CRI to match Kubernetes container status fields.
  - Use `PodSandboxStatus.network.ip` as `pod_ip` (Kubernetes `podIP` semantics).
- Implement `exec` via `ExecSync`
  - `ExecSync` is the CRI equivalent of Kubernetes exec probes (non-interactive, exit code based).
  - Pass container name (`main` or sidecar) explicitly to align with Kubernetes "container" target.
- Add unit tests for mapping + lifecycle with stubbed gRPC
  - Assert PodSandbox metadata fields (`name`, `namespace`, `uid`, `attempt`) match Kubernetes expectations.
  - Assert container metadata names match Kubernetes container names (main/init/sidecar).

### Phase 2 — Networking + endpoints
- Done: implement port mapping logic in CRI PodSandboxConfig
  - Use CRI `PortMapping` (`container_port`, `host_port`, `protocol`, `host_ip`) to mirror Kubernetes `hostPort`.
  - Only set mappings when `service.port` / `service.ports` requires a host port (avoid implicit hostPort).
- Done: update endpoint selection to prefer pod IP + container port
  - Align with Kubernetes: Service/Ingress routes to `podIP:containerPort`, not host ports.
  - Use readiness probe port (`httpGet.port` or `tcpSocket.port`) as the preferred container port.
  - Only fall back to hostPort for local/dev or when podIP is unavailable.
- Done: update `list_containers_info` to return:
  - `host_ports`, `port_map`, `pod_ip`, `host_ip`
  - one entry per replica (main container only)
  - Map `uid` to PodSandbox ID (closest equivalent to Kubernetes Pod UID).
  - Preserve labels on the PodSandbox as the authoritative label set (Pod-level metadata).
- Done: add CRI Service VIP provider (iptables NAT).
  - Aligns with Kubernetes `ClusterIP` semantics on single-node setups.
- Done: add tests for endpoint selection, pod IP mapping, port mapping
  - Validate `pod_ip` is used for readiness/ingress when present.
  - Validate hostPort mappings match requested Service ports (K8s hostPort semantics).

### Phase 3 — Storage + sidecars + init
- Done: implement storage manager (hostPath) for CRI:
  - `ensure_storage_volumes`, `remove_storage_volumes`, `list_storage_volumes`
  - Treat `spec.storage` as Kubernetes `hostPath` (node-local, not portable like PVC/PV).
  - Map storage volumes into `ContainerConfig.mounts` (`host_path`, `container_path`, `readonly`).
- Done: implement init containers (sequential run, exit code, timeout)
  - Align with Kubernetes initContainers: run sequentially, block main until success.
  - Use container names from manifest; set `ContainerConfig.metadata.name` accordingly.
  - Retry on failure similar to K8s pod restart policy (until backoff/limit).
- Done: implement sidecars in same PodSandbox
  - Map to additional containers in same PodSandbox (Kubernetes Pod containers).
  - Use container name as `metadata.name`; set `ae.container=<name>` label.
  - Do not create separate sandboxes for sidecars.
- Done: add tests for init/sidecar mapping and storage lifecycle (unit coverage).
  - Ensure init containers run in order and stop rollout on failure (unless policy says otherwise).
  - Ensure sidecars share pod IP and volumes.

### Phase 4 — Streaming + multi-node polish
- Done: implement exec/attach streaming via `crictl exec` (node dependency).
  - A CRI-native streaming proxy can still replace this later.
- Done: implement log follow via CRI `log_path` (basic; kubelet-style rotation not covered).
- Done: validate CI integration tests: containerd node smoke + CRI adapter in CI
  - CI workflow added (`.github/workflows/cri-ci.yml`) using `scripts/cri_ci_setup.sh`.
  - CI setup now runs under sudo and waits for `RuntimeReady` + `NetworkReady`.
  - Gated integration tests exist locally (AE_CRI_SMOKE_PULL, AE_CRI_IT).
  - `cri-ci` validated on runner.
- Done: update docs: `docs/ops/runbook.md` with CRI debug and `crictl` usage.

---

## Kubernetes/CRI terminology glossary

Use this as the source of truth for naming and alignment in code and docs.

- **Pod** (Kubernetes) == **PodSandbox** (CRI)
  - Kubernetes `Pod` metadata -> CRI `PodSandboxConfig.metadata`
  - Pod labels/annotations -> CRI `PodSandboxConfig.labels` / `annotations`
  - Pod IP -> CRI `PodSandboxStatus.network.ip`
- **Container** (Kubernetes) == **ContainerConfig** (CRI)
  - Kubernetes `container.name` -> CRI `ContainerConfig.metadata.name`
  - Kubernetes `container.image` -> CRI `ImageSpec.image`
  - Kubernetes `command`/`args` -> CRI `ContainerConfig.command` / `args`
- **Init container** (Kubernetes) == CRI container in same PodSandbox, run sequentially
  - Must finish successfully before main container starts
- **Sidecar container** (Kubernetes) == CRI container in same PodSandbox
  - Shares Pod network + volumes
- **Pod UID** (Kubernetes) -> use PodSandbox ID or stable hash for `metadata.uid`
- **HostPort** (Kubernetes) -> CRI `PortMapping.host_port`
- **ContainerPort** (Kubernetes) -> CRI `PortMapping.container_port`
- **Restart policy** (Kubernetes) -> emulate in controller/runtime logic (CRI does not enforce)
- **Exec probe** (Kubernetes) -> CRI `ExecSync`
- **Exec/Attach streaming** (Kubernetes) -> CRI `Exec`/`Attach` streaming endpoints + SPDY proxy
- **Logs** (Kubernetes) -> CRI log format from `ContainerStatus.log_path`

Alignment checklist:
- Pod labels belong on PodSandbox, not per-container (unless you intentionally duplicate).
- Container labels are allowed but should be minimal; prefer Pod labels for selection.
- PodSandbox name should match replica_id; container name should match manifest container name.
- Readiness/liveness should target `podIP:containerPort` by default.
- HostPort should be opt-in only (e.g., when Service requires it).


---

## CRI utilities and dependencies (project + node)

### Project Python dependencies

Add to `requirements.in` (or a CRI extras group):
- `grpcio`
- `protobuf`
- `grpcio-tools` (only if you generate stubs in‑repo)
- `types-protobuf` (optional, mypy type hints)

### Repo scripts (recommended)

- `scripts/cri_codegen.sh`
  - Fetch `cri-api` proto and generate `runtime.v1` Python stubs.
  - Keep package/service names unchanged to preserve CRI method naming.
- `scripts/cri_preflight.sh`
  - Verify containerd socket, CRI plugin enabled, cgroup driver, CNI dirs.
- `scripts/cri_smoke.sh`
  - Run `crictl info`, pull sandbox image, and run a minimal PodSandbox.

### Node/system utilities (document in runbook)

Required:
- `containerd` (with CRI plugin enabled)
- `runc` (or `crun` if you support it)
- CNI plugins in `/opt/cni/bin` and config in `/etc/cni/net.d`

Recommended:
- `crictl` (CRI debug CLI)
- `nerdctl` (operator-friendly CLI for containerd)
- `skopeo` (optional, registry/air‑gap validation)


---

## Future CRI features to consider (post‑MVP)

### Runtime introspection & config
- CRI `Version`, `Status`, `RuntimeConfig`, `UpdateRuntimeConfig`
- Validate CRI plugin enabled and cgroup driver alignment (systemd vs cgroupfs).

### Stats & metrics (HPA/observability)
- `ContainerStats`, `ListContainerStats`, `PodSandboxStats`, `ListPodSandboxStats`, `ImageFsInfo`
- Align with Kubernetes metrics semantics (bytes, cpu usage, timestamps).

### Resource updates
- `UpdateContainerResources` for live CPU/memory updates
- Match Kubernetes semantics for mutable resources (best‑effort if unsupported).

### Image management & policy
- `ListImages`, `RemoveImage`, image garbage collection
- Honor `imagePullPolicy` (Always/IfNotPresent/Never)
- Support Kubernetes credential providers and imagePullSecrets mapping

### Security + isolation
- PodSecurityContext: `fsGroup`, supplemental groups, pod‑level seccomp
- Container security: `no_new_privileges`, `privileged`, `readonly_paths`, `masked_paths`
- SELinux/AppArmor/Seccomp via CRI `SecurityProfile` (RuntimeDefault/Unconfined/Localhost)

### Networking & DNS
- Pod DNS config: `dns_config`, `hostname`, `hostAliases`
- Namespace options: `hostNetwork`, `hostPID`, `hostIPC`
- Pod‑level sysctls mapping

### Advanced CNI / multi‑network
- Multus/secondary networks via pod annotations
- Bandwidth/MTU constraints when CNI supports them

### Logs & rotation
- `ReopenContainerLog` to align with kubelet log rotation

### Streaming & port‑forward
- CRI `Exec`, `Attach`, `PortForward` streaming endpoints
- SPDY channel semantics for stdin/stdout/stderr/error/resize

### Ephemeral containers
- kubectl debug / ephemeral containers within same PodSandbox

### RuntimeClass / runtime handler
- Map `runtimeClassName` -> CRI `runtime_handler`
- Support gVisor/Kata via runtime handlers

### Devices & hugepages
- CDI device injection (GPU, RDMA) when runtime supports it
- Hugepage limits and device allocations in `LinuxContainerResources`
