# Node identity and incarnation fencing

K1s treats a node ID as a stable project-scoped identity and a generation as
the identity's current process incarnation. Together, `(node_id, generation)`
is the public reference that later integrations, including WorkerBee adapters,
may retain.

This contract covers registration and heartbeat ownership. It does not stop a
runtime process, evict workloads, replace controller leadership fencing, or
prove that a runtime has enforced a command.

## Canonical node IDs

Stable identity requires an explicit node ID. `ae-node --node-id` takes
precedence over `AE_NODE_ID`; the local controller uses `AE_NODE_ID`. A node ID
is case-sensitive, is 1–253 ASCII characters, starts and ends with an
alphanumeric character, and otherwise contains only alphanumeric characters,
periods, underscores, or hyphens. K1s rejects values that need trimming or path
normalization.

An edge ID is scoped as `<site_id>--<local_node_id>`. Applying the same scope
twice is idempotent. Applying a different scope to an already scoped ID is an
error. `name`, hostname, endpoint, labels, and session identifiers are not node
identity.

The canonical installed `node` profile requires an immutable owner-private
identity-v1 bootstrap created with `k1s profile bootstrap create node`. The
bootstrap records the exact node ID, controller and advertise endpoints,
listen address, and one selected Podman or Docker backend. Portable execution
always passes that explicit ID to the node agent; it does not derive identity
from hostname, profile name, an alias, or a development stub. The private node
token is delivered only to the child process and is not part of public plan,
status, log, error, or digest output.

The canonical `edge-gateway` bootstrap scopes its explicit local ID as
`<site_id>--<local_node_id>` and starts only the gateway component. The
development aliases `edge` and `edge-core` continue to resolve to `edge-dev`;
they never select or reinterpret this production identity contract.

Hostname fallback remains a pre-v1 compatibility mode. Such records have
`identity_version: 0` and `identity_mode: legacy`; consumers must not treat
them as the stable explicit contract.

## Claim and heartbeat state machine

K1s owns all timestamps. A client timestamp cannot extend a lease.

- The first explicit claim creates generation 1 and returns an opaque lease.
- Retrying the claim with the same process session returns that lease without
  increasing the generation.
- A heartbeat must present the matching session and lease.
- A different live session, a wrong lease, or an unfenced heartbeat is rejected
  without changing node metadata or readiness.
- At `now >= lease_expires_at`, a new session may replace the stale owner. The
  replacement receives a new opaque lease and the next generation.
- Heartbeats from an older generation remain rejected after replacement.

SQLite serializes claims in one write transaction. The etcd store compares the
lease record's modification revision before replacing it. Concurrent losers
fail closed.

Operator cordon state survives an incarnation replacement. An auto-assigned Pod
CIDR also survives when the replacement omits one. Other node metadata belongs
to the accepted claimant.

## Legacy migration

An existing unleased record remains readable and uses the legacy heartbeat
grace calculation. K1s does not silently claim a fresh legacy record. Stop the
legacy sender, wait one configured heartbeat TTL, and then start the explicit
identity-v1 claimant. Once an explicit lease exists, an unfenced legacy sender
cannot update that ID.

The additive lease generation defaults to zero for legacy data. Rolling code
back leaves that column harmless, but all identity-v1 claimants must be stopped
before rollback so that removing the fence cannot admit an older process.

## HTTP heartbeat contract

An explicit `POST /v1/heartbeat` includes:

```json
{
  "identity_version": 1,
  "node_id": "site-a--node-1",
  "session_id": "process-uuid",
  "lease_id": "opaque-token-on-renewal",
  "status": "Ready"
}
```

The first request omits `lease_id`. A successful response includes the
canonical node ID, generation, opaque lease, server expiry, and server-selected
renewal interval. Identity conflicts return HTTP 409 and perform no node or
heartbeat write. The lease and session are claimant secrets and are not exposed
by node inventory.

Inventory may expose these non-secret fields:

- `node_id`
- `identity_version`
- `identity_mode`
- `generation`
- `site_id` and `local_node_id`
- `status` and `seen_at`
- `lease_expires_at`

Consumers of the stable contract accept only `identity_version: 1` and
`identity_mode: explicit`, and associate observations with both `node_id` and
`generation`. A changed generation invalidates assumptions about the previous
process incarnation.

## Deliberate limits

K1s continues to use controller-epoch fencing for mutating runtime commands.
Node-incarnation fencing does not replace it. Per-node credential issuance,
persistent host installation identity, node drain or eviction, and the
WorkerBee live adapter remain separate checkpoints. The installed lifecycle
stores credential material and private bootstrap identity only in exact
owner-private XDG records; that proves deterministic request construction and
ownership, not live runtime enforcement.
