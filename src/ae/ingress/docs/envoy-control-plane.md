# Envoy Control Plane

- Source: `ingress/envoy_control_plane.py`
- Last reviewed: 2026-05-13
- Size: 510 lines

## Purpose
Control-plane Envoy renderer for docs/dashboard browser auth.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ControlPlaneEnvoyConfig | 13 | No class docstring. |  |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| build_control_plane_envoy_config_from_env | 35 | function | Entrypoint/helper without docstring. |
| render_control_plane_envoy_config | 70 | function | Entrypoint/helper without docstring. |
| render_control_plane_envoy_secrets | 347 | function | Entrypoint/helper without docstring. |
| write_control_plane_envoy_bundle | 368 | function | Entrypoint/helper without docstring. |
| _route | 382 | function | Internal helper. |
| _direct_response | 405 | function | Internal helper. |
| _validate_oauth_config | 412 | function | Internal helper. |
| _auth_authorization_endpoint | 429 | function | Internal helper. |
| _auth_token_endpoint_uri | 433 | function | Internal helper. |
| _auth_scopes_from_env | 443 | function | Internal helper. |
| _upstream_host | 451 | function | Internal helper. |
| _upstream_port | 458 | function | Internal helper. |
| _parse_env_upstream | 465 | function | Internal helper. |
| _read_secret_file | 479 | function | Internal helper. |
| _truthy_env | 494 | function | Internal helper. |
| os_env | 498 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- External libraries: `yaml`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_envoy_control_plane.py`
