# Installed Profile Contracts

K1s exposes an installed, non-mutating profile catalog through three commands:

```text
k1s profile list
k1s profile doctor <role-or-alias>
k1s profile plan <role-or-alias>
```

Each command supports `--output text` and `--output json`. They inspect only
the installed profile contract. They do not start a backend, create state,
prepare a host, open a network connection, invoke a service manager, or call
the legacy lifecycle runner.

The JSON contract uses `apiVersion: k1s.dev/v1alpha1`, `schemaVersion: 1`, and
`mutating: false`. Catalog ordering and output fields are deterministic.

## Installed catalog resource

The catalog is installed by `k1s-core` as the package resource
`ae/resources/profiles/profile-catalog-v1.json`. That canonical JSON document
contains only the static role, posture, component, requirement, alias, and
deferred-execution contract described on this page. It contains no executable
command, credential, environment setting, port, state path, backend setting,
service definition, supervisor configuration, or host-preparation instruction.

The loader accepts only canonical package-relative path components and does
not fall back to the source checkout, current directory, legacy profile
runner, environment, or network. Missing, malformed, duplicated-key,
noncanonical, or semantically inconsistent catalog data is an invalid
installation and fails closed. The wheel and source-distribution validators
require the installed bytes to match this source resource exactly, and the
wheel `RECORD` must cover it with the correct SHA-256 and size.

This resource is static package construction, not profile installation or
execution. Reading it does not create state, select a backend, prepare a host,
start a supervisor, or activate a runtime.

## Canonical roles

| Role | Contract posture | Production eligible | Components | Execution |
| --- | --- | --- | --- | --- |
| `local` | GA | yes | controller, local node | deferred |
| `core` | GA | yes | controller | deferred |
| `edge-gateway` | GA | yes | gateway only | deferred |
| `node` | GA | yes | node agent | deferred |
| `edge-dev` | development-only | no | gateway, worker stub | deferred |
| `core-ha` | preview | no | controller replica set, HA control plane | deferred |

GA describes the stability of the installed catalog and plan schema. It does
not claim that a stateful profile execution path has been implemented or
release-qualified. Stateful execution, supervision, state ownership, backend
selection, host preparation, and rollback belong to later lifecycle
checkpoints.

`core-ha` is not production eligible until a later release-qualification
checkpoint explicitly promotes it.

The production `edge-gateway` contract contains only the gateway component.
It never resolves through `edge-dev` and never includes the development worker
stub.

## Compatibility aliases

The original profile names remain inspectable without changing their meaning:

| Alias | Canonical role | Compatibility variant | Safety rule |
| --- | --- | --- | --- |
| `edge` | `edge-dev` | none | development-only and never production eligible |
| `core-edge` | `core` | `nats-core` | records the legacy transport variant but starts nothing |
| `edge-core` | `edge-dev` | `core-peering` | development-only and never resolves to `edge-gateway` |

The repository runner labels `k1s-core`, `k1s-edge`, and `k1s-ha-core` are
adapter metadata, not additional public profile aliases.

## Command behavior

`profile list` emits all canonical roles, aliases, postures, component IDs,
and deferred requirements. It exits zero.

`profile doctor` performs static installation checks only: supported Python,
Linux platform, the `ae` import, `k1s-core` distribution metadata, the `k1s`
console entry point, catalog consistency, and profile posture. It does not
enumerate or print environment values. A failed installed contract returns
exit code 1; warnings such as preview/development posture or deferred
execution return zero. Invalid command usage returns 2.

`profile plan` returns declarative component identifiers, prerequisites,
posture warnings, and compatibility context. It never returns an executable
command or a mutable state path. A recognized canonical role or alias returns
zero; invalid usage returns 2.

The existing `profile start`, `status`, `stop`, and `restart` commands remain
legacy compatibility actions backed by the repository-local profile runner.
They are not redefined by this installed contract and are not evidence that
the deferred role execution paths above are ready.

## Installed state and supervision primitives

`k1s-core` includes an additive, internal state and supervision layer for a
later portable lifecycle adapter. It is not wired to the profile CLI in this
checkpoint, does not select a backend, and starts nothing merely by being
imported. The existing lifecycle commands and the catalog's `deferred`
execution posture remain unchanged.

The layer derives versioned application roots from the four XDG base
directories: `k1s/v1` below config, state, data, and cache. Explicit XDG paths
must be absolute and canonical. `HOME` is required only when a base needs its
standard fallback. Owned roots and run directories are mode `0700`; locks,
state, logs, and generated secrets are mode `0600`. Symlinks, foreign owners,
unsafe modes, traversal, and noncanonical identifiers fail closed and are
never repaired with `chown` or permission widening.

Each canonical profile owns a stable `profile.lock`, `current.json`, and an
immutable `runs/r1-<random-hex>/` directory. Canonical JSON records the
profile, owner UID, monotonically increasing generation and revision, exact
run ID, phase, component process identities, and owned secret/resource
receipts. Atomic writes use owner-private same-directory temporary files,
file and directory `fsync`, and reject an unsafe existing destination before
replacement. Inspection is read-only: an absent profile returns absent
without creating XDG directories or a lock; authoritative state without its
pre-existing valid lock is an error.

The durable phases are `preparing`, `starting`, `running`, `stopping`,
`stopped`, `failed`, and `orphaned`, with explicit legal transitions. Process
authority requires PID, process group, session, kernel boot ID, procfs start
time, and executable device/inode. Stop and recovery revalidate the complete
identity before every process-group signal. A reused PID or any identity
mismatch is fenced as orphaned and is never signalled.

Generated secrets and non-process resources carry exact run/profile ownership
receipts. Cleanup uses only the recorded allowlisted handler and exact secret
device/inode, in reverse resource order. Successful receipts are removed;
failed and pending receipts remain for deterministic same-run retry. No glob,
prefix, process scan, socket scan, or ambient backend discovery grants
ownership. Logs and stopped-run state are retained; deletion and pruning are
later lifecycle work.

These are packaged source contracts and deterministic fakeable boundaries,
not evidence of live supervisor or backend enforcement. Portable backends,
CLI activation, legacy-state migration, restart policy, host preparation,
run deletion, WorkerBee integration, and distributed/HA ownership remain
deferred to separately reviewed checkpoints.
