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

The `F0n-nvidia-dev` subtrack is included so development-substrate evidence can
be recorded without satisfying `F0` or `D0`.

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

