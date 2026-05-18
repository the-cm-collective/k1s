# Manager

- Source: `config/manager.py`
- Last reviewed: 2026-05-13
- Size: 43 lines

## Purpose
Config management helpers (YAML/JSON to environment variables).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ConfigManager | 14 | Loads config files and projects selected keys into environment variables. | public methods: load_env |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`
- External libraries: `yaml`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_config_manager.py`
- `tests/unit/test_rollout_hooks.py`
