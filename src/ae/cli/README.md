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
`ae`, `ae._utc`, `ae.accelerators`, `ae.config.manager`, `ae.controller.health`, `ae.controller.inference_cell`, `ae.controller.reconciler`, `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.ingress`, `ae.ingress.tls_sync`, `ae.k8s`, `ae.k8s.check`, `ae.k8s.exporter`, `ae.k8s.presets`, `ae.k8s.validate`, `ae.observability`, `ae.observability.logging`, `ae.runtime`, `ae.runtime.registry`, `ae.secrets`, `ae.secrets.manager`, `ae.security`

## Maintenance Notes
- `__main__.py` line 221: `help="API shim base URL for SPDY exec (defaults to AE_APISHIM_SERVER when set)",`
- `__main__.py` line 224: `"--ws-fallback",`
- `__main__.py` line 239: `help="API shim base URL for SPDY exec (defaults to AE_APISHIM_SERVER when set)",`
- `__main__.py` line 242: `"--ws-fallback",`
- `__main__.py` line 253: `help="Forward a local TCP port to a pod via the API shim (WebSocket)",`
- `__main__.py` line 275: `help="API shim base URL for WebSocket port-forward (defaults to AE_APISHIM_SERVER when set)",`
- `__main__.py` line 429: `help="Deprecated alias for --pod (filter by pod name)",`
- `__main__.py` line 748: `help="Treat warnings as errors (deprecated; use --policy strict)",`
- `__main__.py` line 918: `auth_mint = auth_sub.add_parser("mint", help="Mint short-lived API shim session tokens")`
- `__main__.py` line 934: `help="Optional TTL in seconds (bounded by shim limits)",`
- `__main__.py` line 939: `help="API shim base URL (defaults to AE_APISHIM_SERVER)",`
- `__main__.py` line 2852: `raise RuntimeError("labs session fallback unavailable: AE_LABS_TOKEN is not set")`
- `__main__.py` line 4004: `"warning: no direct apishim stream token resolved; CLI will rely on AE_LABS_TOKEN session fallback on 401",`
- `__main__.py` line 4598: `cols, rows = shutil.get_terminal_size(fallback=(80, 24))`
- `__main__.py` line 4885: `cols, rows = shutil.get_terminal_size(fallback=(80, 24))`
- `__main__.py` line 5252: `f"fallback={'1' if fallback_used else '0'}",`
- `__main__.py` line 5338: `print(f"{kind} exec got 401; trying labs session token fallback...")`
- `__main__.py` line 5368: `f"{kind} exec failed ({exc}); trying {transport_order[idx + 1]} fallback..."`
- `__main__.py` line 5395: `print("warning: --stdin/--tty are only supported against the API shim (SPDY/WebSocket)")`
- `__main__.py` line 5424: `# Fallback: select a pod by name substring`
- `__main__.py` line 5480: `print("shell requires the API shim; set --apishim or AE_APISHIM_SERVER")`
- `__main__.py` line 5518: `print("port-forward requires the API shim; set --apishim or AE_APISHIM_SERVER")`
- `__main__.py` line 5601: `print("port-forward got 401; trying labs session token fallback...")`
- `__main__.py` line 6229: `# Local store fallback`

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
