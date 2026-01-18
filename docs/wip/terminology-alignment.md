# Terminology Alignment Phases (k1s -> Kubernetes)

Goal: align user-facing terminology and workflows with Kubernetes so k1s can
move toward CNCF conformance over time without breaking existing users.

Scope: this plan focuses on terminology and surface compatibility. It does not
promise full Kubernetes conformance or feature parity.

## Phase 1: Surface alignment (docs + CLI)

Objective: make user-facing language match Kubernetes terms while keeping the
native App model intact.

Deliverables:
- Docs: add a k1s <-> Kubernetes term map and prefer Kubernetes terms with the
  k1s term in parentheses on first use.
- CLI: accept Kubernetes-like aliases for App (deployment/workload synonyms);
  update help strings to say "workload (App)" instead of "application".
- No schema, API, or database changes; internal code remains App/replica.

Non-goals:
- No manifest format changes.
- No new resources (Pods/Services) exposed in the native CLI.

## Phase 2: Input/output compatibility

Objective: accept Kubernetes manifests and present Kubernetes terms in outputs.

Deliverables:
- `ae apply` accepts Kubernetes Deployment/Service/Ingress manifests via
  `--k8s` flag or auto-detect, using shared conversion logic.
- Extract conversion helpers from the API shim into a shared module
  (e.g., `src/ae/k8s/convert.py`) so CLI/docs/shim use the same mapping.
- `k1s` CLI exposes read-only `pods` and `services` views sourced from
  replica status and service endpoints.
- Deprecate "replica" in user output in favor of "pod".

Non-goals:
- No breaking changes to App manifests or existing CLI flags.
- No database migrations yet.

## Phase 3: Core model alignment

Objective: align native manifests, storage, and labels with Kubernetes terms.

Deliverables:
- Introduce a native `Deployment`-named manifest (same schema as App) and mark
  `App` as a compatibility alias with deprecation warnings.
- Add namespace support to native manifests (`metadata.namespace`) and update
  storage keys to be namespace-aware.
- Adopt Kubernetes standard labels (`app.kubernetes.io/*`) while preserving
  existing `ae.app` labels for backward compatibility.
- Align metrics and status outputs with Kubernetes naming conventions.

Non-goals:
- Full Kubernetes conformance and admission-webhook parity are tracked in
  `docs/wip/conformance.md`.
