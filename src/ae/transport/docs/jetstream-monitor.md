# Jetstream Monitor

- Source: `transport/jetstream_monitor.py`
- Last reviewed: 2026-05-13
- Size: 119 lines

## Purpose
JetStream monitoring poller for Phase 6 operability signals.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| JetStreamMonitorConfig | 33 | No class docstring. |  |
| JetStreamMonitor | 40 | public methods: start, stop, run_once | public methods: start, stop, run_once |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _read | 20 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.observability.http_api`, `ae.transport.nats_client`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
