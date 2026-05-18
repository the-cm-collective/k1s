# State

- Source: `controller/state.py`
- Last reviewed: 2026-05-13
- Size: 4126 lines

## Purpose
State persistence helpers backed by SQLite (default) or Postgres (optional).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| AppStatus | 39 | Latest reconcile snapshot for an application. |  |
| RegistryEntry | 63 | Registered desired-state manifest for reconciliation. |  |
| AuthorityObjectEntry | 76 | Shared-authority shim object persisted outside the legacy apishim DB. |  |
| WorkloadMetricsSnapshot | 93 | Aggregated workload metrics used by the HA HPA controller. |  |
| RegistryConflictError | 108 | Raised when a registry CAS write sees a stale resource version. | 1 internal method(s) |
| PodStatus | 122 | Status for a single pod in the state store. | public methods: replica_id, replica_id |
| ProbeHistoryEntry | 153 | Recorded probe evaluation for audit/history purposes. | public methods: replica_id, replica_id |
| AppEvent | 178 | Event emitted during reconciliation or runtime changes. |  |
| WorkQueueLease | 189 | Leased work item for lab-edge work.pull. |  |
| NodeLease | 201 | Lease record for a node (lab-edge semantics). |  |
| SiteIngressEndpoint | 216 | Ingress endpoint metadata for a site (core-proxy/core-to-edge-public). |  |
| SiteIngressListItem | 229 | No class docstring. |  |
| EdgeIngressRouteRecord | 238 | No class docstring. |  |
| EdgeIngressPolicyRecord | 251 | No class docstring. |  |
| WorkOutboxEntry | 261 | No class docstring. |  |
| WorkLedgerEntry | 274 | No class docstring. |  |
| ServiceRecord | 292 | Service-level metadata such as ClusterIP and exposed ports. |  |
| ServiceEndpoint | 301 | Endpoint backing a Service port. |  |
| ServiceListItem | 312 | Brief view used for IP allocation. |  |
| VolumeAttachment | 320 | Attachment record for a volume bound to a node. |  |
| NodeRecord | 331 | Registered node information. |  |
| NodeStatus | 350 | Latest heartbeat/status for a node. |  |
| RevisionInfo | 359 | Information about a stored application revision. |  |
| InferenceCellRecord | 370 | Stored InferenceCell desired+observed state. |  |
| InferenceCellEvent | 392 | Event emitted for inference cell reconciliation. |  |
| InferenceCellSetRecord | 402 | Stored InferenceCellSet template and rollout status. |  |
| FabricSessionRecord | 418 | Persisted per-cell fabric session metadata. |  |
| SQLiteStateStore | 432 | Minimal state store; sqlite by default, Postgres via AE_STATE_DSN or dsn=. | public methods: record_snapshot, get_status, list_status, list_pods, list_pod_nodes, set_pod_nodes, get_probe_history, prepare_revision, register_app, list_registered_apps ... |
| _PgCompatConnection | 4085 | Light wrapper to allow sqlite-style '?' placeholders on psycopg connections. | public methods: execute, executemany, commit, rollback, close |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _outbox_publish_subject | 4064 | function | Internal helper. |
| _outbox_publish_msg_id | 4068 | function | Internal helper. |
| state_store_from_env | 4072 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.accelerators`, `ae.controller.etcd_state`, `ae.controller.health`, `ae.controller.spec`, `ae.ha.fencing`, `ae.resources`, `ae.runtime`
- External libraries: `psycopg`
- Environment inputs: `AE_POD_NODE_TTL_SECONDS`, `AE_POD_STATUS_TTL_SECONDS`, `AE_STATE_BACKEND`, `AE_STATE_DB`, `AE_STATE_DSN`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
- Line 77: `"""Shared-authority shim object persisted outside the legacy apishim DB."""`
- Line 449: `# Drop legacy replica tables now that pod naming is canonical.`

## Related Tests And Docs
- `tests/integration/test_agent_service_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/integration/test_multinode_agent_flow.py`
- `tests/integration/test_overlay_vip.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_agent_api.py`
- `tests/unit/test_apishim_ha_crd_authority.py`
- `tests/unit/test_apishim_ha_mode.py`
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_apishim_ha_store.py`
- `tests/unit/test_apishim_ha_workload_authority.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_apishim_remote_pods.py`
