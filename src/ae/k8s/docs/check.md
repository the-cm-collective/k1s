# Check

- Source: `k8s/check.py`
- Last reviewed: 2026-05-13
- Size: 370 lines

## Purpose
K8s portability checker for AppManifest.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| Issue | 17 | No class docstring. |  |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| k8s_portability_issues | 23 | function | Entrypoint/helper without docstring. |
| apply_policy | 237 | function | Transform issues based on a named policy (baseline\|strict). |
| infer_hpa_issues | 258 | function | Validate HPA assumptions against the manifest's resources. |
| _valid_quantity | 304 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/integration/_profile_smoke.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_cri_smoke.py`
- `tests/integration/test_docs_export_and_links.py`
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/integration/test_storage.py`
- `tests/integration/test_storage_purge.py`
- `tests/integration/test_strict_cri_profile_smoke.py`
- `tests/unit/test_audit_cp_metrics.py`
- `tests/unit/test_audit_runtime_attribution.py`
- `tests/unit/test_bench_rollout_policy_helper.py`
- `tests/unit/test_bench_runtime_class_helper.py`
- `tests/unit/test_check_idle_snapshot.py`
- `tests/unit/test_containerd_runtime.py`
