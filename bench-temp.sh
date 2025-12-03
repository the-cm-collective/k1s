# bash bench_rollout.sh | tee bench_rollout.log
log(){ echo "[$(date +%H:%M:%S)] $*"; }

log "k3d up"
AE_PODMAN_BIN=/nonexistentpodman scripts/bench/k3s_up.sh --name bench || exit 1
AE_PODMAN_BIN=/nonexistentpodman kubectl wait --for=condition=Ready node --all --timeout=180s || exit 1
AE_PODMAN_BIN=/nonexistentpodman kubectl -n default apply -f specs/examples/k3s-echo.yaml || exit 1
log "k3d rollout-2 snapshots"
AE_PODMAN_BIN=/nonexistentpodman bash scripts/bench/run_rollout_k3s.sh \
  --label-suite baseline --deploy echo --namespace default --replicas 2 --duration 30 --sudo || exit 1
AE_PODMAN_BIN=/nonexistentpodman scripts/bench/k3s_up.sh --name bench --down || true

# ensure controller is running for k1s rootful
pgrep -f "python -m ae.controller" >/dev/null || \
  (python -m ae.controller --loop --specs specs --watch --metrics-port 9108 \
     > /tmp/k1s_ctrl.log 2>&1 & sleep 5)

log "k1s rootful rollout-2 snapshots"
AE_COLLECT_PODMAN_SUDO=1 AE_ALLOW_PLAINTEXT_SECRETS=1 \
bash scripts/bench/run_rollout_k1s.sh \
  --label-suite baseline --app specs/examples/echo.yaml --app-name echo \
  --replicas 2 --duration 30 --sudo || exit 1

log "recombine + charts + docs"
python scripts/bench/mem_combine.py snapshots/*/* || exit 1
python scripts/bench/plot_overhead.py combined/combined.csv charts || exit 1
sudo make bench-fix-perms || true
python docs/build_docs.py || exit 1

log "done"
