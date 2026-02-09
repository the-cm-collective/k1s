#!/usr/bin/env bash
set -euo pipefail

# Bootstrap developer environment for the ae controller project.
# Installs Python tooling, Caddy, SQLite, and pre-commit hooks.
# Update required versions here whenever dependencies change.

python -m pip install --upgrade pip
pip install -e .[dev]
pre-commit install

cat <<'EOM'
Bootstrap complete.
- Python tooling installed with extras.
- Pre-commit hooks configured.
Remember to install Docker, Caddy, and SQLite via your OS package manager.
Optional: install Rosenpass (for WireGuard PSK rotation) if using Option C.
EOM
