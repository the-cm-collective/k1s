# k1s Concepts

This guide explains core concepts used throughout k1s.

## App

The primary unit of deployment. Defined by an `App` manifest with a desired image, replicas, ports, health checks, and optional ingress/secrets/resources.

## Replica

An individual container instance belonging to an app revision. Replica IDs follow the pattern `<app>-rev<revision>-<index>` (e.g., `echo-rev3-0`).

## Revision

An immutable snapshot of desired state computed from the manifest spec hash. A new revision is created when the manifest content changes.

- Stored in SQLite with its normalized JSON and status.
- Used for rollbacks: `ae rollback <app> [--to <rev>]`.

## Reconcile

The controller compares the desired state from specs to the observed state (Docker + SQLite) and performs the operations needed to converge.

- Creates missing replicas (pulling images as needed).
- Removes containers from older revisions after the new revision is live.
- Updates ingress to a healthy upstream once readiness passes.

## Health

- Readiness: Determines whether a replica should receive traffic (2xx HTTP implies success).
- Liveness: Indicates the replica is still alive (defaults to true if not specified).
- Initial delay: Allows the app to boot before probes start.

## Status

Per‑app aggregate status is one of:

- ready: all desired replicas are ready
- progressing: desired count is live but not yet all ready
- degraded: fewer than desired live replicas

CLI examples:

```
ae status myapp --wide --events
```

## Events

Lightweight audit trail for changes and noteworthy events:

- ApplyStarted, ApplyCompleted
- IngressConfigured, IngressRemoved
- (future) ReadinessFailed, RolloutFailed

Inspect with:

```
ae events myapp --limit 20
```

## Rollouts

Single‑node rolling replace with `maxUnavailable=0, maxSurge=1` semantics:

1. Start a new replica for the next revision.
2. Wait for readiness.
3. Switch ingress to the new endpoint.
4. Stop and remove old revision containers.

Rollback uses the recorded manifest for the target revision:

```
ae rollback myapp --to 3
```

## Resources & Volumes

- CPU/memory limits map to Docker `nano_cpus` and `mem_limit`.
- Volumes map to hostPath → bind mount (ro/rw).

## Ingress

For apps with `spec.ingress`, the controller writes a Caddy site snippet and reloads the proxy. When Caddy runs in a container, upstreams pointing to 127.0.0.1 are rewritten to `host.docker.internal`.

## HTTP API

Read‑only status/metrics/events published at:

- `/metrics`, `/status`, `/status/<app>`, `/events/<app>`
- `/openapi.json` and a tiny docs page at `/docs`

