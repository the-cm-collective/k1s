# Inference Cell

- Source: `controller/inference_cell.py`
- Last reviewed: 2026-05-13
- Size: 1909 lines

## Purpose
InferenceCell reconcile lane (experimental).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| CellPhase | 48 | No class docstring. |  |
| StagePlacement | 62 | public methods: to_dict, from_dict | public methods: to_dict, from_dict |
| EdgeRequirement | 87 | public methods: to_dict | public methods: to_dict |
| AdmissionReport | 105 | public methods: to_dict | public methods: to_dict |
| LeaseBundle | 129 | No class docstring. |  |
| FabricSessionInfo | 138 | public methods: to_dict | public methods: to_dict |
| StagePlanner | 182 | Deterministic stage placement helper. | public methods: plan, gpu_slots |
| BoundaryBudgetAdmission | 241 | Boundary-based admission evaluator for cross-site PP. | public methods: evaluate |
| FabricBroker | 377 | public methods: create_session, teardown_session | public methods: create_session, teardown_session |
| FabricAgentClient | 392 | public methods: ensure_session, teardown_session | public methods: ensure_session, teardown_session |
| NoopFabricAgentClient | 398 | Plan-time fabric agent that reports success. | public methods: ensure_session, teardown_session |
| HttpFabricAgentClient | 409 | Node-agent-backed fabric session client. | public methods: ensure_session, teardown_session |
| LocalFabricBroker | 482 | State-store-backed fabric broker stub for v0 control-plane bring-up. | public methods: create_session, teardown_session |
| InferenceLeaseManager | 540 | Lease operations for GPU/port/node resources. | public methods: reserve, release |
| InferenceCellController | 661 | Experimental reconcile loop for inference cells. | public methods: reconcile_manifest, delete_cell |
| InferenceCellSetController | 1815 | Replica-set style reconcile for inference cells. | public methods: reconcile_manifest, scale |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _now | 159 | function | Internal helper. |
| _truthy_env | 163 | function | Internal helper. |
| _site_order | 167 | function | Internal helper. |
| _members_by_site | 175 | function | Internal helper. |
| _build_allowed_rules | 623 | function | Internal helper. |
| _make_condition | 651 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae._utc`, `ae.accelerators`, `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.runtime`
- External libraries: `requests`
- Environment inputs: `AE_AGENT_API_TOKEN`, `AE_AGENT_TOKEN`, `AE_INFERENCE_AGENT_TIMEOUT`, `AE_INFERENCE_AGENT_TOKEN`, `AE_INFERENCE_API_HEALTH_TIMEOUT`, `AE_INFERENCE_DEBUG_HOLD_ON_FAILURE`, `AE_INFERENCE_RUNTIME_CLASS`
- Side-effect surfaces: network/API.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 16: `except ImportError:  # pragma: no cover - Python < 3.11 compatibility`
- Line 18: `"""Backport-compatible StrEnum shim."""`
- Line 630: `# Reserve mp rendezvous even for Ray when fallback is enabled.`
- Line 1491: `worker_message = "runtime applied (fallback=mp)"`

## Related Tests And Docs
- `tests/unit/test_etcd_inference_state.py`
- `tests/unit/test_inference_cell_controller.py`
- `tests/unit/test_remote_runtime_fencing.py`
