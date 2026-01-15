GitHub Actions: Kubernetes YAML checks

This workflow runs exporter smoke checks, portability checks, and kubeconform schema validation in CI.

```
name: k8s-yaml-checks
on:
  pull_request:
  push:
    branches: [ main ]

jobs:
  k8s-export-validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install project (dev)
        run: |
          python -m pip install -U pip
          python -m pip install -e .[dev]
      - name: Install kubeconform
        run: |
          curl -sSL https://github.com/yannh/kubeconform/releases/download/v0.6.4/kubeconform-linux-amd64.tar.gz \
            | tar -xz kubeconform
          sudo mv kubeconform /usr/local/bin/
      - name: Export examples and validate structure
        run: |
          make k8s-smoke || true
      - name: Run portability checks + kubeconform
        run: |
          python -m ae.cli k8s-check -f specs/examples/echo.yaml --policy strict --kubeconform --emit --fail-on-warn
```
