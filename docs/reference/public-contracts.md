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
| Version metadata | stable | Package metadata, CLI build information, OpenAPI v2/v3, and release checks resolve `ae.__version__`. |
| CRI protobuf resources | stable | Package-local CRI and gogoproto modules ship in wheel and source distributions. |

Stable here means compatibility is covered by release-contract tests; it does
not mean the project has declared a 1.0 API freeze. Runtime-starting commands
still require an explicitly selected and prepared backend.

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

Experimental surfaces must still fail safely and remain documented, but their
internal schemas and command details may change in a pre-release. A surface may
move to the stable table only with implementation, compatibility tests, and a
changelog entry.
