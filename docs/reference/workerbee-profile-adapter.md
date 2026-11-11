# WorkerBee profile evidence adapter

K1s ships one experimental, metadata-safe adapter for later WorkerBee
consumers. It adds no distribution or console. The pure Python API in
`ae.kctl.profile_adapter` invokes no lifecycle command and opens no runtime or
network connection. The adapter accepts an already selected successful K1s JSON
result and returns a strict allowlist projection. Its exact v1 wire and local
same-runtime consumer fixture are source/synthetic/local-contract qualified.
Its public qualification posture is still always
`source-validated/unqualified` because that evidence does not qualify a live
provider, MCP channel, runtime, or deployment.

The installed contract resource is
`ae/resources/profiles/workerbee-adapter-v1.json`. It is canonical UTF-8 JSON
with sorted keys, two-space indentation, and one final LF. Duplicate, unknown,
missing, reordered operation, or wrongly typed fields fail closed. Its exact
top-level fields are:

| Field | Exact value |
| --- | --- |
| `apiVersion` | `k1s.dev/workerbee-profile-adapter/v1` |
| `evidenceApiVersion` | `k1s.dev/workerbee-profile-evidence/v1` |
| `kind` | `WorkerBeeProfileAdapterContract` |
| `qualificationPosture` | `source-validated/unqualified` |
| `schemaVersion` | integer `1` |
| `operations` | `list`, `doctor`, `plan`, `status`, `logs`, `bootstrap`, in that order |

Every operation descriptor has exactly `acceptedExitCodes`,
`adapterMutating`, `name`, `nodeObservation`, `sourceApiVersion`, `sourceKind`,
`sourceMode`, `sourceMutating`, and `sourceSchemaVersion`. The resource is the
normative declaration of the exact literals. All adapter operations are
non-mutating. Bootstrap alone accepts an optional node observation; the other
five forbid it.

## Common evidence envelope

Evidence has exactly nine keys:

- `apiVersion`: `k1s.dev/workerbee-profile-evidence/v1`.
- `evidenceId`: the deterministic identity described below.
- `kind`: `WorkerBeeProfileEvidence`.
- `nodeIdentity`: null except for bootstrap.
- `operation`: one of the six declared operations.
- `payload`: the operation-specific object below.
- `provenance`: the exact source contract identity.
- `qualificationPosture`: `source-validated/unqualified`.
- `schemaVersion`: integer `1`.

`provenance` has exactly `adapterApiVersion`, `distribution`,
`distributionVersion`, `sourceApiVersion`, `sourceExitCode`, `sourceKind`,
`sourceMode`, and `sourceSchemaVersion`. The distribution is exactly
`k1s-core`; its version equals installed `ae.__version__`. The remaining
values equal the selected resource descriptor. Boolean values are never
accepted as integers.

There is no common outcome field. List, plan, logs, and bootstrap project only
successful accepted results. Doctor uses `pass`, `warn`, or `fail`. Status uses
the lifecycle vocabulary below. Adapter failures produce no evidence and
expose only `workerbee profile adapter failed closed` plus one stable code:
`invalid-input`, `unsupported-schema`, `unsupported-operation`,
`source-exit-mismatch`, `source-shape-mismatch`, `unsafe-metadata`,
`size-limit`, `node-observation-mismatch`, or `internal`.

## Operation payloads

### List

Source: exit 0 from `k1s profile list --output json`, with the exact
`ProfileCatalog` v1 schema. Evidence payload has only `profiles`, containing
the six roles in order: `local`, `core`, `edge-gateway`, `node`, `edge-dev`,
and `core-ha`.

Each profile has exactly `aliases`, `components`, `contractPosture`,
`executionStatus`, `name`, `productionEligible`, `qualificationPosture`, and
`requirements`. Each alias has only `compatibilityVariant` and `name`.
Descriptions and compatibility warnings are deliberately omitted. The
installed catalog is the authority for every value and array order.

### Doctor

Source: `k1s profile doctor NAME --output json` without an engine probe.
Exit 0 is accepted for `pass` or `warn`; exit 1 is accepted only for `fail`.
`engineCapability` must be null. Checks are ordered exactly as
`catalog.valid`, `python.supported`, `platform.linux`, `import.ae`,
`distribution.k1s-core`, `console.k1s`, `profile.posture`, followed by either
`execution.portable`, `backend.qualification` or `execution.deferred`.

The payload has exactly `alias`, `canonicalName`, `checks`,
`compatibilityVariant`, `contractPosture`, `executionStatus`,
`productionEligible`, `qualificationPosture`, `requestedName`, and `status`.
Each check exposes only `id` and `status`. Overall status is recomputed: any
failed check means `fail`; otherwise any warning means `warn`; otherwise
`pass`. Check summaries and all engine/path/platform prose are omitted.

### Plan

