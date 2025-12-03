sudo make bench-engines-clear CONFIRM=1 && make bench-engines-clear CONFIRM=1

export WAIT_READY_TRIES=180
export WAIT_READY_DELAY=5
export APP=specs/examples/echo-no-service.yaml
export AE_ALLOW_PLAINTEXT_SECRETS=1
export BENCH_DISABLE_INGRESS=1
export BENCH_AUTOCLEAN_PODMAN=1
export BENCH_KEEP_ENV=1
export SOPS_AGE_KEY_FILE=~/.config/ae/keys.txt
make bench-mem-e2e-minimal DURATION=30 REPLICAS=1

AE_BENCH_WATCH_APP=echo-nosvc AE_BENCH_WATCH_INTERVAL=2 AE_BENCH_WATCH_DIR=state/bench-env/debug ./scripts/bench/runtime_watch.sh

