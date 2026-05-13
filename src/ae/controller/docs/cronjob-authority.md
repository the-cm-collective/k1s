# Cronjob Authority

- Source: `controller/cronjob_authority.py`
- Last reviewed: 2026-05-13
- Size: 313 lines

## Purpose
Leader-gated CronJob scheduling over shared HA authority state.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| CronJobAuthorityControllerConfig | 29 | No class docstring. |  |
| CronJobAuthorityController | 33 | Schedules CronJob-backed Job manifests from shared authority state. | public methods: start, stop, run_once |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _job_manifest_from_cronjob | 172 | function | Internal helper. |
| _next_due_schedule | 221 | function | Internal helper. |
| _next_interval_schedule | 236 | function | Internal helper. |
| _next_cron_schedule | 261 | function | Internal helper. |
| _parse_status_time | 284 | function | Internal helper. |
| _iso_utc | 299 | function | Internal helper. |
| _scheduled_job_name | 303 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.apishim.ha_store`, `ae.apishim.store`, `ae.controller.spec`, `ae.controller.state`, `ae.k8s.convert`
- External libraries: `croniter`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_cronjob_authority_controller.py`
