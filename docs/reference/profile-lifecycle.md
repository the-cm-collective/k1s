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

The separate `k1s profile host plan strict-cri` command is also non-mutating,
but it is not a role-catalog alias or portable backend. It reads two explicit,
canonical, digest-pinned files and emits a deterministic
`source-validated/unqualified` Linux/containerd plan. It does not read or
create profile locks, current/run state, bootstrap state, XDG roots, or backend
receipts; it performs no target, process, service, socket, or network probe.
Digest-bound `profile host prepare strict-cri` and `rollback strict-cri` are
explicit reviewed mutations. They require the exact plan authorities,
caller-supplied expected digests and confirmation, an owner-private
transaction root, and pre-opened anonymous-pipe file descriptors for a helper
that the CLI never launches or discovers. The installed strict-CRI node path
requires a paired exact plan and applied-receipt digest. A successful plan or
source-only prepare smoke is never a live qualification claim.

Preparation journals `planned`, `snapshotting`, `snapshotted`, `applying`, and
`applied`; rollback journals `rolling-back` and `rolled-back`. Writes are
canonical, descriptor-relative, no-follow, owner-private, same-device, atomic,
and fsynced. Crash recovery accepts only the exact write-ahead zero-progress
state or a contiguous action prefix/suffix. Unknown, duplicate, reordered,
divergent, or partially observed authority fails closed. No stale-lock timeout
or takeover exists.

## Canonical roles

| Role | Contract posture | Production eligible | Components | Execution |
| --- | --- | --- | --- | --- |
| `local` | GA | yes | controller, local node | portable; source-validated/unqualified |
| `core` | GA | yes | controller | portable; source-validated/unqualified |
| `edge-gateway` | GA | yes | gateway only | portable; source-validated/unqualified |
| `node` | GA | yes | node agent | portable; source-validated/unqualified |
| `edge-dev` | development-only | no | gateway, worker stub | deferred |
| `core-ha` | preview | no | controller replica set, HA control plane | deferred |

GA describes the stability of the installed catalog and plan schema. The
`portable` execution status for `local`, `core`, `edge-gateway`, and `node`
says that an installed source path exists; it does not claim that Podman,
Docker, the gateway transport, or the profile is live-qualified. All four
remain `source-validated/unqualified` until a later isolated WorkerBee/runtime
lane qualifies an exact engine, transport, and environment.

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

Without `--engine`, `profile doctor` performs static installation checks only:
supported Python,
Linux platform, the `ae` import, `k1s-core` distribution metadata, the `k1s`
console entry point, catalog consistency, and profile posture. It does not
enumerate or print environment values. A failed installed contract returns
exit code 1; warnings such as preview/development posture or deferred
execution return zero. `profile doctor local|core --engine podman|docker`
performs one explicitly selected, bounded, read-only capability query. It
does not fall back to another engine, start a daemon or machine, create XDG
state, acquire a mutation lock, or promote live qualification. Invalid command
usage returns 2.

`profile plan` returns declarative component identifiers, prerequisites,
posture warnings, and compatibility context. It never returns an executable
command or a mutable state path. A recognized canonical role or alias returns
zero; invalid usage returns 2.

Installed `profile start`, `status`, `stop`, and `restart` use the portable
path for canonical `local`, `core`, `edge-gateway`, and `node`. Development
and preview names retain the repository-local compatibility runner where one
exists. Canonical portable roles do not accept `--legacy`. There is no
automatic Podman/Docker fallback and no canonical-role fallback to
`edge-dev`.

The same exact-owner lifecycle surfaces accept the four portable roles:

```text
k1s profile run local|core [--backend podman|docker]
k1s profile run edge-gateway|node --bootstrap-id BOOTSTRAP
k1s profile logs ROLE --component ID [--run-id RUN] [--tail N]
k1s profile delete ROLE --run-id RUN --confirm-delete ROLE/RUN
```

