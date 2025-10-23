# Repository Guidelines

## Project Structure & Module Organization
- `src/ae/__main__.py` boots the controller daemon; keep it thin and pass work to packages.
- `src/ae/controller/`, `src/ae/runtime/`, and `src/ae/ingress/` implement reconcile logic, container adapters, and ingress writers with explicit interfaces.
- `src/ae/observability/` exposes logging, metrics, and event sinks; treat it as a leaf module.
- `specs/` tracks declarative manifests and sealed secrets; keep runnable samples in `specs/examples/`.
- `state/` holds the SQLite DB and other runtime artifacts (git-ignored) while `docs/` hosts references such as `FEAT.md`, ADRs, and diagrams.

## Build, Test, and Development Commands
- `python -m pip install -e .[dev]` installs the package with console scripts and tooling.
- `python -m ae.controller --loop` runs the reconcile loop against local Docker and watches `specs/`.
- `python -m ae.cli apply -f specs/examples/echo.yaml` applies a manifest for fast smoke tests.
- `pytest` runs unit suites; add `--maxfail=1 --disable-warnings` before opening PRs.
- `docker compose -f ops/dev/docker-compose.yaml up` starts Caddy and Prometheus fixtures; shut down with `docker compose ... down`.

## Coding Style & Naming Conventions
- Run `ruff format` and `ruff check`; both are wired into pre-commit. YAML/JSON files use two-space indentation.
- Type annotate new code and gate merges on `mypy src/ae`.
- Follow snake_case for modules and functions and lowercase package names; public APIs use verbs like `apply_spec`.
- Name manifests `<app>-deployment.yaml`, secrets `<app>-secret.sops.yaml`, and CLI subcommands with dashes (`ae rollout`).

## Testing Guidelines
- Keep table-driven tests for reconciliation and adapters in `tests/unit/`; share fixtures via `tests/unit/testdata/`.
- Run Docker-backed integration suites from `tests/integration/` with `pytest tests/integration/ --docker`.
- Maintain ≥80% coverage in `src/ae/controller/` and `src/ae/runtime/`.
- Exercise readiness/liveness transitions and rollback paths with parametrized tests.

## Commit & Pull Request Guidelines
- Use Conventional Commits (`feat:`, `fix:`, `chore:`) with ≤72-character summaries to drive changelog generation.
- PR descriptions must link issues, outline rollout impact, and list manual or automated checks; attach logs or screenshots for ingress tweaks.
- Rebase onto `main` before review, resolve conflicts locally, and rerun `pytest` plus `ruff check`.
- Request reviewers aligned with touched areas (`controller`, `ingress`, `observability`) and respond within one business day.

## Security & Configuration Tips
- Commit only sealed secrets (`*.sops.yaml`) and verify `sops --decrypt specs/<name>-secret.sops.yaml` before deployment.
- Store age keys under `~/.config/ae/keys.txt` (gitignored) and document required tool versions inside `scripts/bootstrap.sh`.
- Use `python -m ae.cli status --verbose` to audit Docker access and record TLS host mappings in `docs/ingress.md`.
- Configure registry credentials in `~/.config/ae/registries.yaml` (username/password) and list them via `ae registry list`; prefer short-lived tokens.
- Set `AE_ALLOW_PLAINTEXT_SECRETS=1` only for local development to bypass SOPS; ensure CI leaves it unset.
