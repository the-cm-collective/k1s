# Gitea Actions WIP: Runner-specific workflow failures

Date: 2026-01-26

## Overview
These workflows are currently disabled in Gitea Actions via a job-level guard:
`if: ${{ env.GITEA_ACTIONS != 'true' }}`. They still run in GitHub Actions and
locally (act / manual). The failures are environment-specific to the Gitea
runner and not logic regressions.

## 1) .github/workflows/apishim-spdy-matrix.yml (docker leg)

### Symptom
- `apishim` exits during startup.
- Stack traces show `SSLCertVerificationError` while connecting to
  `tcp://docker:2376` via the Docker SDK.

### Root cause hypothesis
- The Docker daemon in the Gitea runner is TLS-protected, but the client inside
  the job container cannot validate the daemon certificate.
- The CA in `/certs/client/ca.pem` does not match the daemon cert chain (or is
  not being used by the SDK), so Docker SDK initialization fails and apishim
  terminates.

### Short-term mitigation (current)
- Job is skipped in Gitea Actions.
- Validate locally using `scripts/dev/apishim_spdy_matrix_run.sh` with a working
  Docker daemon.

### Proposed fixes
- Ensure the Docker daemon presents a cert signed by the CA available to the
  job container and that `DOCKER_CERT_PATH` points to that CA.
- Alternatively expose a Unix socket (`/var/run/docker.sock`) to the job and
  disable TLS for local-only runners.
- Validate with `docker info` and a Python SDK smoke test before running apishim.

## 2) .github/workflows/kubectl-exec-smoke.yml

### Symptom
- `Wait for apishim` fails with the same `SSLCertVerificationError` to
  `tcp://docker:2376` during Docker SDK init.

### Root cause hypothesis
- Same TLS trust mismatch as the docker leg in `apishim-spdy-matrix.yml`.

### Short-term mitigation (current)
- Job is skipped in Gitea Actions.
- Validate locally using the workflow or by running apishim + kubectl exec tests
  against a Docker daemon with valid certs.

### Proposed fixes
- Same as (1); fix runner Docker TLS trust chain or provide a Unix socket.
- Add a preflight Docker SDK check in runner provisioning.

## 3) .github/workflows/helm-dryrun-openapi.yml (dryrun job)

### Symptom
- `helm template` produces zero output (`/tmp/chart.yaml` empty) even though the
  chart templates exist and are valid.

### Root cause hypothesis
- The helm binary on the runner does not execute correctly (PATH mismatch,
  corrupted download, or incompatible binary), yielding empty output despite
  a zero exit code.

### Short-term mitigation (current)
- Job is skipped in Gitea Actions.
- Run the dry-run locally or in GitHub Actions where helm output is known good.

### Proposed fixes
- Verify helm installation on the runner (arch match, executable bits, PATH).
- Add a health check step that verifies `helm version` and a non-empty template
  output before the dry-run.
- Consider installing helm via a pinned package or container image.

## Re-enable criteria
- Docker TLS trust is fixed in the runner and `docker info` succeeds without
  TLS errors from within job containers.
- Helm renders a minimal chart with non-empty output in the runner.
- Re-enable jobs by removing the `if: ${{ env.GITEA_ACTIONS != 'true' }}` guard.
