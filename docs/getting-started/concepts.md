# k1s Concepts

This guide explains core concepts used throughout k1s.

## Terminology map (k1s -> Kubernetes)

| k1s term | Kubernetes term | Notes |
| --- | --- | --- |
| Deployment | Deployment (workload) | k1s native workload manifest (`kind: Deployment`). |
| Pod | Pod | One container instance for a Deployment revision. |
| Revision | ReplicaSet / Deployment revision | Immutable snapshot of desired state. |
| Service VIP | Service / ClusterIP | Stable virtual IP for service routing. |
| Spec / manifest | Manifest | YAML resource definition. |

## Deployment (k1s workload)

The primary unit of deployment. Defined by a `Deployment` manifest with a desired image, replicas, ports, health checks, and optional ingress/secrets/resources.

## Pod

An individual container instance belonging to an app revision. Pod names follow the pattern `<app>-rev<revision>-<index>` (e.g., `echo-rev3-0`).

## Node

A registered worker (or the controller host) that can run pods. Nodes send heartbeats, advertise labels/taints, and expose a runtime endpoint that the controller uses for ensure/logs/exec/probes. Nodes can be cordoned or drained via `ae nodes`.

## Service VIP (Service/ClusterIP)

A stable virtual IP allocated from the Service CIDR and fronted by the overlay provider. Ingress prefers VIPs when ready endpoints exist; hostPorts remain for single-node edge cases.

## Revision (Deployment revision)

An immutable snapshot of desired state computed from the manifest spec hash. A new revision is created when the manifest content changes.

- Stored in the controller state backend (SQLite by default; Postgres/etcd durable lanes supported) with normalized JSON and status.
- Used for rollbacks: `ae rollback <app> [--to <rev>]`.

## Reconcile

The controller compares the desired state from specs to the observed state (state backend + node inventory + runtimes) and performs the operations needed to converge.

- Schedules replicas onto Ready nodes that match `nodeSelector`/tolerations/topology spread. Storage pinning keeps retained volumes on a single node.
- Creates or updates containers via the node agent runtime (pulling images as needed); falls back to the local runtime when no nodes are eligible.
- Removes containers from older revisions after the new revision is live.
- Allocates Service VIPs and updates ingress to healthy upstreams once readiness passes.

## Health

- Readiness: Determines whether a pod should receive traffic (2xx HTTP implies success).
- Liveness: Indicates the pod is still alive (defaults to true if not specified).
- Initial delay: Allows the app to boot before probes start.
- Startup probes gate readiness/liveness until they succeed; lifecycle hooks (postStart/preStop) run around pod start/stop.

## Status

Per‑app aggregate status is one of:

- ready: all desired replicas are ready
- progressing: desired count is live but not yet all ready
- degraded: otherwise (e.g., live replicas below desired or readiness fails)

CLI examples:

```
ae status myapp --wide --events
```

## Events

Lightweight audit trail for changes and noteworthy events:

- ApplyStarted, ApplyCompleted
- IngressConfigured, IngressRemoved
- NodeRegistered, NodeCordon, NodeUncordon
- ServiceVIPAllocated, ServiceVIPRemoved
- Readiness/rollout failures when health gates trip

Inspect with:

```
ae events myapp --limit 20
```

## Rollouts

Rolling replace with `maxUnavailable=0, maxSurge=1` semantics across one or more nodes:

1. Start a new pod for the next revision.
2. Wait for readiness.
3. Switch ingress to the new Service VIP endpoints (or hostPort when VIP disabled).
4. Stop and remove old revision containers.

Pause/resume and canary:
- Pause/resume: `ae rollout pause|resume <app>` halts or resumes rollout progression.
- Canary: set `spec.rollout.strategy: canary` and `spec.rollout.weight` (optional `auto` ramp) to bias routing.

Rollback uses the recorded manifest for the target revision:

```
ae rollback myapp --to 3
```

## Resources & Volumes

- CPU/memory limits map to engine flags (Docker nano_cpus; Podman/Docker `--cpus`, memory quantities → bytes).
- Volumes map to hostPath → bind mount (ro/rw). `spec.storage` creates named engine volumes per app.
- Storage volumes are bound to the node that first creates them; scheduler pins retained volumes to that node.

## Ingress

For apps with `spec.ingress`, the controller writes a Caddy site snippet and reloads the proxy. When Caddy runs in a container, loopback upstreams are rewritten to the host alias: `host.docker.internal` (Docker) or `host.containers.internal` (Podman).

In multi-node runs, ingress prefers Service VIPs supplied by the overlay provider so upstream selection is node-agnostic.

## HTTP API

Read‑only status/metrics/events published at:

- `/metrics`, `/health`, `/status`, `/status/<app>`, `/events/<app>`
- `/history/<app>` for probe history, `/manifest/<app>` for latest stored manifest
- `/nodes` for node inventory + heartbeat staleness
- `/system` + `/dashboard` for a quick UI snapshot
- `/openapi.json` and a tiny docs page at `/docs`

Note: when any token is configured, reads require the READ token.

Kubernetes API shim (optional):

- `AE_APISHIM_ENABLE=1 python -m ae.apishim serve --token <bearer>` exposes `/api`, `/apis`, `/openapi/v2|v3` with SSA/patch and port-forward (HTTP by default; add `--tls` for HTTPS).
- Works with kubectl/helm for Deployments/Services/Ingress/HPA/RBAC; StatefulSet/DaemonSet/Job/CronJob are accepted but emulated (see `docs/reference/apishim-compatibility-matrix.md`).
