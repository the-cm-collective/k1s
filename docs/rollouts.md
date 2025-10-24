## Rollout Policy

Apps can specify a rollout policy to influence how replicas are created and how
old revisions are removed during an update.

### Fields

```
spec:
  rollout:
    strategy: ordered | parallel   # default: parallel
    maxSurge: 1                    # keep old replicas temporarily (default: 1)
    maxUnavailable: 0              # allow up to N replicas unavailable (default: 0)
```

### Semantics

- strategy=ordered:
  - Creates at most one new replica per reconcile until desired is met.
  - Leaves old revision replicas running during rollout (surge).
  - Removes old replicas when readiness satisfies desired - maxUnavailable.

- strategy=parallel:
  - Creates all missing new replicas in one reconcile.
  - Leaves old running during rollout (surge), then removes when readiness target is met.

- maxSurge (current behavior):
  - Old revision containers are kept until new replicas are ready enough per maxUnavailable.

- maxUnavailable:
  - Old replicas are removed only when `ready_replicas >= desired - maxUnavailable`.

### Routing Bias

Ingress uses a “first available upstream” policy by default. The controller writes
the new revision endpoints first, so traffic prefers the new revision as soon as it
becomes ready; old replicas are removed once readiness satisfies the policy.

Weighting (intra-revision bias): set `AE_ROLLOUT_FIRST_WEIGHT` (default `1`) to
duplicate the first upstream N times in the Caddy config, biasing selection toward it.
This is a simple approximation and currently applies within the active upstream set.

### Demo

Apply a two-step ordered rollout for `echo`:

```
./scripts/init_demo.sh --demo-rollout -y -d
```

This applies `specs/examples/echo.yaml` then `specs/examples/echo-rollout.yaml`
with rollout `{ strategy: ordered, maxSurge: 1, maxUnavailable: 0 }`.

Optional bias:

```
AE_ROLLOUT_FIRST_WEIGHT=3 ./scripts/init_demo.sh --demo-rollout -y
```