`run` claims a newly generated run under the same exclusive profile lock used
by `start`; it never attaches to an already-active run. It uses the same
transaction journal, activation gate, exact process identities, readiness,
reverse-order stop, and owned cleanup as portable start/stop. `SIGINT` and
`SIGTERM` handlers only record an intent. Normal foreground control flow
performs all state, process, and filesystem actions, returning 130 or 143 after
an exact clean stop. A component exit fails the run; an identity mismatch is
fenced as orphaned and the mismatched process is never signalled.

`logs` is a bounded snapshot, never an unbounded follow. It selects either the
explicit immutable run ID or the exact recorded current run, then requires the
named component's canonical `logs/<component>.log` descriptor. The file must
remain an owner-owned, mode-`0600`, single-link regular file below the exact run
directory. The default tail is 200 lines, the accepted range is 0 through
10,000, and at most 1 MiB is read. There is no filename discovery, path
normalization, source-checkout fallback, or process/runtime access.

`delete` accepts only a stopped, cleanup-complete run with no live process
authority or resource/secret receipts, and the confirmation must equal the
literal `PROFILE/RUN`. Under the exclusive profile lock it persists an
owner-private canonical deletion intent bound to the run-state digest, removes
only that run's exact logs/data/cache trees, then records completion. A pending
intent occupies the authoritative `transaction.json` slot as well as the exact
run tombstone, so pre-checkpoint readers reject it instead of treating the run
as cleanly terminal. It rolls forward after a crash; a complete intent is
idempotent after descriptor-relative target-absence checks. The run
state and current generation authority remain so an older or unknown deletion
record fails closed and the next generation remains monotonic.

## Portable local/core transaction

`k1s-core` includes an installed portable local/core adapter. Importing it,
listing profiles, and planning profiles start nothing. An explicit start first
probes exactly the requested engine, persists its absolute executable identity
and deterministic plan digest, writes an exact current/run transaction
journal, and launches components behind an activation gate. The gate opens
only after exact process identity is durable. Bounded loopback readiness then
advances components in controller-before-node order. Stop revalidates complete
process identity and uses reverse dependency order.

The layer derives versioned application roots from the four XDG base
directories: `k1s/v1` below config, state, data, and cache. Explicit XDG paths
must be absolute and canonical. `HOME` is required only when a base needs its
standard fallback. Owned roots and run directories are mode `0700`; locks,
state, logs, and generated secrets are mode `0600`. Symlinks, foreign owners,
unsafe modes, traversal, and noncanonical identifiers fail closed and are
never repaired with `chown` or permission widening.

Each canonical profile owns a stable `profile.lock`, `current.json`,
`transaction.json`, and an immutable `runs/r1-<random-hex>/` directory.
Canonical JSON records the
profile, owner UID, monotonically increasing generation and revision, exact
run ID, phase, component process identities, and owned secret/resource
receipts. The transaction journal records complete before/after identities
for each current/run mutation. Recovery accepts only an exact before pair, an
exact completed pair, or the recognized run-after/current-before intermediate
pair; every other mixture fails closed. Atomic writes use owner-private
same-directory temporary files,
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
ownership. Logs are retained until the exact stopped-run deletion contract is
invoked. State authority and its digest-bound deletion tombstone are retained;
ambient pruning and broad filesystem cleanup are not supported.

## Canonical edge and node bootstrap

Canonical edge execution begins with one explicit owner-private bootstrap:

```text
k1s profile bootstrap create edge-gateway \
  --site-id SITE --node-id NODE --transport-url tls://HOST:PORT \
  --credential-file /absolute/owner-private/nats.creds

k1s profile bootstrap create node \
  --node-id NODE --controller-url https://HOST:PORT \
  --runtime-backend podman|docker --listen-host IP --listen-port PORT \
  --advertise-endpoint https://HOST:PORT \
  --credential-file /absolute/owner-private/token
```

The command writes one immutable canonical JSON record at the standard
owner-private XDG config location and returns only its random versioned
bootstrap ID plus the public role/identity/endpoint facts. There is no output
path option, alias selection, `latest` pointer, directory scan, or orphan
adoption. The credential input must be an absolute, owner-owned, mode-`0600`,
single-link regular file opened without following symlinks. Bootstrap files
and containing directories remain owner-private.

