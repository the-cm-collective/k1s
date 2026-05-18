# Provider Iptables

- Source: `network/provider_iptables.py`
- Last reviewed: 2026-05-13
- Size: 270 lines

## Purpose
Iptables-based Service VIP provider (single-node, CRI-friendly).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| IptablesProvider | 20 | Service VIP provider using iptables NAT rules (kube-proxy style). | public methods: ensure_network, ensure_service, update_service_endpoints, remove_service |

## Runtime And Data Flow
- Internal dependencies: `.provider`, `ae.controller.state`
- External libraries: `shutil`
- Environment inputs: `AE_IPTABLES_BIN`
- Side-effect surfaces: subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
