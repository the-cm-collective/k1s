## Scheduling & Placement (Planner)

Use the `plan` command to dry-run an apply and check for placement conflicts.

### Dry Run

```
ae plan -f specs/examples/echo.yaml
```

Output includes replica count, rollout strategy, and host port conflict checks for `spec.service.port` (single-replica apps) using the runtime’s published ports.
Use `--verbose` for a replica placement plan (replica IDs, storage mounts, and network hints).

### Spec Fields (K8s export)

- `spec.affinity`: passed through to Deployment template (`spec.template.spec.affinity`).
- `spec.tolerations[]`: passed through to `spec.template.spec.tolerations`.
- `spec.topologySpreadConstraints[]`: passed through to `spec.template.spec.topologySpreadConstraints`.

Notes
- These fields are exported as-is to K8s; validation is minimal to keep the engine lean.
- For multi-replica apps, `k8s-check --policy strict` recommends anti-affinity or topology spread.
