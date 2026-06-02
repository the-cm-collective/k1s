# Fabric Phase Assurance

Status: executable checkpoint contract for the fabric roadmap.

This contract converts the `F*` roadmap order into a small machine-readable
gate. It does not mark a roadmap phase complete. It records whether a phase has
the required evidence for the current integration claim and whether that phase
is allowed to proceed based on its dependencies.

The command is:

```sh
python scripts/dev/fabric_phase_assurance.py --evidence evidence.json --json
```

The output has API version `k1s.fabric.phase-assurance/v1` and includes one row
per phase:

- `status`: `present` when all required evidence keys for that phase are true,
  otherwise `missing`
- `gate.ready`: true only when the phase itself is `present` and all dependency
  phases are `present`
- `gate.blocked_by`: dependency phases that prevent the gate from opening
- `present` and `missing`: the specific evidence keys used for the assessment
- `evidence`: the normalized values used to decide those keys

The `F0n-nvidia-dev` subtrack is included so development-substrate evidence can
be recorded without satisfying `F0` or `D0`.

For `F1`, controller node records can be converted into evidence with
`ae.fabric.phase_assurance.f1_evidence_from_nodes(...)`. That helper preserves
typed node fact details while keeping `F0` as the readiness gate for `F1`.
Controller-owned F2-F5 records can also be converted with
`f2_evidence_from_store(...)`, `f3_evidence_from_store(...)`,
`f4_evidence_from_store(...)`, and `f5_evidence_from_store(...)`.

## Evidence Keys

`F0n-nvidia-dev`:

- `gpu_nodes_controller_visible`
- `single_node_cells_ready`
- `two_host_pp2_cell_ready`
- `restart_delete_teardown_repeatable`
- `standard_ethernet_evidence`
- `non_substitutive_for_d0`

`F0`:

- `inference_cell_ready`
- `fabric_sessions_controller_visible`
- `member_status_controller_visible`
- `rollback_signal_controller_visible`
- `vm_gpu_validation_artifacts`

`F1`:

- `typed_node_capabilities`
- `typed_accelerators`
- `typed_storage_media`
- `typed_link_topology`
- `typed_rnic_rdma`
- `identity_role_separation`
- `gpu_label_projection`

`F2`:

- `content_addressed_chunks`
- `residency_state`
- `controlled_push_pull`
- `integrity_epoch_semantics`

`F3`:

- `advisory_contract`
- `decision_traces`
- `divergence_logging`
- `replay_evaluation`
- `bounded_planning`
- `continuity_coherence_signals`

`F4`:

- `capability_negotiation`
- `transfer_leases`
- `landing_zone_safety`
- `roce_development_path`
- `standard_transport_fallback`

`F5`:

- `das_cell_bundles`
- `local_first_query_warming_promotion`
- `controlled_cross_site_replication`
- `cognitive_fabric_substrate`

## Dependency Gate

The main substrate dependency chain remains:

| Phase | Gate dependencies |
| --- | --- |
| `F0n-nvidia-dev` | none |
| `F0` | none |
| `F1` | `F0` |
| `F2` | `F1` |
| `F3` | `F1`, `F2` |
| `F4` | `F2`, `F3` |
| `F5` | `F2`, `F3` |

This means an experimental `F3` Hyperon advisory report can be stored and
evaluated while its gate remains blocked until typed facts and locality evidence
are present.

## Controller Evidence Surfaces

F4 adds read-only controller surfaces for accelerated-movement readiness:

- `/fabric/transfer-capabilities`
- `/fabric/transfer-leases`
- `/fabric/landing-zones`
- `/fabric/transport-attempts`

These records describe negotiated capability, bounded transfer leases, safe
landing zones, and standard-transport fallback. RoCE is represented as a
development-path capability; this phase does not require a live RoCE data path.

F5 adds read-only controller surfaces for DAS-cell readiness:

- `/fabric/das-cells`
- `/fabric/das-query-traces`
- `/fabric/das-replications`
- `/fabric/cognitive-signals`

These records describe per-site DAS bundles, local-first query warming and
promotion, controlled cross-site replication intent, and cognitive-substrate
continuity/coherence signals. WorkerBee lab evidence may populate compatible
records, but k1s remains the authoritative phase gate.

WorkerBee AI fabric lab runtime facts use source
`workerbee.ai-fabric.runtime-facts/v1`, namespace `runtime`, and this shared
relationship vocabulary:

- `owns_service`
- `depends_on`
- `serves_model`
- `requires_resource`
- `produced_artifact`
- `supports_advisory`

Those facts can support F3/F5 advisory evaluation and DAS-cell evidence, but
they do not change controller authority or open a phase gate by themselves.

WorkerBee DAS advisory decisions use
`workerbee.ai-fabric.advisory-decision/v1`. Required decision fields are
`subject`, `intent`, `recommended_action`, `confidence`, `evidence_refs`,
`risks`, `blocked_conditions`, and `authoritative`. These decisions remain
non-authoritative evidence records for k1s review and do not replace controller
phase gates.

The shared advisory risk vocabulary currently includes
`symbolic_blocked_condition`, `missing_symbolic_evidence`,
`relationship_context_sparse`, `dependency_context_incomplete`,
`validation_artifact_unhealthy`, `fabric_phase_gate_blocked`,
`missing_phase_evidence`, `phase_report_stale`, and
`lora_adapter_not_ready`. Phase and adapter risks describe review evidence from
WorkerBee DAS scenarios; k1s remains the authority for opening fabric gates.

WorkerBee advisor scenario evaluations use
`workerbee.ai-fabric.advisor-scenario-eval/v1`. Required top-level artifact
fields are `api_version`, `run_id`, `scenario_count`, `results`, and `ok`.
Required per-result fields are `id`, `kind`, `status`, `risks`,
`blocked_conditions`, `checks`, and `ok`. These artifacts are repeatable
validation evidence for advisory behavior; they do not import synthetic facts
into k1s state and do not open a fabric phase gate by themselves.
