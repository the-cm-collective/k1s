# Fabric Advisory Trace Review

Purpose
- Give operators a stable procedure for reviewing fabric advisory traces and deciding whether to accept or diverge from them.
- Keep the procedure portable across WorkerBee/dev, lab/HA, and production-mode deployments.
- Preserve the core authority rule: `k1s` controller state and fabric phase assurance remain authoritative. Hyperon/DAS evidence is optional, experimental advisory input.

Use this guide when the Hive dashboard shows a pending fabric advisory trace or a cognitive signal linked to an advisory trace. Use [Fabric Phase Assurance](fabric-phase-assurance.html) for the gate contract and [Distributed Compute Fabric](distributed-compute-fabric.html) for roadmap context.

## Decision Semantics

Accept
- Use `Accept` when the advisory trace accurately reflects current `k1s` evidence, current fabric gate state, and the operator agrees it should be retained as accepted non-authoritative evidence.
- Accepting a trace records an operator review event. It does not open a fabric phase gate, mutate placement, or make Hyperon/DAS authoritative.

Diverge
- Use `Diverge` when the trace is stale, contradicted by current `k1s` state, missing key evidence, phase-mismatched, generated from an unhealthy model/DAS path, synthetic/test-only, or recommends action outside the current authority envelope.
- Diverging records that the operator disagreed with the trace. It does not delete the trace or change controller state by itself.

Pending
- Leave the trace pending when the operator cannot decide from the available context.
- There is no separate defer writeback state today. Pending is the correct state for "needs more context".

## Mode Matrix

| Mode | Review posture | What to verify before deciding |
| --- | --- | --- |
| WorkerBee/dev | Treat traces as development and validation evidence. Synthetic traces are expected. | Confirm whether the trace came from a test, dashboard validation, advisory-store import, or a live workload scenario. |
| Lab/HA | Treat traces as operator rehearsal and integration evidence. | Compare the trace to live controller state, fabric gate readiness, workload health, and retained validation artifacts. |
| Production mode | Treat traces as advisory audit records only. | Confirm current controller authority, evidence freshness, approved operating policy, and whether the trace came from an enabled Hyperon/DAS integration. |

The review rules are intentionally the same in every mode. Only the evidence threshold changes.

## Dashboard Procedure

- Open the Hive dashboard and go to the Fabric Advisory area.
- Locate the pending trace or trace-linked cognitive signal and open Review.
- Confirm the `k1s authoritative` indicator before reading the recommendation.
- Inspect the request JSON and advisory response.
- Inspect trace metadata, replay/evaluation status, and divergence reason if present.
- Inspect Hyperon/DAS tabs only when evidence is attached. If no evidence is attached, treat the review as a `k1s` advisory-only review.
- Compare the trace to the current F3/fabric gate state from phase assurance.
- Submit `Accept` or `Diverge` with a short operator note.
- Refresh and confirm the pending trace count and linked cognitive signal review status changed as expected.

## Decision Criteria

Accept when all of these are true:
- The trace matches current controller state and current phase-assurance output.
- Evidence references are present, current enough for the operating mode, and relevant to the claimed condition.
- Any Hyperon/DAS evidence is clearly advisory and does not contradict `k1s`.
- The recommendation stays within the current review-only authority envelope.

Diverge when any of these are true:
- The trace was generated from stale phase assurance or superseded workload state.
- The trace claims a blocked or ready gate that current `k1s` state does not support.
- Required evidence is missing, malformed, synthetic-only, or attached to the wrong phase.
- Model, adapter, DAS, or replay health failed in a way that undermines the trace.
- The advisory suggests automation, placement, rollback, or gate movement that is not currently authorized.

Leave pending when:
- The operator cannot confirm evidence freshness.
- The trace cannot be tied to the current workload or phase gate.
- The right decision requires an incident owner, fabric owner, or release owner to supply more context.

## Operator Note Examples

Accepted trace:

```text
Accepted. Trace matches current k1s F3 gate state and evidence refs are current. Hyperon/DAS input is advisory only.
```

Stale or superseded trace:

```text
Diverged. Trace was generated from stale phase-assurance output; current k1s state no longer supports the blocked condition.
```

Missing evidence:

```text
Diverged. Recommendation cites missing or incomplete evidence; no current typed facts/locality evidence supports the claim.
```

Unhealthy advisory path:

```text
Diverged. Model/DAS health was not clean for this trace, so the advisory cannot be accepted as operator-reviewed evidence.
```

Synthetic or test trace:

```text
Diverged. Trace was produced by a test/import path and is not production workload evidence.
```

Needs more context:

```text
Left pending. Evidence freshness and workload ownership are not clear enough for accept/diverge review.
```

## Updating This Guide

- Keep authority language stable: `k1s` remains authoritative until the fabric authority model changes through an explicit design and ops update.
- Add new evidence sources by extending the mode matrix and decision criteria, not by weakening accept/diverge semantics.
- If a new review state is added, update the Decision Semantics and Dashboard Procedure sections together.
- If Hyperon/DAS moves beyond advisory behavior, update this page, [Fabric Phase Assurance](fabric-phase-assurance.html), and the fabric control-plane design before enabling production use.
