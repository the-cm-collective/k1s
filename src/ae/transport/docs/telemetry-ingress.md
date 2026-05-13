# Telemetry Ingress

- Source: `transport/telemetry_ingress.py`
- Last reviewed: 2026-05-13
- Size: 105 lines

## Purpose
NATS ingress for site telemetry (status/logs/caps).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| TelemetryIngress | 14 | public methods: start, close | public methods: start, close |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _safe_json | 87 | function | Internal helper. |
| _site_id_from_subject | 96 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.observability.http_api`, `ae.transport.nats_client`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
