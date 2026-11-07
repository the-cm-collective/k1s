# Public Contract Inventory

K1s is pre-1.0 software. This inventory distinguishes compatibility surfaces
that release checks preserve from experimental features that may change between
pre-releases. The status vocabulary is deliberately limited to **stable** and
**experimental** so tests can detect an undocumented public-surface change.

## Stable compatibility surfaces

| Surface | Status | Contract |
| --- | --- | --- |
| Distribution | stable | Install releases as `k1s-core`; `ae` is not the distribution name. |
| Python package | stable | `import ae` and `ae.__version__` remain available. |
| Primary CLI | stable | `k1s --help` remains safe without starting a runtime. |
| Compatibility CLIs | stable | `ae`, `ae-rotate-certs`, `ae-node`, `ae-gateway`, and `ae-worker-stub` remain installed entry points. |
| Installed profile catalog | stable | `k1s profile list`, `doctor`, and `plan` are deterministic, non-mutating installed-contract commands. |
| Portable profile lifecycle | experimental | `k1s profile run`, `logs`, and confirmed `delete` are exact-owner contracts for `local`, `core`, `edge-gateway`, and `node`; live backend/transport qualification remains separate. |
| Canonical role bootstrap | experimental | `k1s profile bootstrap create edge-gateway\|node` creates one immutable owner-private record and returns only a secret-free public projection. |
| Explicit profile migration | experimental | `k1s profile migrate plan\|apply\|rollback` accepts only caller-selected stopped schema-v1 records and exact digest-bound state authority; it performs no ambient discovery or alias promotion. |
| Version metadata | stable | Package metadata, CLI build information, OpenAPI v2/v3, and release checks resolve `ae.__version__`. |
| CRI protobuf resources | stable | Package-local CRI and gogoproto modules ship in wheel and source distributions. |

Stable here means compatibility is covered by release-contract tests; it does
not mean the project has declared a 1.0 API freeze. Runtime-starting commands
still require an explicitly selected and prepared backend.

The installed profile catalog has its own exact posture vocabulary. `local`,
`core`, `edge-gateway`, and `node` are GA catalog contracts; `edge-dev` is
development-only; and `core-ha` is preview. GA stabilizes the catalog and plan
schema, not the deferred stateful execution path. Development-only and preview
roles remain experimental under this document's top-level vocabulary. The
production `edge-gateway` contract never includes the development worker stub.
Canonical `edge-gateway` and `node` have installed portable source contracts
and remain `source-validated/unqualified`; this is not a live qualification
claim. Their public plans/results never contain credential material, direct
credential or full-record verifiers, or private source/stored paths.

### Supported Python lanes

CPython 3.11, 3.12, and 3.13 are blocking release-qualification lanes and are
declared in package classifiers. CPython 3.14 is exploratory: failures must be
recorded, but 3.14 is not yet a supported classifier or a blocking release
lane. The `requires-python = ">=3.11"` floor permits that forward testing
without claiming that an exploratory interpreter is already supported.

## Experimental surfaces

| Surface | Status | Contract |
| --- | --- | --- |
| Inference fabric | experimental | Advisory placement and locality schemas may evolve before 1.0. |
| Fabric control plane | experimental | External WorkerBee evidence remains advisory to K1s-owned reconciliation. |
| Lab and VM automation | experimental | Host-specific validation helpers are not installed-artifact runtime contracts. |
| HA drill tooling | experimental | Drill and retained-evidence formats may evolve before 1.0. |
| Legacy profile runner | experimental | Development/preview `profile start`, `status`, `stop`, and `restart` remain checkout-local compatibility actions where supported. |

Experimental surfaces must still fail safely and remain documented, but their
internal schemas and command details may change in a pre-release. A surface may
move to the stable table only with implementation, compatibility tests, and a
changelog entry.

Compatibility migration preserves the development posture of `edge` and
`edge-core` by mapping them only to `edge-dev`. `core-edge` maps to canonical
`core` with explicit historical `nats-core` composition evidence. No migration
maps an old alias to `edge-gateway`, `node`, or `core-ha`. Planning is pure;
apply and rollback are owner-private state-only transactions with exact source,
plan, receipt, run, and scaffold identities. The source record is retained.
Imported records assert stopped composition only and provide no live-runtime,
process-ownership, backend, or WorkerBee qualification evidence.
