# k1s CLI Reference

The installed `k1s` command is the compact, kubectl-like interface shipped by
the `k1s-core` distribution. It has two responsibilities:

1. operate common application resources; and
2. inspect or explicitly operate versioned runtime-profile contracts.

The broader `ae` command remains installed for controller, registry, TLS,
backup, export, and other engineering surfaces. `k1s` does not silently proxy
unknown commands to `ae`.

Run `k1s --help` or `k1s <command> --help` for the installed argument contract.
Help and the read-only profile commands are safe without starting a runtime.

## Install and inspect

Install the distribution from an approved package index or exact reviewed
artifact, then use the primary installed command:

```text
python -m pip install k1s-core
k1s --help
k1s profile list
k1s profile doctor core
k1s profile plan core
k1s profile doctor edge-gateway
k1s profile plan edge-gateway
k1s profile doctor node
k1s profile plan node
```

These commands are deterministic and non-mutating and report the
`source-validated/unqualified` posture. They do not start a backend, prepare a
host, or claim live WorkerBee, CRI/containerd, provider, or deployment
qualification. The installed `ae` command remains the advanced
compatibility and engineering surface. Make targets, `python -m ae.*`, and
repository `scripts/`, `specs/`, `state/`, or `ops/` paths are source-checkout
development material, not commands provided by the installed package.

## Application commands

| Command | Purpose |
| --- | --- |
| `k1s get RESOURCE [NAME]` | List or inspect apps, deployments, workloads, pods, or services. |
| `k1s describe REF` | Describe an app/deployment reference. |
| `k1s apply -f FILE` | Apply one manifest file. |
| `k1s rollout history REF` | Show bounded rollout history. |
| `k1s rollout undo REF [--to-revision N]` | Restore a recorded revision. |
| `k1s logs REF` | Read workload logs, with optional follow, container, revision, tail, or time filters. |
| `k1s events REF` | Show recent workload events. |
| `k1s delete REF [--purge]` | Delete a workload; `--purge` also removes its events and revisions. |
| `k1s scale REF --replicas N` | Reconcile the requested replica count. |

Application references accept `app/NAME`, `deployment/NAME`, or `NAME` where
the command help permits them. Use `k1s get --help` for the accepted resource
aliases. These commands use the same k1s state and reconciliation contracts as
the corresponding `ae` surfaces.

## Installed profile inspection

These commands inspect installed metadata and do not start a backend, create
profile state, prepare a host, or make a network connection:

```text
k1s profile list
k1s profile doctor ROLE [--engine podman|docker]
k1s profile plan ROLE
```

Without `--engine`, `doctor` is a static installation check. Supplying an
engine requests one bounded, read-only capability query; it does not fall back
to another engine. All three commands support text or JSON output.

## Portable profile lifecycle

Canonical `local`, `core`, `edge-gateway`, and `node` profiles use the
installed exact-owner lifecycle. Live backend and transport qualification is a
separate claim from command availability.

```text
k1s profile start ROLE [--backend podman|docker] [--bootstrap-id ID]
k1s profile status ROLE
k1s profile stop ROLE
k1s profile restart ROLE [--backend podman|docker] [--bootstrap-id ID]
k1s profile run ROLE [--backend podman|docker] [--bootstrap-id ID]
k1s profile logs ROLE --component ID [--run-id RUN] [--tail N]
k1s profile delete ROLE --run-id RUN --confirm-delete ROLE/RUN
```

`run` owns a new foreground run and performs exact cleanup after `SIGINT` or
`SIGTERM`. `logs` is a bounded snapshot. `delete` accepts only an exact stopped,
cleanup-complete run and requires the literal confirmation value shown above.

Development and preview profile names retain repository-local compatibility
runners only where the profile contract explicitly supports one. Portable
canonical roles do not accept `--legacy`, and no command silently changes
backend, profile, or role.

## Canonical bootstrap records

Edge gateway and node execution require an explicit immutable, owner-private
bootstrap record:

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

Credential inputs must be absolute, owner-owned, mode-`0600`, single-link
regular files. Public output contains only the bootstrap ID and secret-free
identity and endpoint facts.

## Explicit legacy-profile migration

Migration never discovers ambient checkouts, processes, or state. The caller
selects an exact stopped legacy record and destination:

```text
k1s profile migrate plan LEGACY \
  --legacy-root PATH --destination-profile ROLE --destination-root PATH
k1s profile migrate apply LEGACY \
  --legacy-root PATH --destination-profile ROLE --destination-root PATH \
  --expect-plan-digest sha256:...
k1s profile migrate rollback LEGACY \
  --legacy-root PATH --destination-profile ROLE --destination-root PATH \
  --migration-id ID --expect-receipt-digest sha256:... \
  --confirm-rollback ROLE/RUN
```

Only the documented stopped schema-v1 compatibility profiles are accepted.
Migration never adopts or signals a process and never promotes development
aliases to production roles.

## Strict-CRI host transactions

The pure host plan consumes only explicit, canonical, digest-pinned inputs:

```text
k1s profile host plan strict-cri \
  --intent FILE --expect-intent-sha256 SHA256 \
  --facts FILE --expect-facts-sha256 SHA256
```

`k1s profile host prepare strict-cri` and
`k1s profile host rollback strict-cri` are reviewed, digest-bound host
transactions. They require an owner-private transaction root, exact
transaction identifiers and expected evidence digests, explicit confirmation,
and pre-opened anonymous-pipe descriptors for a helper. The `k1s` CLI never
launches or discovers that helper. Use `--help` for the complete required
arguments and follow the procedure in
[CRI/containerd](cri-containerd.html).

## Stability and evidence

The `k1s` entry point and non-mutating installed profile catalog are stable
1.0-alpha compatibility surfaces. Portable lifecycle, bootstrap, migration,
and strict-CRI host transactions remain experimental and fail closed on
ambiguous ownership or evidence. See [Public Contract Inventory](public-contracts.html)
and [Installed Profile Contracts](profile-lifecycle.html) for the normative
stability, state, and security boundaries.
