# ae.controller

- Source folder: `src/ae/controller`
- Last reviewed: 2026-05-13

## System Summary
Core control plane: manifest loading, reconcile loop, state stores, scheduling, HA authority, and workload controllers.

## Subsystems
- Manifest/spec parsing and normalization.
- Reconcile orchestration and health-gated rollout state.
- Local SQLite and shared etcd authority state.
- Scheduling, node registry, service endpoints, storage authority, HPA/CronJob controllers, and inference-cell lifecycle.

## Package Initializer
Controller reconciliation logic package.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| __main__.py | [docs/main.md](docs/main.md) | Controller daemon entry point. | service_controller_factory, _local_node_id, _parse_labels, _register_local_node, _truthy_env |
| agent_api.py | [docs/agent-api.md](docs/agent-api.md) | Lightweight controller-side API for node agents (heartbeats, node info). | _json, _serialize_nodes, make_handler, _node_site, _node_is_hub |
| app_ingress.py | [docs/app-ingress.md](docs/app-ingress.md) | Derived EdgeIngressRoute sync for AppManifest ingress declarations. | sync_translated_app_ingress, build_translated_route, edge_ingress_is_translated, _truthy_env, _translate_ingress_mode |
| authority.py | [docs/authority.md](docs/authority.md) | Provides classes KvClient, LeaseClient, AuthorityConfig, LeaderInfo, AuthorityMember within Core control plane:... | KvClient, LeaseClient, AuthorityConfig, LeaderInfo, AuthorityMember |
| cronjob_authority.py | [docs/cronjob-authority.md](docs/cronjob-authority.md) | Leader-gated CronJob scheduling over shared HA authority state. | CronJobAuthorityControllerConfig, CronJobAuthorityController |
| etcd_lease_client.py | [docs/etcd-lease-client.md](docs/etcd-lease-client.md) | Provides classes LeaseKeepAliveResult, GrpcEtcdLeaseClient within Core control plane: manifest loading, reconcile... | LeaseKeepAliveResult, GrpcEtcdLeaseClient |
| etcd_state.py | [docs/etcd-state.md](docs/etcd-state.md) | Provides classes EtcdHttpClient, EtcdStateStore within Core control plane: manifest loading, reconcile loop, state... | EtcdHttpClient, EtcdStateStore |
| health.py | [docs/health.md](docs/health.md) | Health probe evaluation utilities. | ProbeOutcome, PodHealth, HealthReport, HealthManager |
| hpa_authority.py | [docs/hpa-authority.md](docs/hpa-authority.md) | Leader-gated HPA control loop over shared HA authority state. | WorkloadMetricsCollectorConfig, HPAAuthorityControllerConfig, WorkloadMetricsCollector, HPAAuthorityController |
| inference_cell.py | [docs/inference-cell.md](docs/inference-cell.md) | InferenceCell reconcile lane (experimental). | CellPhase, StagePlacement, EdgeRequirement, AdmissionReport, LeaseBundle |
| node_identity.py | [docs/node-identity.md](docs/node-identity.md) | Provides functions scoped_node_id within Core control plane: manifest loading, reconcile loop, state stores,... | scoped_node_id |
| reconciler.py | [docs/reconciler.md](docs/reconciler.md) | Reconcile loop coordinating manifests, runtime operations, and health. | ReconcileReport, _ObservedRuntimeReplica, Reconciler |
| scheduler.py | [docs/scheduler.md](docs/scheduler.md) | Pod placement planner for multi-node scheduling. | Placement, Scheduler |
| spec.py | [docs/spec.md](docs/spec.md) | Declarative specification models for the ae application engine. | ManifestError, Metadata, HTTPGetProbe, TCPSocketProbe, ExecProbe |
| state.py | [docs/state.md](docs/state.md) | State persistence helpers backed by SQLite (default) or Postgres (optional). | AppStatus, RegistryEntry, AuthorityObjectEntry, WorkloadMetricsSnapshot, RegistryConflictError |
| storage_authority.py | [docs/storage-authority.md](docs/storage-authority.md) | Leader-owned HA storage controller hosting for shared-authority storage resources. | StorageAuthorityRunner |
| work_watchdog.py | [docs/work-watchdog.md](docs/work-watchdog.md) | Work ledger watchdogs for rescheduling stuck dispatches. | WorkWatchdogConfig, WorkWatchdog |

