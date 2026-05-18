# Ca

- Source: `security/ca.py`
- Last reviewed: 2026-05-13
- Size: 233 lines

## Purpose
Lightweight CA helper for agent mTLS bootstrap using openssl.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _ensure_root | 31 | function | Internal helper. |
| ensure_ca | 35 | function | Entrypoint/helper without docstring. |
| issue_cert | 66 | function | Return (cert, key, ca) paths for the issued node cert. |
| _record_issue | 145 | function | Persist issued cert metadata for revocation/rotation bookkeeping. |
| record_used_token | 182 | function | Entrypoint/helper without docstring. |
| token_used | 195 | function | Entrypoint/helper without docstring. |
| revoke_serial | 206 | function | Entrypoint/helper without docstring. |
| is_revoked | 220 | function | Entrypoint/helper without docstring. |
| revoke_from_file | 231 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies:
- External libraries: `shutil`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/test_apishim_env.py`
- `tests/unit/test_cli_auth.py`
- `tests/unit/test_cli_exec.py`
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_ha_ops.py`
- `tests/unit/test_http_api_apishim_verify.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_netfs_validation_scripts.py`
- `tests/unit/test_nix_dev_env.py`
- `tests/unit/test_nixos_bridge_helper.py`
