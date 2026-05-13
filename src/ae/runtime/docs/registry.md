# Registry

- Source: `runtime/registry.py`
- Last reviewed: 2026-05-13
- Size: 95 lines

## Purpose
Registry authentication helpers.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| RegistryAuthProvider | 18 | Loads registry credentials and logs into docker clients as needed. | public methods: ensure_login, list_registries |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _default_config_path | 11 | function | Internal helper. |

## Runtime And Data Flow
- External libraries: `yaml`
- Environment inputs: `AE_REGISTRY_CONFIG`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/_profile_smoke.py`
- `tests/integration/test_cri_runtime_integration.py`
- `tests/integration/test_cri_smoke.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_cri_bootstrap_scripts.py`
- `tests/unit/test_cri_stack.py`
- `tests/unit/test_dashboard_template_ha.py`
- `tests/unit/test_f0n_nvidia_validate_script.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_microk8s_stack_bundle.py`
- `tests/unit/test_nix_dev_env.py`
- `tests/unit/test_nixos_bridge_helper.py`
- `tests/unit/test_registry_kubesecret.py`
- `tests/unit/test_runtime_docker.py`
- `tests/unit/test_storage_provisioners.py`
