# ae.config

- Source folder: `src/ae/config`
- Last reviewed: 2026-05-13

## System Summary
Configuration reference loading, transport configuration parsing, and shared environment-derived settings.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| manager.py | [docs/manager.md](docs/manager.md) | Config management helpers (YAML/JSON to environment variables). | ConfigManager |
| transport.py | [docs/transport.md](docs/transport.md) | Transport feature flags and NATS/gateway configuration. | TransportConfig, GatewayJetStreamConfig |

## Cross-Package Dependencies
`ae.controller.spec`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in direct modules during static review.

## Related Tests
- `tests/unit/test_config_manager.py`
- `tests/unit/test_gateway_service_fencing.py`
- `tests/unit/test_rollout_hooks.py`
- `tests/unit/test_transport_authority.py`
- `tests/unit/test_transport_config.py`
