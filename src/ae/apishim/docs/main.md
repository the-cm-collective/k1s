# Main Entrypoint

- Source: `apishim/__main__.py`
- Last reviewed: 2026-05-13
- Size: 142 lines

## Purpose
CLI entry point for the Kubernetes API shim (serve, kubeconfig, migrate).

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _touch_stream_log | 12 | function | Internal helper. |
| cmd_serve | 24 | function | Entrypoint/helper without docstring. |
| cmd_kubeconfig | 42 | function | Entrypoint/helper without docstring. |
| cmd_migrate | 71 | function | Entrypoint/helper without docstring. |
| main | 101 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `.server`, `.store`, `ae.observability.logging`
- Environment inputs: `AE_APISHIM_ENABLE`, `AE_APISHIM_SPDY_LOG`, `AE_APISHIM_TOKEN`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 1: `"""CLI entry point for the Kubernetes API shim (serve, kubeconfig, migrate)."""`
- Line 27: `raise SystemExit("AE_APISHIM_ENABLE=1 required to run the API shim")`
- Line 102: `p = argparse.ArgumentParser(prog="python -m ae.apishim", description="k1s Kubernetes API shim")`
- Line 105: `s = sub.add_parser("serve", help="Run the API shim server")`
- Line 121: `k = sub.add_parser("kubeconfig", help="Emit a kubeconfig pointing to the shim")`
- Line 125: `help="Shim server URL, e.g. https://127.0.0.1:8445 or http://...",`
- Line 132: `m = sub.add_parser("migrate", help="Migrate shim storage between sqlite path and Postgres DSN")`

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
