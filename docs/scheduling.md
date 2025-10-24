## Scheduling & Placement (Planner)

Use the `plan` command to dry-run an apply and check for placement conflicts.

### Dry Run

```
ae plan -f specs/examples/echo.yaml
```

Output includes replica count, rollout strategy, and host port conflict checks for `spec.service.port` (single-replica apps) using the runtime’s published ports.
Use `--verbose` for a replica placement plan (replica IDs, storage mounts, and network hints).

### Notes

- Current checks are limited to service port availability. Future versions will
  add cross-app conflict checks and affinity hints.