## Environment And Operational Touchpoints
`AE_AGENT_API_CLIENT_CA`, `AE_AGENT_API_HOST`, `AE_AGENT_API_PORT`, `AE_AGENT_API_REQUIRE_CLIENT_CERT`, `AE_AGENT_API_TLS_CERT`, `AE_AGENT_API_TLS_KEY`, `AE_AGENT_API_TOKEN`, `AE_AGENT_PORT`, `AE_AGENT_TOKEN`, `AE_AGENT_TOKEN_EXPIRES`, `AE_AGENT_URL`, `AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_APISHIM_MIRROR`, `AE_APISHIM_NAMESPACE`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_SERVER`, `AE_APISHIM_SOT`, `AE_APISHIM_TLS_CA`, `AE_APISHIM_TOKEN`, `AE_APPLY_RECONCILE_BURST`, `AE_APPLY_RECONCILE_DELAY_MS`, `AE_CADDY_PREFER_HOST_PORT_UPSTREAMS`, `AE_CONTROLLER_EPOCH`, `AE_CONTROLPLANE_API_HOST`, `AE_CONTROLPLANE_DASH_HOST`, `AE_CONTROLPLANE_DOCS_HOST`, `AE_CONTROLPLANE_PUBLIC_ENABLE`, `AE_CORE_PROXY_PORT_MAX`, `AE_CORE_PROXY_PORT_MIN`, `AE_CRASHLOOP_TTL`, `AE_CRONJOB_AUTHORITY_INTERVAL_S`, `AE_DOCKER_BIN`, `AE_DOCKER_NETWORK_SUBNET`, `AE_DOCS_PORT`, `AE_EDGE_INGRESS_APP_SITE`, `AE_EDGE_INGRESS_MODE`, `AE_EDGE_INGRESS_TRANSLATE_MODE`, `AE_ENABLE_SERVICE_PROXY`, `AE_ETCD_MAINTENANCE_INTERVAL_SEC`, `AE_ETCD_MAINTENANCE_THRESHOLD_PCT`, `AE_ETCD_QUOTA_BACKEND_BYTES`, `AE_ETCD_RETRY_BACKOFF`, `AE_ETCD_RETRY_JITTER`, `AE_ETCD_RETRY_MAX`, `AE_GATEWAY_JS_ACK_WAIT`, `AE_GATEWAY_JS_MAX_ACK_PENDING`, `AE_GATEWAY_JS_MAX_DELIVER`, `AE_GATEWAY_JS_MAX_WAITING`, `AE_HPA_COOLDOWN_SECONDS`, `AE_HPA_METRICS_MAX_AGE_SECONDS`, `AE_HPA_POLL_INTERVAL_SECONDS`, `AE_INFERENCE_AGENT_TIMEOUT`, `AE_INFERENCE_AGENT_TOKEN`, `AE_INFERENCE_API_HEALTH_TIMEOUT`, `AE_INFERENCE_DEBUG_HOLD_ON_FAILURE`, `AE_INFERENCE_RUNTIME_CLASS`, `AE_IPTABLES_BIN`, `AE_JS_MONITOR_INTERVAL_S`, `AE_JS_STORAGE`, `AE_JS_STREAM_NAME`, `AE_JS_WORK_SUBJECT`, `AE_LABS_HELM_NAMESPACE`, `AE_LABS_HELM_SERVER`, `AE_LABS_HELM_TOKEN`, `AE_NETWORK_NAME`, `AE_NETWORK_SUBNET`, `AE_NODE_ID`, `AE_NODE_LABELS`, `AE_NODE_NAME`, `AE_NODE_NOTREADY_AFTER`, `AE_NODE_PROFILE`, `AE_OUTBOX_PUBLISH_BATCH`, `AE_OUTBOX_PUBLISH_INTERVAL_S`, `AE_OVERLAY_HUB_ENDPOINT`, `AE_OVERLAY_HUB_SITE`, `AE_OVERLAY_MANAGE_NETWORK`, `AE_OVERLAY_NET`, ...

## Cross-Package Dependencies
`.state`, `ae`, `ae.accelerators`, `ae.apishim.adapter`, `ae.apishim.ha_store`, `ae.apishim.store`, `ae.cli.__main__`, `ae.config.manager`, `ae.config.transport`, `ae.controller.agent_api`, `ae.controller.app_ingress`, `ae.controller.authority`, `ae.controller.cronjob_authority`, `ae.controller.etcd_lease_client`, `ae.controller.etcd_state`, `ae.controller.health`, `ae.controller.hpa_authority`, `ae.controller.reconciler`, `ae.controller.scheduler`, `ae.controller.spec`, `ae.controller.state`, `ae.controller.storage_authority`, `ae.controller.work_watchdog`, `ae.ha.dashboard`, `ae.ha.fencing`, `ae.ingress.edge_core_proxy`, `ae.ingress.service`, `ae.ingress.tls_sync`, `ae.k8s.convert`, `ae.network`, `ae.network.overlay_health`, `ae.network.pod_cidr`, `ae.observability.http_api`, `ae.observability.logging`, `ae.resources`, `ae.runtime`, `ae.runtime.containerd_runtime`, `ae.secrets`, `ae.secrets.manager`, `ae.security.ca`, `ae.security.tokens`, `ae.storage.config`, `ae.storage.controller`, `ae.transport.controller_ingress`, `ae.transport.jetstream_monitor`, `ae.transport.nats_client`, `ae.transport.outbox_publisher`, `ae.transport.route_bundle_publisher`, `ae.transport.telemetry_ingress`

## Maintenance Notes
Detailed markers live in the per-module docs; direct module counts:
- `__main__.py`: 3 marker(s)
- `agent_api.py`: 2 marker(s)
- `reconciler.py`: 2 marker(s)
- `state.py`: 2 marker(s)
- `storage_authority.py`: 2 marker(s)

## Related Tests
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/integration/test_agent_pvc_pending.py`
- `tests/integration/test_agent_service_proxy.py`
- `tests/integration/test_agent_streaming_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_cri_runtime_integration.py`
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/integration/test_etcd_state_adapter.py`
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
- `tests/unit/test_apishim_statefulset_claims.py`
- `tests/unit/test_apishim_workloads.py`
