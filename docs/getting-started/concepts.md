# k1s Concepts

This page is the glossary and mental-model guide for the core nouns and control loops used throughout k1s. Use [Overview](overview.html) for the current architecture snapshot and [Concepts in Practice](concepts-in-practice.html) for chapterized walkthroughs.

## Terminology Map (k1s -> Kubernetes)

| k1s term | Kubernetes term | Notes |
| --- | --- | --- |
| Deployment | Deployment (workload) | k1s native workload manifest (`kind: Deployment`). |
| Pod | Pod | One running container instance for a revision. |
| Revision | Deployment revision / ReplicaSet snapshot | Immutable desired-state snapshot. |
| Service VIP | Service / ClusterIP | Stable service address used for L4 routing. |
| Spec / manifest | Manifest | YAML resource definition for desired state. |
| HTTP API | Controller-native API | Operational status, metrics, planner, and mutation surface. |
| API shim | Kubernetes-compatible API | Discovery and object surface for `kubectl`, Helm, and compatibility clients. |

## Deployment

The primary workload unit in k1s is a `Deployment` manifest. It captures the desired image, replica count, ports, health checks, config and secret projection, rollout preferences, scheduling hints, and optional service or ingress exposure.

## Pod

A pod is one running container instance for a deployment revision. Pod names follow the `<app>-rev<revision>-<index>` pattern and give the controller a stable way to track readiness, liveness, logs, and rollout progress.

## Node

A node is a worker that can host pods. Nodes advertise labels, taints, runtime capability, and heartbeat freshness. The scheduler uses that inventory to decide placement, and operators manage node availability with `ae nodes`.

## Service VIP

A Service VIP is the stable service address allocated from the service CIDR and backed by the current overlay or provider path. It gives the system a node-agnostic L4 target for service routing, health-based upstream switching, and multi-node ingress.

## Revision

A revision is an immutable snapshot of desired workload state derived from the manifest spec hash. Revision metadata lives in controller state: SQLite is the local default, while shared-authority HA and strict-CRI lanes use the durable control-plane state path. Rollbacks target a stored revision rather than reconstructing state from live containers.

## Reconcile

Reconcile is the control loop that compares desired state with observed state and performs the operations needed to converge:

- schedule replicas onto eligible nodes
- create or update runtime containers
- pin retained storage to the correct node
- allocate Service VIPs
- update exposure paths once readiness gates pass

This is the core controller pattern that maps most directly to Kubernetes controller behavior.

## Health and Status

- Readiness decides whether a pod should receive traffic.
- Liveness decides whether the runtime should treat the pod as still alive.
- Startup probes delay readiness and liveness evaluation until boot is complete.
- Aggregate app status rolls up these signals into states such as `ready`, `progressing`, or `degraded`.

Health is pod-level; status is the controller’s higher-level summary of the app as a whole.

## Events

Events are the lightweight audit trail for desired-state changes and notable runtime transitions. They record things like apply completion, ingress changes, node cordon actions, Service VIP allocation, and rollout or probe failures.

## Rollouts

A rollout is the controlled transition from one revision to the next. k1s starts new pods, waits for readiness, switches service and ingress traffic, and only then removes the older revision. Rollbacks reuse a previously stored revision, so rollout history is part of the control-plane model rather than a best-effort log.

## Resources, Volumes, and Placement

CPU and memory requests or limits describe runtime expectations. Volumes describe host-path or retained named storage. Placement hints such as selectors, tolerations, and topology spread tell the scheduler where replicas are allowed to run. Retained storage is single-node by default, so storage locality is part of placement, not just runtime mounting.

## Ingress and Exposure

Service exposure in k1s has two layers:

- Service VIPs provide stable L4 routing for app traffic.
- Ingress provides the L7 entry path on top of those services.

Current ingress modes split by topology:

- core ingress modes use Envoy as the primary ingress surface on the core side
- edge-local renders gateway-local Caddy from route bundles on the edge side
- docs, API, and dashboard TLS hostnames are adjacent control-plane surfaces, not the same thing as app ingress modes

Use [Ingress](ingress.html) for the current architecture and [Ingress Validation](ingress-capability-test-sequence.html) for the command-heavy validation sequence.

## API Surfaces

k1s exposes two distinct API layers:

- [HTTP API](http-api.html): the controller-native operational surface for status, metrics, planner calls, dashboard data, and opt-in mutations
- [API Shim](api-shim.html): the Kubernetes-compatible discovery and object surface for `kubectl`, Helm, and compatibility-oriented tooling

Auth and mutation gating are separate concerns from the route catalog. Use [API Auth](api-auth.html) for token roles, scopes, expiry, controller mutation gating, and shim auth modes.

## Further Reading

- [Overview](overview.html)
- [Start Here](start-here.html)
- [Concepts in Practice](concepts-in-practice.html)
- [Architecture](architecture.html)
