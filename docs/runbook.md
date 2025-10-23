# Operations Runbook

## Deploying a Revision
1. Update the manifest under `specs/` and apply with `python -m ae.cli apply -f <path>`.
2. Watch progress with `python -m ae.cli status <app> --events --history 5`; ready state should show `rev=<n>(ready)` and recent `ApplyCompleted` events.
3. If rollout regresses, execute `python -m ae.cli rollback <app>` to revert to the previous revision or specify `--to <rev>` for an explicit target.

## Secrets and Credentials
- Keep sealed secrets in `specs/<name>-secret.sops.yaml`. Verify decryption locally via `sops --decrypt` before deployment.
- Set `AE_ALLOW_PLAINTEXT_SECRETS=1` only for local smoke tests; production environments must provide an `AE_SOPS_BIN` capable of decrypting.
- Registry credentials live in `~/.config/ae/registries.yaml`. Use `python -m ae.cli registry list` to confirm tokens before a rollout.

## Observability
- Summarize fleet health with `python -m ae.cli metrics`. Use `--json` for dashboards.
- Inspect recent reconciliation events with `python -m ae.cli events <app>`; copy critical findings into an incident document.
- Raw SQLite artifacts reside in `state/controller.db`; back them up along with Caddy config and specs for disaster recovery.

## Troubleshooting
- If Caddy reloads fail, check `/tmp/*.caddy` renderings and rerun `python -m ae.cli apply` after corrections.
- Use `pytest tests/integration/test_reconcile_flow.py -q` to ensure the reconciliation pipeline remains healthy after major changes.
