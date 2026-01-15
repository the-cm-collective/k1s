## Rollout Policy

Apps can specify a rollout policy to influence how replicas are created and how
old revisions are removed during an update.

### Fields

```
spec:
  rollout:
    strategy: ordered | parallel | canary   # default: parallel
    maxSurge: 1                             # keep old replicas temporarily (default: 1)
    maxUnavailable: 0                       # allow up to N replicas unavailable (default: 0)
    pause: true|false                       # pause rollout without changing runtime
    # canary options
    weight: 1                               # bias first upstream Nx in ingress (canary)
    auto: { start: 1, step: 2, intervalSeconds: 60, max: 10 }  # controller‑tracked ramp
```

### Semantics

- strategy=ordered:
  - Creates at most one new replica per reconcile until desired is met.
  - Leaves old revision replicas running during rollout (surge).
  - Removes old replicas when readiness satisfies desired - maxUnavailable.

- strategy=parallel:
  - Creates all missing new replicas in one reconcile.
  - Leaves old running during rollout (surge), then removes when readiness target is met.

- strategy=canary:
  - Biases ingress routing toward the first upstream. With `weight: N`, the first upstream is duplicated N times in the Caddy config.
  - With an `auto` block, the controller persists the weight in SQLite and increases it on schedule until `max`.

- maxSurge (current behavior):
  - Old revision containers are kept until new replicas are ready enough per maxUnavailable.

- maxUnavailable:
  - Old replicas are removed only when `ready_replicas >= desired - maxUnavailable`.

### Routing Bias

Ingress uses a “prefer first” policy by default. The controller writes the new revision
endpoints first, so traffic prefers the new revision as soon as it becomes ready; old
replicas are removed once readiness satisfies the policy. For canary rollouts, use
`rollout.weight` (and optional `rollout.auto`) to bias and ramp traffic.

### Demo

Apply a two-step ordered rollout for `echo`:

```
./scripts/init_demo.sh --demo-rollout -y -d
```

This applies `specs/examples/echo.yaml` then `specs/examples/echo-rollout.yaml`
with rollout `{ strategy: ordered, maxSurge: 1, maxUnavailable: 0 }`.

Pause/resume:

```
ae rollout pause echo
ae rollout resume echo
```
