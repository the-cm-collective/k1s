## Scheduling & Placement (Planner)

Use the `plan` command to dry-run an apply and check for placement conflicts.

### Dry Run

```
ae plan -f specs/examples/echo.yaml
```

Output includes replica count, rollout strategy, and a simple host port conflict
check for `spec.service.port` (single-replica apps). The planner attempts to bind
the host port to detect if it is already in use.

### Notes

- Current checks are limited to service port availability. Future versions will
  add cross-app conflict checks and affinity hints.
