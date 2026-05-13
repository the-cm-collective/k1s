# App Ingress

- Source: `controller/app_ingress.py`
- Last reviewed: 2026-05-13
- Size: 229 lines

## Purpose
Derived EdgeIngressRoute sync for AppManifest ingress declarations.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| sync_translated_app_ingress | 15 | function | Synchronize translated ingress routes from registered apps. |
| build_translated_route | 75 | function | Entrypoint/helper without docstring. |
| edge_ingress_is_translated | 130 | function | Entrypoint/helper without docstring. |
| _truthy_env | 137 | function | Internal helper. |
| _translate_ingress_mode | 142 | function | Internal helper. |
| _normalize_ingress_mode | 162 | function | Internal helper. |
| _manifest_prefers_core_local | 175 | function | Internal helper. |
| _translate_ingress_site | 185 | function | Internal helper. |
| _translate_ingress_tls | 199 | function | Internal helper. |
| _translate_ingress_port | 209 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`
- Environment inputs: `AE_EDGE_INGRESS_APP_SITE`, `AE_EDGE_INGRESS_MODE`, `AE_EDGE_INGRESS_TRANSLATE_MODE`, `AE_SITE_ID`, `AE_TRANSPORT_BACKEND`, `EDGE_INGRESS_MODE`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_app_ingress.py`
