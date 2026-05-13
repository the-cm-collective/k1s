# Tls Sync

- Source: `ingress/tls_sync.py`
- Last reviewed: 2026-05-13
- Size: 76 lines

## Purpose
TLS secret sync helper.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| TlsSecretResolver | 23 | public methods: resolve | public methods: resolve |

## Runtime And Data Flow
- External libraries: `yaml`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_tls_sync.py`
