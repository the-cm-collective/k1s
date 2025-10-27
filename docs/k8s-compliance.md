# Kubernetes Compliance

This page summarizes our current Kubernetes spec compliance for exported manifests.

How it works
- We export K8s YAML from representative App manifests using `ae cli export-k8s` (preset: web-hardened).
- We run offline structural validation, optional `kubeconform -strict` schema checks, optional `kubectl apply --dry-run=server`, and our `k8s-check --policy strict`.
- A weighted score is computed per sample; the overall score is the average across samples.

Update the status
- Generate a fresh report and write it where the docs server picks it up:
  - `python -m ae.cli k8s-report --run-dry-run -o docs/site/k8s_status.json`
- Rebuild docs to embed the status in this page:
  - `python docs/build_docs.py`

The compliance status and per-sample details render below when a report is present.

