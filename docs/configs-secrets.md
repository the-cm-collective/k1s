## Configs and Secrets

This page explains how k1s projects values from configs and sealed secrets into
containers — as environment variables and (optionally) as files.

### Merge Order
- `configRefs` < `secretRefs` < `spec.env`
- Manifest `env` wins last and can override values coming from configs/secrets.

### Environment Projection

```
spec:
  configRefs:
    - name: app-config
      path: configs/app-config.yaml   # YAML or JSON mapping
      env:
        - { name: APP_MODE,  key: mode }
        - { name: APP_COLOR, key: color }
  secretRefs:
    - name: demo-secret
      path: specs/examples/demo-secret.sops.yaml
      env:
        - { name: API_TOKEN, key: token }
  env:
    - { name: APP_MODE, value: demo-override }  # overrides config/secret
```

For local demos without SOPS, set `AE_ALLOW_PLAINTEXT_SECRETS=1`.

### Sealing the sample secret (SOPS/age)

If you prefer to use a real sealed secret for the examples:

- Ensure you have an age recipient:
  - EITHER create an age identity (private key) at `~/.config/ae/keys.txt` using `age-keygen -o ~/.config/ae/keys.txt` (preferred),
  - OR set `AE_AGE_RECIPIENT=age1...` to your public recipient directly.
- Seal the demo secret:
  - `make secrets-seal-demo` (runs `scripts/seal_demo_secret.sh`)
- Verify decryption works locally:
  - `sops --decrypt specs/examples/demo-secret.sops.yaml | yq` (should show a `token` field)

Notes:
- The controller attempts to decrypt via `sops --decrypt`. In dev, you can bypass sealing by setting `AE_ALLOW_PLAINTEXT_SECRETS=1`.
- If decryption fails at runtime, the controller records an `AppEvent` with type `SecretError` and continues; use `ae events <app>` to inspect.

### File Projection

At reconcile time, k1s writes key/value files under:
- Host: `state/projections/<app>-rev<revision>/{config,secret}/...`
- Container (mounted RO): `/var/run/ae/config/<app>`

You can either project all keys by default, or select keys and filenames:

```
spec:
  configRefs:
    - name: app-config
      path: configs/app-config.yaml
      files:
        - { key: mode,  file: config/app_mode.txt }
        - { key: color, file: config/app_color.txt }
  secretRefs:
    - name: demo-secret
      path: specs/examples/demo-secret.sops.yaml
      files:
        - { key: token, file: secret/token }
```

Inside the container this produces, for example:
- `/var/run/ae/config/<app>/config/app_mode.txt`
- `/var/run/ae/config/<app>/secret/token`

### CLI Helpers
- `ae config validate -f configs/app-config.yaml` — list top-level keys.
- `ae secret validate -f specs/examples/demo-secret.sops.yaml` — list decrypted keys.

### Notes
- For production, use SOPS/age and keep plaintext disabled (do not set `AE_ALLOW_PLAINTEXT_SECRETS`).
- File projection is additive; you can still mount your own volumes.
