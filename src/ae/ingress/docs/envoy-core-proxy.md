# Envoy Core Proxy

- Source: `ingress/envoy_core_proxy.py`
- Last reviewed: 2026-05-13
- Size: 418 lines

## Purpose
Envoy core ingress config renderer for edge core-proxy mode.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| CoreProxyRoute | 12 | No class docstring. |  |
| CoreProxyCluster | 34 | No class docstring. |  |
| DownstreamTlsCert | 46 | No class docstring. |  |
| EnvoyRenderConfig | 53 | No class docstring. |  |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| render_envoy_config | 64 | function | Entrypoint/helper without docstring. |
| write_envoy_config | 390 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- External libraries: `yaml`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_envoy_render_yaml.py`
