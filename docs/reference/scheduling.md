## Scheduling & Placement

The controller includes a lightweight scheduler for multi-node runs. It:
- Filters nodes by readiness/staleness (`AE_NODE_NOTREADY_AFTER`), cordon state, `nodeSelector`, and taints/tolerations.
- Pins all replicas to a single node when `spec.storage` is declared (retained volumes stay on the node that created them).
- Spreads across eligible nodes round-robin by default; honors `topologySpreadConstraints` when a topology key is present.
- Falls back to the local runtime when no nodes are eligible (emits a planner warning).

### Planner Dry Run

```
ae plan -f specs/examples/echo-multinode.yaml --verbose
```

Verbose output shows replica IDs, chosen nodes/endpoints, storage bindings, and Service VIP hints. Warnings include stale nodes, missing labels for selectors, or hostPort use when VIPs are enabled.

### Managing Nodes

- List/describe: `ae nodes list`, `ae nodes <node-id>`
- Cordon/uncordon: `ae nodes <node-id> --cordon` / `--uncordon`
- Drain (best-effort evict via agent): `ae nodes <node-id> --drain`
- Node labels: set on the agent via env `AE_NODE_LABELS=role=worker,zone=az1`. Taints are respected when present on node records (planner will require tolerations).

### Spec knobs that affect placement

- `spec.nodeSelector` — must match node labels.
- `spec.tolerations[]` — must tolerate taints.
- `spec.topologySpreadConstraints[]` — best-effort balancing by `topologyKey`.
- `spec.affinity` and `spec.priorityClassName` — stored/exported; affinity is a soft hint for the scheduler today.
- `spec.storage[]` — enables storage pinning to a single node.

### K8s Export

`affinity`, `tolerations`, `topologySpreadConstraints`, `priorityClassName`, and `nodeSelector` are passed through to the Deployment/StatefulSet template. Use `ae k8s-check --policy strict` for validation and `ae k8s-report` to score parity.
