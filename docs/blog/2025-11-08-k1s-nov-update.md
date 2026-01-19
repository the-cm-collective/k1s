---
title: "k1s November Update: Dashboard, Playground, Storage, and K8s Parity"
date: 2025-11-08
authors: [k1s Team]
tags: [k1s, updates, dashboard, playground, storage, tls, rbac, haproxy, kubernetes]
summary: "What’s new since the Oct 29 deep dive: a live Dashboard and Labs Playground, PV‑lite storage with a volumes CLI, RBAC + dev mutations on the HTTP API, L4 services guidance with an HAProxy watcher, rollout and canary polish, and tighter Kubernetes export/compliance tooling."
cover_image: "../docs.home.arpa_8443_playground.html.png"
---

# k1s November Update: Dashboard, Playground, Storage, and K8s Parity

Well, hello again! This is a short technical tour of what changed since the Oct 29 deep dive, with commands you can run locally. It focuses on the dashboard/playground, PV‑lite storage, HTTP API auth, L4 service patterns, rollouts, and portability tooling.

## TL;DR Highlights

- Dashboard (/dashboard): live UI for status, events, and logs, plus a System snapshot backed by `/system` (ingress/sites, services, volumes, RBAC).
- Labs Playground: interactive page that runs read‑only by default; controlled actions require `AE_LABS=1` and `AE_LABS_TOKEN`.
- PV‑lite storage: `spec.storage` → engine‑named volumes, `ae volumes list` for inspection, retention honored by `ae delete --purge`.
- HTTP API auth + dev mutations: `AE_API_READ_TOKEN`, `AE_API_SCALER_TOKEN`, `AE_API_ADMIN_TOKEN`; `AE_API_MUTATIONS=1` enables scale/delete.
- L4 services guide: stable host port for single‑replica apps; HAProxy watcher tracks replica ports via `/system`.
- Rollout polish: canary weight + controller‑tracked auto ramps persisted in SQLite.
- Kubernetes parity: `ae export-k8s` presets, `k8s-check`, and a docs‑embedded compliance report (`k8s-report`).
- Registry + supply chain: helpers for GHCR/GCR/ECR and `verify-image` via cosign.

## 1) Live Dashboard and System Snapshot

The controller’s HTTP server serves a simple dashboard at `/dashboard` with:

- Per‑app status cards, event stream, and log tails (poll/SSE when available)
- A System panel sourced from `GET /system`: last reconcile timings, ingress site health, declared services, discovered volumes, and RBAC state

The `/system` response is a JSON snapshot you can use in scripts or dashboards without scraping the UI.

Quick start (local):

```bash
python -m ae.controller --loop --watch --metrics-port 9108 &
python -m ae.cli apply -f specs/examples/echo.yaml
open http://127.0.0.1:9108/dashboard
```

Docs: see `docs/reference/observability.md` and `docs/reference/http-api.md`.

## 2) Labs Playground (Interactive)

The Playground is an interactive page for fast hands‑on exploration. It prefers HTMX + SSE for live updates and can run in two modes:

- Read‑only: verifiers and copyable CLI, no server‑side actions
- Controlled actions: with `AE_LABS=1` and `AE_LABS_TOKEN`, you can apply an example, scale replicas, and adjust canary weight

Try it in the dev stack:

```bash
# Start the docs/API/dev stack with Labs enabled (token optional)
make demo ARGS="--docs-only -y -d"  # or use ./scripts/init_demo.sh --docs-only -y -d
# Open the playground
open https://docs.home.arpa:8443/playground
```

Docs: `docs/guides/playground.md`. Demo flags: `scripts/init_demo.sh --labs[ --labs-token <T>]`.

## 3) PV‑Lite Storage + Volumes CLI

Declare persistent volumes with a tiny spec and inspect them via CLI.

Spec excerpt:

```yaml
spec:
  storage:
    - name: data
      mountPath: /var/lib/echo
      retention: Retain   # or Delete
```

CLI:

```bash
python -m ae.cli volumes list --app echo  # or omit --app for all
python -m ae.cli delete echo --purge      # removes volumes with retention: Delete
```

Docs: `docs/reference/storage.md`.

PV‑lite uses engine‑named volumes and retention flags; it is not full PVC/PV semantics.

Spec → runtime: `spec.storage` creates engine‑named volumes (`ae-<app>-<name>`) and mounts them at `mountPath`. Retention controls whether `ae delete --purge` removes the volume.

## 4) HTTP API: RBAC and Dev Mutations

The HTTP surface stays read‑only by default, but you can optionally enable mutations for demos/tests and gate them with tokens. When any token is configured, the API expects a bearer token.

