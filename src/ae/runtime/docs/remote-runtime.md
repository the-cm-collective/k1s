# Remote Runtime

- Source: `runtime/remote_runtime.py`
- Last reviewed: 2026-05-13
- Size: 513 lines

## Purpose
Remote runtime shim that delegates RuntimeAdapter calls to an HTTP agent.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| RemoteRuntime | 33 | RuntimeAdapter that forwards calls to an ae.node agent over HTTP. | public methods: ensure_app, remove_app, remove_old_revisions, remove_replicas, list_containers_info, list_workload_metrics, read_logs, exec, exec_attach, exec_resize ... |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _runtime_result_from_json | 470 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `.base`, `ae.controller.spec`, `ae.ha.fencing`
- External libraries: `requests`
- Environment inputs: `AE_AGENT_CA_FILE`, `AE_AGENT_CERT_FILE`, `AE_AGENT_KEY_FILE`, `AE_REMOTE_RUNTIME_ENSURE_TIMEOUT`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_agent_pvc_pending.py`
- `tests/integration/test_agent_streaming_proxy.py`
- `tests/unit/test_apishim_remote_pods.py`
- `tests/unit/test_remote_runtime_fencing.py`
