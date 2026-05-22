# ae.cli

- Source folder: `src/ae/cli`
- Last reviewed: 2026-05-13

## System Summary
Primary `ae` command-line interface for apply/status/logs/exec/export/auth/profile-oriented operations.

## Package Initializer
Command-line interface utilities for ae.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| __main__.py | [docs/main.md](docs/main.md) | Command-line interface for the ae orchestrator. | CLIArgumentParser |
| rotate_certs.py | [docs/rotate-certs.md](docs/rotate-certs.md) | CLI helper to issue node certs and join tokens. | main |

## Environment And Operational Touchpoints
`AE_AGENT_JOIN_SECRET`, `AE_ALLOW_PLAINTEXT_SECRETS`, `AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_EXEC_TOKEN`, `AE_APISHIM_INSECURE`, `AE_APISHIM_MINT_TOKEN`, `AE_APISHIM_PORTFORWARD_TOKEN`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_SERVER`, `AE_APISHIM_SESSION_CACHE`, `AE_APISHIM_SESSION_SECRET`, `AE_APISHIM_TLS_CA`, `AE_APISHIM_TOKEN`, `AE_API_ADMIN_TOKEN`, `AE_API_READ_TOKEN`, `AE_API_SCALER_TOKEN`, `AE_API_SERVER`, `AE_CADDY_BIN`, `AE_CADDY_CONTAINER`, `AE_CADDY_FILE`, `AE_CADDY_RELOAD_TIMEOUT`, `AE_CADDY_SITES`, `AE_CLI_HTTP_TIMEOUT`, `AE_CLI_LABS_MINT_FALLBACK`, `AE_CLI_SHARED_GROUP`, `AE_CONTAINER_CLI`, `AE_DISABLE_INGRESS`, `AE_DOCKER_NETWORK`, `AE_ETCD_ENDPOINTS`, `AE_ETCD_PREFIX`, `AE_EXEC_TRANSPORT_REPORT`, `AE_EXEC_WS_FALLBACK`, `AE_HA_MODE`, `AE_LABS_TOKEN`, `AE_NAMESPACE`, `AE_NODE_ID`, `AE_NODE_NOTREADY_AFTER`, `AE_PODMAN_BIN`, `AE_PODMAN_NETWORK`, `AE_REGISTRY_CONFIG`, `AE_RUNTIME_BACKEND`, `AE_SPECS_DIR`, `AE_STATE_BACKEND`, `AE_STATE_DB`, `AE_TLS_DIR`, `APISHIM_ENV_FILE`, `APISHIM_PID_FILE`, `APISHIM_PORT`, `APISHIM_UPSTREAM`, `AWS_ACCOUNT_ID`, `AWS_DEFAULT_REGION`, `AWS_REGION`, `CONTROLLER_ENV_FILE`, `COSIGN_BIN`, `DEV_ENV_FILE`, `GHCR_TOKEN`, `GHCR_USERNAME`, `GH_TOKEN`, `KUBECONFIG`, `KUBECONFORM_BIN`, `KUBECTL_BIN`, `USER`, `XDG_CACHE_HOME`

## Cross-Package Dependencies
`ae`, `ae.accelerators`, `ae.config.manager`, `ae.controller.health`, `ae.controller.inference_cell`, `ae.controller.reconciler`, `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.ingress`, `ae.ingress.tls_sync`, `ae.k8s`, `ae.k8s.check`, `ae.k8s.exporter`, `ae.k8s.presets`, `ae.k8s.validate`, `ae.observability`, `ae.observability.logging`, `ae.runtime`, `ae.runtime.registry`, `ae.secrets`, `ae.secrets.manager`, `ae.security`

## Maintenance Notes
Detailed markers live in the per-module docs; direct module counts:
- `__main__.py`: 7 marker(s)

## Related Tests
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/integration/_profile_smoke.py`
- `tests/integration/test_storage.py`
- `tests/integration/test_storage_purge.py`
- `tests/unit/test_backup.py`
- `tests/unit/test_bench_script_contracts.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_cli_auth.py`
- `tests/unit/test_cli_exec.py`
- `tests/unit/test_cli_namespace.py`
- `tests/unit/test_cli_remote.py`
- `tests/unit/test_cli_split_export.py`
- `tests/unit/test_k8s_check_policy.py`
- `tests/unit/test_nightly_runtime_workflow.py`
- `tests/unit/test_plan_validation.py`
- `tests/unit/test_registry_kubesecret.py`
- `tests/unit/test_runtime_factory.py`
- `tests/unit/test_version.py`