Every public execution-plan digest and lifecycle result is derived solely
from a secret-free projection: canonical role, bootstrap ID, identity-v1 node
ID, site and endpoint facts, selected backend/listen facts, component order,
engine executable identity where applicable, and installed Python executable
identity. Credential bytes, credential digests, the full bootstrap-record
digest, source/stored credential paths, and stored bootstrap path never enter
public output, status, logs, errors, or public digests. Exact full-record
digest and file identity stay only in private run state. The manager reopens
and revalidates that identity before every use; private replacement or drift
fails even when the public projection is unchanged.

`edge-gateway` launches exactly one gateway component. It has no hidden node,
worker stub, engine probe, or engine-manager fallback. Its NATS credential is
materialized as one exact mode-`0600` run-owned secret receipt and the child
receives only that private path. `node` launches exactly one node-agent,
always passes the bootstrap's explicit identity-v1 node ID, and uses exactly
the Podman or Docker executable identity selected by the bootstrap; its token
is supplied only through the narrowly allowlisted child environment, which is
excluded from plan repr/rendering and durable public state.

`edge`, `edge-core`, and `edge-dev` remain the development profile with its
worker stub. They cannot create or consume a canonical gateway/node bootstrap
and never enter portable dispatch.

These are packaged source contracts and deterministic fakeable boundaries,
not evidence of live Podman/Docker, gateway transport, or node qualification.
This checkpoint uses only injected engine, process, readiness, transport, and
signal boundaries in validation. Host preparation, ambient resource adoption,
WorkerBee live qualification, and distributed/HA ownership remain deferred to
separately reviewed checkpoints.

## Explicit stopped-profile migration

Compatibility migration is an explicit, state-only transaction. It never
searches `HOME`, a checkout, processes, or an XDG tree. The caller supplies one
canonical absolute legacy-record directory, one canonical destination profile,
and one existing owner-private versioned state root:

```text
k1s profile migrate plan edge \
  --legacy-root /absolute/legacy-records \
  --destination-profile edge-dev \
  --destination-root /absolute/k1s/v1/state

k1s profile migrate apply edge \
  --legacy-root /absolute/legacy-records \
  --destination-profile edge-dev \
  --destination-root /absolute/k1s/v1/state \
  --expect-plan-digest sha256:...
```

Only exact stopped schema-v1 records for `edge`, `edge-core`, and `core-edge`
are accepted. `edge` and `edge-core` remain development aliases of `edge-dev`;
they are never promoted to canonical `edge-gateway` or `node`. `core-edge`
imports canonical `core` stopped composition evidence for `controller` plus
`nats-core`; it never promotes `core-ha`. Any process identity observed for the
recorded PID, including a reused PID, is an active conflict. Inspector
ambiguity also fails closed, and migration never signals or adopts that PID.

`plan` is byte-for-byte nonmutating: it creates no destination directory or
lock. `apply` revalidates the source and PID immediately before mutation, then
creates only the exact state authority needed for an imported clean stopped
run, an immutable receipt, and a distinct migration journal. Imported
component records are historical stopped-composition evidence, not claims that
K-P1-3 started or owned a legacy process. Config, data, cache, logs, secrets,
and process state are untouched.

The apply journal recognizes only its exact before state and each ordered
receipt/run/current prefix. While it is pending, public planning and ordinary
lifecycle readers fail closed; an identical digest-bound apply is the only
roll-forward path. Rollback requires the exact migration ID, original applied
receipt digest, and `PROFILE/RUN_ID` confirmation. It removes only the exact
imported current/run objects, retains the legacy source, and replaces the
receipt with an immutable rolled-back audit record. Replaying rollback is
accepted only with the original applied-receipt digest. A directory is removed
only when its exact recorded migration-created identity still matches; an
empty directory alone is not ownership evidence. Drift, later generations,
foreign files, symlinks, hardlinks, unsafe modes, malformed records, and
unknown transaction states all fail closed.

The migration command is installed and offline-safe but is not a live-runtime
qualification claim. It performs no backend, network, service, host,
certificate, or WorkerBee action.