- Tokens (optional): `AE_API_READ_TOKEN`, `AE_API_SCALER_TOKEN`, `AE_API_ADMIN_TOKEN`
- Mutations (opt‑in): set `AE_API_MUTATIONS=1` on the controller
- Endpoints: `/status`, `/events`, `/logs`, `/metrics`, `/health`, `/openapi.json`, `/docs`, `/dashboard`, plus `/scale/<app>` and `/delete/<app>` when enabled

Examples:

```bash
export AE_API_MUTATIONS=1 AE_API_SCALER_TOKEN=scaletok AE_API_ADMIN_TOKEN=admintok
python -m ae.controller --loop --metrics-port 9108 &

# Scale over HTTP via the CLI
python -m ae.cli --server http://127.0.0.1:9108 --token scaletok scale echo --replicas 2

# Stream logs remotely (READ token if any token is set)
python -m ae.cli --server http://127.0.0.1:9108 --token scaletok logs echo --tail 100
```

Docs: `docs/reference/http-api.md`, `docs/reference/api-auth.md`.

## 5) L4 Services: TCP Patterns + HAProxy Watcher

When you need non‑HTTP (TCP/UDP) on a single host, the guide shows pragmatic options:

- Single‑replica with a stable host port via `spec.service.port` (multi‑replica needs an external proxy)
- External L4 proxy for multi‑replica (recommended). We provide a dev HAProxy service and two helpers:
  - One‑shot config render from `/system`
  - A watcher that rewrites the backend list as replicas/ports change and validates the config inside the container before restart

Makefile helpers:

```bash
docker compose -f ops/dev/docker-compose.yaml up -d haproxy
make haproxy-update APP=tcp-echo
make haproxy-watch APP=tcp-echo   # continuous
```

Docs and scripts: `docs/guides/l4-services.md`, `scripts/dev/update_haproxy_from_api.py`, `scripts/dev/watch_haproxy.py`.

## 6) Rollouts: Ordered, Parallel, Canary (with Auto Ramps)

We clarified semantics, polished events, and wired a controller‑tracked canary ramp that persists in SQLite.

Spec → runtime: `rollout.strategy=canary` biases ingress toward the first upstream, and `rollout.auto` stores the ramp schedule in SQLite so it survives restarts.

Spec excerpt:

```yaml
spec:
  rollout:
    strategy: canary
    weight: 3
    auto: { start: 3, step: 2, intervalSeconds: 60, max: 9 }
```

Pause/resume without changing runtime state:

```bash
python -m ae.cli rollout pause echo && python -m ae.cli rollout resume echo
```

Docs: `docs/reference/rollouts.md`.

## 7) Kubernetes Parity: Export, Check, Report

We tightened validations and made it easier to see how close your spec is to “portable K8s”.

- Exporter: `ae export-k8s` with presets, strict “require requests”, HPA/PDB options, PVC emission from `spec.storage`
- Checklist: `ae k8s-check --policy strict -f <file>`
- Compliance report (embeds on the docs site):

```bash
python -m ae.cli k8s-report --run-dry-run -o docs/site/k8s_status.json
python docs/build_docs.py
```

Docs: `docs/reference/k8s-compliance.md` (coverage summary and compliance status).

## 8) Registry Auth + Image Verification

Helpers for common registries, plus a `verify-image` wrapper around cosign.

```bash
# GHCR
python -m ae.cli registry login ghcr --username $USER --token $GHCR_TOKEN
python -m ae.cli registry list

# Verify signatures (cosign must be installed)
python -m ae.cli verify-image ghcr.io/org/app:1.2.3 --certificate-identity your@id --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Docs: `docs/reference/api-auth.md` (registry section).

## 9) Demos and Docs Refresh

The demo script gained clearer flags and prints direct links to Swagger, ReDoc, and the Dashboard.

```bash
./scripts/init_demo.sh --demo-standard -y -d            # TLS blue/green
./scripts/init_demo.sh --demo-echo-mr -y -d             # multi‑replica echo
./scripts/init_demo.sh --demo-rollout -y -d             # ordered rollout + canary
./scripts/init_demo.sh --demo-storage -y -d             # PV‑lite volumes
./scripts/init_demo.sh --docs-only -y -d                # docs + API + dashboard
```

Docs pages touched recently:

- `docs/reference/observability.md`, `docs/reference/http-api.md`, `docs/guides/playground.md`, `docs/guides/l4-services.md`, `docs/reference/storage.md`, `docs/reference/rollouts.md`, `docs/reference/k8s-compliance.md`.

## What’s Next

Next up is packaging (controller container + wheels), stricter presets for production via `k8s-check`, and more metrics (reconcile histograms and canary step counters) with sample Grafana panels.

If you try the Playground or the dev demos, let us know what workflows you want to see next.
