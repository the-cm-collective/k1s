# Docker Stub

- Source: `runtime/docker_stub.py`
- Last reviewed: 2026-05-13
- Size: 155 lines

## Purpose
Stub runtime adapter for local testing without Docker.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| StubRuntime | 14 | Stubbed runtime; returns ready replicas without touching Docker. | public methods: ensure_app, read_logs, remove_app, remove_old_revisions, remove_replicas, list_containers_info, list_workload_metrics |

## Runtime And Data Flow
- Internal dependencies: `.base`, `ae.controller.spec`
- Environment inputs: `AE_STUB_BACKEND_HOST`, `AE_STUB_BACKEND_PORT`, `AE_STUB_NAMESPACE`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_apishim_statefulset_claims.py`
- `tests/unit/test_projection.py`
- `tests/unit/test_reconciler_endpoints.py`
