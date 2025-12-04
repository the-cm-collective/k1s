# k1s Overview

k1s is a tiny, single‑node application engine that lets you declare apps in YAML, run them with a container runtime (Podman preferred; Docker supported), and expose them via Caddy. It aims to give you the everyday ergonomics of a small Kubernetes subset without the operational weight.

- Goal: simple, predictable rollouts for 1–3 services on a small VPS.
- Non‑goals: multi‑node scheduling, CRDs, complex RBAC, full Kubernetes API.

## Current State (Dec 2025)

k1s now ships a Podman‑first controller with Docker fallback, Caddy ingress, SQLite state, and a thin `ae` CLI plus kubectl‑style `k1s` wrapper. Specs cover rolling deploys, health probes, config/secret projection (SOPS/age aware), storage, security context, observability, and a small HTTP API. Everything is packaged to stay comfortable on a 1‑CPU/2‑GB VPS while still feeling like day‑to‑day Kubernetes.

Progress through mid‑November 2025 delivered multi‑container runtime with init containers, canary rollouts with persisted weights, strict health gating, HPA/PDB/PVC/ServiceAccount support, and `export-k8s` coverage for Deployment/StatefulSet/Ingress/Service plus optional HPA/PDB/SA/PVC/NetworkPolicy. Podman defaults to `crun` (2025‑11‑10) for faster startup and lower memory, RBAC emitters, Job/CronJob exporters, percent PDB flags, and PodSecurity label presets all landed on 2025‑11‑12.

Compatibility is a first‑class focus: `export-k8s --validate` and `k1s k8s-check --policy strict` emit upstream‑clean YAML, and the in‑progress Kubernetes API shim targets Helm/kubectl workflows (discovery, core CRUD/watch, Deployment/Service/Ingress translation with status/scale) so more charts work unmodified than on other lightweight K8s‑like stacks.

Footprint remains well below k3s. Recent bench runs in `combined/combined.csv` (2025‑12‑02) show k1s control‑plane PSS around 338–345 MB on Podman+crun (rootless), while comparable k3s runs from 2025‑11‑21 sit near 960–1,020 MB. Rollout snapshots hold the same ~3× gap, keeping us smaller than k3s while aiming for higher Kubernetes compatibility than other micro platforms.

## Features (High‑Level)

- Declarative specs: `apiVersion: ae.dev/v1alpha1`, `kind: App`.
- Rolling deploys and rollback with health gates; pause/resume via `ae rollout`.
- Probes: HTTP, TCP, and Exec with initial delays/timeouts.
- Ingress via Caddy site fragments + reload; TLS via BYO certs or k8s‑style secrets.
- Configs and Secrets: project keys to env and files (SOPS/age supported).
- Storage: per‑app named volumes (PV‑lite) with `retention: Retain|Delete`.
- Security: runAsUser/runAsGroup, readOnlyRootFilesystem, dropCapabilities.
- Registry auth from `~/.config/ae/registries.yaml`.
- Events and metrics; CLI snapshot and Prometheus text endpoint.
- CLIs: `ae` (native) and `k1s` (kubectl‑like wrapper).
- HTTP API (read‑mostly): `/metrics`, `/health`, `/status`, `/status/<app>`, `/events/<app>`, `/logs/<app>`, `/openapi.json`, `/docs`, `/dashboard`.
- K8s helpers: `export-k8s`, `k8s-check`, and `k8s-report` for parity/compliance.

### Architecture at a Glance (Diagram)

```mermaid
flowchart LR
  A[Specs/*.yaml] -->|load| C[Controller]
  C -->|ensure| R[(Container Runtime)]
  C -->|health| H[Probes]
  C -->|write| S[(SQLite State)]
  C -->|render+reload| I[Caddy Ingress]
  C -->|expose| API[HTTP API: /status /metrics]
  CLI[ae / k1s CLI] -->|reads| S
  CLI -->|apply/rollback| C
```

## Requirements

- Python 3.11+
- Podman (preferred) or Docker
- Optional: Caddy (for ingress), SOPS/age (for secrets), watchdog (for live file watch)

## Install

- Dev install: `python -m pip install -e .[dev]`
- Optional file watching: `python -m pip install -e .[watch]`

## Getting Started

1) Start the controller loop

- Polling only: `python -m ae.controller --loop --specs specs/ --metrics-port 9108`
- With file watch (if `watchdog` installed): `python -m ae.controller --loop --watch --specs specs/ --metrics-port 9108`

2) Apply a sample app

- `python -m ae.cli apply -f specs/examples/echo.yaml`

3) Inspect status, events, logs

- `python -m ae.cli status echo --wide --events`
- `python -m ae.cli logs echo --tail 50`

4) Kubectl‑like aliases

- `k1s get apps`
- `k1s describe app/echo`
- `k1s logs app/echo --follow --tail 100`

5) API and metrics

- JSON and metrics: `curl :9108/status`, `curl :9108/metrics`
- OpenAPI and docs: `curl :9108/openapi.json`, open `http://localhost:9108/docs`

## Configuration (Quick Reference)

- AE_STATE_DB: path to SQLite DB (default `state/controller.db`)
- AE_SPECS_DIR: specs directory (default `specs`)
- AE_CADDY_SITES, AE_CADDY_BIN, AE_CADDY_FILE, AE_CADDY_CONTAINER: ingress tuning
- AE_REGISTRY_CONFIG: registry credentials file (default `~/.config/ae/registries.yaml`)
- AE_ALLOW_PLAINTEXT_SECRETS=1: allow bypassing SOPS (dev only)
- AE_LOG_LEVEL: logging level (DEBUG/INFO/…)

## Project Layout

- src/ae/controller: reconcile loop, state, health, spec models
- src/ae/runtime: Podman (default) and Docker adapters, registry auth, stub runtime
- src/ae/ingress: Caddy templating + reload
- src/ae/cli and src/ae/kctl: CLIs (`ae`, `k1s`)
- src/ae/observability: metrics, HTTP API, logging
- specs/: example manifests and secrets
- docs/: runbook, HTTP API, testing, this overview

## Further Reading

- Runbook: `docs/runbook.md`
- End-to-End Guide: `docs/e2e.md`
- HTTP API: `docs/http-api.md`
- Architecture (detailed): `docs/architecture.md`
