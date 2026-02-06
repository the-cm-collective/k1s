# NATS JWT/operator dev flow (Mode A)

This folder provides a dev/lab bootstrap for JWT/operator auth and a path to the
production ops flow (`nsc push`).

## Prereqs
- Install `nsc` (NATS CLI). Use the official installer helper:

```bash
ops/dev/install-nsc.sh
```

- Docker + docker compose.

## 1) Generate operator/account/users (dev bootstrap)

```bash
ops/dev/nsc-bootstrap.sh
```

Outputs (gitignored) are written to `.local/nats-jwt/`:
- `operator.jwt`
- `accounts/` (account JWTs)
- `creds/` (user `.creds`)
- `nats-hub.conf`, `nats-edge.conf`

## 2) Run JWT-enabled NATS + etcd

```bash
docker compose -f ops/dev/docker-compose.nats-etcd.jwt.yaml up -d
```

## 3) Use creds in k1s processes

Examples:

```bash
export AE_NATS_CREDS=.local/nats-jwt/creds/hub-controller.creds
```

Gateway/worker should point to `gateway.creds` / `worker.creds`.

## 4) Update accounts/users (production ops flow)

Typical cycle:
1. Add or edit user/limits via `nsc`.
2. Generate new `.creds`.
3. Push account JWT updates to the running NATS server.

Use the helper:

```bash
NATS_URL=nats://127.0.0.1:4222 \
SYS_CREDS=.local/nats-jwt/creds/sys.creds \
ops/dev/nsc-push.sh
```

### Examples

- Add a new user and push updates:
```bash
nsc add user --name gateway --account K1S --dir .local/nats-jwt/nsc
nsc generate creds --account K1S --name gateway --dir .local/nats-jwt/nsc > .local/nats-jwt/creds/gateway.creds
ops/dev/nsc-push.sh
```

- Revoke a user and push:
```bash
nsc revoke user --name gateway --account K1S --dir .local/nats-jwt/nsc
ops/dev/nsc-push.sh
```

## Notes
- The system account (`SYS`) is used for `nsc push`.
- For production, store the `.local/nats-jwt/nsc` directory securely (it contains signing keys).
