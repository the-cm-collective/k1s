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
