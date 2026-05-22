# ae.apishim

- Source folder: `src/ae/apishim`
- Last reviewed: 2026-05-13

## System Summary
Kubernetes-compatible API shim, storage adapters, HA authority bridge, and kubectl/Helm compatibility surface.

## Subsystems
- Kubernetes API-compatible HTTP server and discovery surface.
- Object-store implementations for local and HA authority-backed modes.
- Kubernetes workload conversion into k1s app manifests.
- Exec, port-forward, watch, RBAC, SSA/patch, and kubeconfig/env bootstrap helpers.

## Package Initializer
Kubernetes API shim for k1s. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| __main__.py | [docs/main.md](docs/main.md) | CLI entry point for the Kubernetes API shim (serve, kubeconfig, migrate). | _touch_stream_log, cmd_serve, cmd_kubeconfig, cmd_migrate, main |
| adapter.py | [docs/adapter.md](docs/adapter.md) | Shim adapter that reconciles Kubernetes objects into k1s runtime state. | AdapterWorker |
| env.py | [docs/env.md](docs/env.md) | Packaged API shim environment and TLS helper. | ensure_local_apishim_env, _read_env_file, _read_value, _resolve_secret, _tls_material_missing |
| ha_store.py | [docs/ha-store.md](docs/ha-store.md) | HA-mode apishim store routing onto shared controller authority. | AuthorityMutationError, CrdAuthorityCatalog, WorkloadAuthorityStore, GenericAuthorityStore, MultiplexApishimStore |
| server.py | [docs/server.md](docs/server.md) | HTTP server implementing a Kubernetes-compatible API for the shim. | Principal, ControllerPodRecord, PodTarget, ShimHandler, ShimServer |
| store.py | [docs/store.md](docs/store.md) | SQLite/Postgres-backed object store with watch support for the API shim. | K8sObject, ObjectStore |

## Environment And Operational Touchpoints
`AE_AGENT_URL`, `AE_APISHIM_ADAPTER`, `AE_APISHIM_AGENT_URL`, `AE_APISHIM_ALLOW_ANON`, `AE_APISHIM_APP_ADMISSION`, `AE_APISHIM_CRI_PORTFORWARD`, `AE_APISHIM_CRI_PORTFORWARD_FORCE`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_APISHIM_ENABLE`, `AE_APISHIM_EXEC_TOKEN`, `AE_APISHIM_HA_CRD_REFRESH_SEC`, `AE_APISHIM_HA_WATCH_POLL_SEC`, `AE_APISHIM_MINT_TOKEN`, `AE_APISHIM_NODEPORT_MAX`, `AE_APISHIM_NODEPORT_MIN`, `AE_APISHIM_PATCH_DEBUG`, `AE_APISHIM_PF_DEBUG`, `AE_APISHIM_POD_STATE_CHECK`, `AE_APISHIM_POD_WATCH_CHECK`, `AE_APISHIM_POD_WATCH_TTL_SECONDS`, `AE_APISHIM_PORTFORWARD_TOKEN`, `AE_APISHIM_PORT_STATE`, `AE_APISHIM_PVC_REQUEUE_SECONDS`, `AE_APISHIM_PVC_RESCAN_SECONDS`, `AE_APISHIM_RBAC`, `AE_APISHIM_RBAC_EVAL`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_RUNTIME`, `AE_APISHIM_SA_TOKEN_TTL`, `AE_APISHIM_SESSION_SECRET`, `AE_APISHIM_SESSION_TTL`, `AE_APISHIM_SESSION_TTL_MAX`, `AE_APISHIM_SOT`, `AE_APISHIM_SPDY_DEBUG`, `AE_APISHIM_SPDY_LOG`, `AE_APISHIM_STREAM_IDLE_SECONDS`, `AE_APISHIM_STREAM_MAX_BYTES`, `AE_APISHIM_STREAM_MAX_SECONDS`, `AE_APISHIM_TLS_CERT`, `AE_APISHIM_TLS_CLIENT_CA`, `AE_APISHIM_TLS_KEY`, `AE_APISHIM_TOKEN`, `AE_APISHIM_TOMBSTONE_TTL`, `AE_APISHIM_WATCH_OUTBOX_BATCH`, `AE_APISHIM_WATCH_OUTBOX_CLEANUP`, `AE_APISHIM_WATCH_OUTBOX_POLL`, `AE_APISHIM_WATCH_OUTBOX_TTL`, `AE_APISHIM_WATCH_QUEUE_SIZE`, `AE_CRI_ENDPOINT`, `AE_HA_MODE`, `AE_HPA_COOLDOWN_SECONDS`, `AE_NODE_ADVERTISE_IP`, `AE_RUNTIME_BACKEND`, `AE_STATE_DB`, `AE_STATE_DSN`, `AE_STUB_BACKEND_HOST`, `AE_STUB_BACKEND_PORT`, `CRICTL_BIN`

## Cross-Package Dependencies
`.adapter`, `.ha_store`, `.server`, `.store`, `ae`, `ae.apishim.store`, `ae.controller.health`, `ae.controller.reconciler`, `ae.controller.spec`, `ae.controller.state`, `ae.k8s`, `ae.k8s.exporter`, `ae.observability.logging`, `ae.resources`, `ae.runtime`, `ae.storage.controller`

## Maintenance Notes
Detailed markers live in the per-module docs; direct module counts:
- `adapter.py`: 1 marker(s)
- `ha_store.py`: 5 marker(s)
- `server.py`: 2 marker(s)

## Related Tests
- `tests/e2e/ha_closeout.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/test_apishim_env.py`
- `tests/unit/test_apishim_endpoint_resolution.py`
- `tests/unit/test_apishim_ha_crd_authority.py`
- `tests/unit/test_apishim_ha_mode.py`
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_apishim_ha_store.py`
- `tests/unit/test_apishim_ha_workload_authority.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_apishim_patch.py`
- `tests/unit/test_apishim_portforward.py`
- `tests/unit/test_apishim_rbac.py`
- `tests/unit/test_apishim_remote_pods.py`
- `tests/unit/test_apishim_runtime_factory.py`
- `tests/unit/test_apishim_scopes.py`
- `tests/unit/test_apishim_snapshot.py`
- `tests/unit/test_apishim_statefulset_claims.py`
- `tests/unit/test_apishim_storage.py`
- `tests/unit/test_apishim_store_metrics.py`
- `tests/unit/test_apishim_store_watch.py`
- `tests/unit/test_apishim_version.py`
- `tests/unit/test_apishim_workloads.py`
