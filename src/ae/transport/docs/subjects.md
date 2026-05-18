# Subjects

- Source: `transport/subjects.py`
- Last reviewed: 2026-05-13
- Size: 71 lines

## Purpose
Subject helpers for Mode A transport.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| local_work_subject | 6 | function | Entrypoint/helper without docstring. |
| local_result_subject | 10 | function | Entrypoint/helper without docstring. |
| local_work_progress_subject | 14 | function | Entrypoint/helper without docstring. |
| local_status_subject | 18 | function | Entrypoint/helper without docstring. |
| local_logs_subject | 22 | function | Entrypoint/helper without docstring. |
| local_caps_subject | 26 | function | Entrypoint/helper without docstring. |
| hub_lease_acquire_subject | 30 | function | Entrypoint/helper without docstring. |
| hub_lease_renew_subject | 34 | function | Entrypoint/helper without docstring. |
| hub_result_subject | 38 | function | Entrypoint/helper without docstring. |
| hub_status_subject | 42 | function | Entrypoint/helper without docstring. |
| hub_logs_subject | 46 | function | Entrypoint/helper without docstring. |
| hub_caps_subject | 50 | function | Entrypoint/helper without docstring. |
| hub_work_pull_subject | 54 | function | Entrypoint/helper without docstring. |
| hub_work_ack_subject | 58 | function | Entrypoint/helper without docstring. |
| hub_route_bundle_subject | 62 | function | Entrypoint/helper without docstring. |
| hub_route_ack_subject | 66 | function | Entrypoint/helper without docstring. |
| work_stream_subject | 70 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- No obvious external side-effect surface in static review.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_apishim_rbac.py`
- `tests/unit/test_route_bundle_sites.py`
- `tests/unit/test_transport_config.py`