Source: exit 0 from `k1s profile plan NAME --output json`. The payload has
exactly `alias`, `bootstrapRequired`, `canonicalName`,
`compatibilityVariant`, `components`, `contractPosture`, `executionStatus`,
`productionEligible`, `qualificationPosture`, `requestedName`, `requirements`,
`supportedBackends`, and `warningCodes`. All fields must equal the installed
catalog plan. Raw warning text is replaced, in source order, by this complete
map:

| Source warning | Evidence code |
| --- | --- |
| `Legacy 'core-edge' records the nats-core transport variant; no transport is started.` | `compatibility.core-edge.nats-core` |
| `Legacy 'edge' selects the development worker-stub profile, not edge-gateway.` | `compatibility.edge.edge-dev` |
| `Legacy 'edge-core' remains development-only and never resolves to edge-gateway.` | `compatibility.edge-core.edge-dev` |
| `The installed portable source path is validated; live Podman/Docker qualification requires the separately reviewed isolated runtime lane.` | `execution.portable.source-validated-unqualified` |
| `Stateful execution is deferred to later lifecycle checkpoints.` | `execution.deferred` |
| `This profile is development-only and is not production-eligible.` | `posture.development-only` |
| `This profile is preview and is not production-eligible until a later release-qualification checkpoint promotes it.` | `posture.preview` |

Unmapped or duplicate warnings fail closed.

### Status

Source: exit 0 from portable `k1s profile status ROLE --output json`, with
exact keys `action`, `backend`, `exit_code`, `generation`, `phase`, `profile`,
`qualification_posture`, `run_id`, and `status`. The payload has exactly
`backend`, `lifecycleStatus`, `phase`, `profile`, `profileGeneration`,
`qualificationPosture`, and `runRef`.

Portable roles are `local`, `core`, `edge-gateway`, and `node`. Backends are
null, `podman`, `docker`, `gateway`, or `strict-cri`. Phases are null,
`preparing`, `starting`, `running`, `stopping`, `stopped`, `failed`, or
`orphaned`. Lifecycle status adds `absent` and `deleted`. Absent requires all
authority fields null. Non-absent requires a positive profile generation and
canonical run ID; status equals phase except `deleted`, which requires
`stopped`. `profileGeneration` is profile state and is never a node lease
generation.

### Logs

Source: exit 0 from an owned bounded logs snapshot with exactly `apiVersion`,
`component`, `kind`, `lines`, `profile`, `runId`, and `truncated`. At most 200
lines, 8192 UTF-8 bytes per line, and 1 MiB total are accepted. Component must
belong to the selected portable role.

The payload has only `component`, `contentPolicy` (`omitted`), `lineCount`,
`profile`, `runRef`, and `sourceTruncated`. No line, substring, event, severity,
timestamp, exception, or line/log digest is emitted.

### Bootstrap

Source: exit 0 from canonical bootstrap create for `edge-gateway` or `node`.
The exact source keys are `advertiseEndpoint`, `backendId`, `bootstrapId`,
`controllerUrl`, `identityVersion`, `listenHost`, `listenPort`, `nodeId`,
`profile`, `siteId`, and `transportUrl`. Evidence payload has only `backendId`,
`bootstrapId`, `connections`, `profile`, and `siteId`. Node ID and identity
version appear only in `nodeIdentity`.

`connections` has exactly `controller`, `transport`, `listen`, and `advertise`.
Each fact has `addressClass`, `portPresent`, `presence`, `schemeClass`, and
`securityClass`. An absent fact is exactly null/null/null with
`presence=absent` and `portPresent=false`. Present facts classify only:

- address: `hostname`, `ipv4`, or `ipv6`;
- scheme: `http`, `https`, `nats`, `tls`, or null for listen;
- security: `cleartext`, `tls`, or null for listen;
- whether a port was explicitly present, never the port value.

Gateway requires only transport; node requires controller, listen, and
advertise. URLs use lowercase exact schemes, canonical ASCII hostname or IP
literals, and an optional canonical decimal port in 1–65535. Userinfo,
credentials, path, query, fragment, percent encoding, Unicode/IDNA input,
zone IDs, noncanonical IP spelling, IP-lookalike hostnames, trailing dots,
default-port inference, and ambiguous ports fail closed. Listen accepts only a
canonical IPv4 or RFC 5952 IPv6 literal plus a numeric port. Raw authority,
host, IP, path, query, userinfo, and port values are discarded before evidence
construction.

## Node observation

The optional bootstrap observation has exactly `apiVersion`, `generation`,
`identityMode`, `identityVersion`, `kind`, `nodeId`, `provenance`, and
`schemaVersion`. It requires `k1s.dev/node-lease-observation/v1`, positive
signed-int64 generation, explicit identity v1, `NodeLeaseObservation`, exact
bootstrap node ID, `k1s-controller-lease`, and schema 1.

