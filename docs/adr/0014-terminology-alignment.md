# ADR 0014 - Terminology alignment with Kubernetes

Date: 2026-01-18
Status: Accepted
Owners: controller/cli/docs/observability

## Context
- k1s uses a native App model that differs from Kubernetes terminology.
- Users expect Kubernetes-aligned names, labels, and outputs as k1s moves toward conformance.
- Backward compatibility is required for existing manifests and CLI workflows.

## Decision
- Prefer Kubernetes terms in docs and CLI while keeping App as a compatibility alias.
- Accept Kubernetes manifests (Deployment/Service/Ingress) via shared conversion helpers.
- Introduce a native Deployment manifest alias for App with deprecation warnings on App usage.
- Add namespace support to native manifests and namespace-aware storage keys.
- Adopt Kubernetes standard labels (`app.kubernetes.io/*`) while preserving `ae.*` labels.
- Align metrics and status outputs with Kubernetes naming conventions.

## Consequences
- Users can apply Kubernetes manifests and read Kubernetes-aligned status/metrics.
- App remains supported but emits deprecation guidance to migrate to Deployment.
- Storage keys, selectors, and labels become namespace-aware and Kubernetes-compatible.

## References
- `src/ae/k8s/convert.py`
- `src/ae/controller/spec.py`
- `docs/wip/conformance.md`
