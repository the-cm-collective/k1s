# Public Contract Inventory

K1s is in its 1.0 alpha line. This inventory is the public compatibility
boundary for `1.0.0a1` and its `1.0.0a2`/`1.0.0a3` publication corrections: it
distinguishes release-gated surfaces from experimental features that may change
between pre-releases. The status
vocabulary is deliberately limited to **stable** and **experimental** so tests
can detect an undocumented public-surface change.

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
| Strict-CRI host plan | experimental | `k1s profile host plan strict-cri` consumes only two explicit canonical digest-pinned files and emits a deterministic secret-free `source-validated/unqualified` plan without target probing, state creation, socket access, or host mutation. |
| Strict-CRI prepare/rollback and node adapter | experimental | Explicit digest-bound prepare/rollback journals exact owner-private authority and uses only a caller-supplied anonymous-pipe helper protocol; the node adapter requires the exact applied receipt. Source validation does not claim helper authentication or live CRI qualification. |
| WorkerBee profile evidence adapter | experimental | `ae.kctl.profile_adapter` projects six already selected profile JSON results into exact metadata-safe v1 evidence. It adds no command or distribution and remains source-validated/unqualified pending W-P0-1 and Q1. |
| Version metadata | stable | Package metadata, CLI build information, OpenAPI v2/v3, and release checks resolve `ae.__version__`. |
| CRI protobuf resources | stable | Package-local CRI and gogoproto modules ship in wheel and source distributions. |

Stable here means compatibility is covered by release-contract tests and is a
1.0 compatibility target. During alpha and beta, an incompatible correction
requires an explicit changelog entry, migration guidance, and contract-test
updates; the final API freeze occurs only at stable `1.0.0`. Runtime-starting
commands still require an explicitly selected and prepared backend.

The contract does not turn experimental rows into stable ones, claim complete
Kubernetes semantics, or imply production suitability. Exact artifacts,
runtime backends, WorkerBee integration, and supported upgrade paths retain
their independent qualification gates.

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

The strict-CRI host plan and source-only prepare/rollback/adapter proof likewise
are not live qualification. Their caller-attested
facts and public plan digest do not prove containerd/CNI configuration, socket
access, registry trust, runtime-handler availability, or node operation. Those
claims require Q1 disposable-guest qualification of the exact guest image,
helper bytes and launch authority, process/fd topology, peer identity, applied
host state, and CRI node behavior.

The WorkerBee adapter is likewise structural evidence, not a live channel. Its
installed resource fixes operation/source identities, its payloads are strict
allowlists, and its optional node observation binds only an explicit
identity-v1 node ID and server lease generation. Raw logs and endpoint values
are omitted. Unknown fields or versions fail closed. The complete byte and
redaction contract is `docs/reference/workerbee-profile-adapter.md`.