Without an observation, bootstrap `nodeIdentity` has explicit identity v1,
the source node ID, null generation, and `unobserved` provenance. With an exact
observation it has that positive generation and `k1s-controller-lease`
provenance. Other operations always use null. Legacy, hostname-derived,
client-counted, zero, negative, overflow, mismatched, or untrusted observations
fail closed. This structural binding is not peer or transport authentication;
W-P0-1 and Q1 must bind the real controller channel.

## Canonical bytes and redaction

Evidence uses UTF-8, NFC strings, sorted object keys, compact separators,
semantic array order, and one LF. Duplicate keys, floats, JSON constants,
surrogates, NUL, controls, noncanonical input, unknown fields, and unsupported
versions fail closed.

`runRef` is `sha256:` plus SHA-256 of
`b"k1s-workerbee-run-ref-v1\0"` followed by compact canonical
`{"profile":...,"runId":...}` plus LF. `evidenceId` uses
`b"k1s-workerbee-profile-evidence-v1\0"` followed by the compact canonical
eight-key envelope with only `evidenceId` omitted, plus LF. Verification
revalidates the full schema, recomputes the ID with constant-time comparison,
and requires byte equality for serialized input.

The projection allowlist forbids credentials, tokens, leases, sessions, raw
run IDs, secret/private/full-record/credential/log digests, protected local
values or paths, HOME/XDG/transaction/receipt/backup paths, executable paths,
helper descriptors, process identities, environment values, provider/cloud
IDs, runtime socket details, raw connection values, timestamps, arbitrary
errors, summaries, warnings, and log content. Project-scoped explicit `nodeId`
is deliberately public. The adapter creates no secret-derived pseudonym.

## W-P0-1 consumer binding and W4 evidence

The W-P0-1 K1 compatibility review requires no v1 wire change. A single
same-runtime provider may acquire the selected `list`, `doctor`, `plan`,
`status`, and `logs` source results through the WorkerBee MCP boundary and pass
each payload and exit code directly to `adapt_workerbee_profile_evidence`.
Source acquisition and evidence projection must use the same installed
`k1s-core` runtime so that resource bytes, `ae.__version__`, provenance, and
validation code cannot diverge.

Bootstrap is deliberately different. The read-only provider must never invoke
`profile bootstrap create`. It may adapt only a public bootstrap projection
that was already selected by an independently authorized workflow. The
descriptor's `sourceMutating=true` records the origin of that projection; it
does not grant the adapter or WorkerBee permission to create a bootstrap.
`adapterMutating` remains false for all six operations.

WorkerBee W4 must retain, for each operation in the exact resource order, the
canonical evidence bytes, `evidenceId`, SHA-256 of those bytes, operation name,
and complete evidence `provenance`. It must also retain the exact installed
adapter resource bytes and their SHA-256 once for the six-record set. The W4
set is incomplete unless it contains exactly `list`, `doctor`, `plan`,
`status`, `logs`, and `bootstrap`.

The retained `logs` record contains only owned component metadata,
`lineCount`, `sourceTruncated`, and pseudonymous `runRef`; raw source lines are
not retained. The retained bootstrap record is the canonical evidence output,
not the selected source projection or private bootstrap record. W4 must not
retain source log text, raw run IDs, connection values, credentials, tokens,
source/stored paths, private record digests, or bootstrap creation authority.
Replaying each retained canonical evidence object through
`verify_workerbee_profile_evidence` must reproduce its object exactly.

This qualification proves that `k1s-core 1.0.0a3` is wire-compatible with the
locked W-P0-1 consumer design. It does not qualify the live MCP transport,
authenticate the provider process, authorize bootstrap creation, or replace
the combined disposable-guest qualification gate.

The RC1 source checkpoint preserves that exact v1 wire contract. A current
`1.0.0rc1` same-runtime replay is required before its evidence can be accepted
for RC review; this source update alone is not an RC qualification claim.

## Compatibility and qualification boundary

V1 rejects unknown fields and versions; consumers do not ignore additive
fields. A wire change requires a new schema identity and documented overlap.
There is no `latest` alias or fallback. The adapter resource and module are
owned by the existing `k1s-core` wheel and `RECORD`; they add no distribution
or console. Installed validation runs all six synthetic projections outside a
checkout with indexes and sockets disabled.

This contract does not inspect WorkerBee, authenticate an observation, start a
profile, read a runtime, or qualify a live engine/transport/CRI boundary. The
K1 compatibility lane at `c9178a5235b695784ed839f31901d476afdd6a98`
qualifies the exact resource, canonical projection, redaction rules, and
six-operation consumer fixture without changing the v1 wire. The remaining
W-P0-1 and Q1 live gates—same-runtime MCP acquisition, provider-process
identity, retained W4 evidence, and combined disposable-guest qualification—
remain mandatory before RC promotion or any live-qualified claim.
