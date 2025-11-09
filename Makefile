.PHONY: install test lint run loop dev-up dev-down apply-sample status-sample logs-sample haproxy-update haproxy-watch install-systemd uninstall-systemd install-docs-service uninstall-docs-service start-here

install:
	python -m pip install -e .[dev]

watch:
	python -m pip install -e .[watch]

test:
	pytest -q

lint:
	ruff check
	mypy src/ae

dev-up:
	docker compose -f ops/dev/docker-compose.yaml up -d

dev-down:
	docker compose -f ops/dev/docker-compose.yaml down

loop:
	python -m ae.controller --loop --specs $${AE_SPECS_DIR:-specs} --metrics-port 9108 --watch

run:
	python -m ae.controller --once --specs $${AE_SPECS_DIR:-specs}

apply-sample:
	python -m ae.cli apply -f specs/examples/echo.yaml

status-sample:
	python -m ae.cli status echo --wide --events

logs-sample:
		python -m ae.cli logs echo --tail 50

# Build docs and open the Start Here page
start-here:
	@python docs/build_docs.py
	@echo "Open file://$$(pwd)/docs/site/start-here.html"
	@{ command -v xdg-open >/dev/null 2>&1 && xdg-open "$$(pwd)/docs/site/start-here.html" >/dev/null 2>&1; } || \
	 { command -v open >/dev/null 2>&1 && open "$$(pwd)/docs/site/start-here.html" >/dev/null 2>&1; } || true

haproxy-update:
		@python scripts/dev/update_haproxy_from_api.py --app $${APP:-tcp-echo} --server $${SERVER:-http://127.0.0.1:9108} --cfg $${CFG:-ops/dev/haproxy/haproxy.cfg} --port $${PORT:-9000}

haproxy-watch:
		@python scripts/dev/watch_haproxy.py --app $${APP:-tcp-echo} --server $${SERVER:-http://127.0.0.1:9108} --cfg $${CFG:-ops/dev/haproxy/haproxy.cfg} --port $${PORT:-9000} --compose $${COMPOSE:-ops/dev/docker-compose.yaml} --service $${SERVICE:-haproxy} --interval $${INTERVAL:-5}

install-systemd:
		@bash scripts/install.sh install --enable

uninstall-systemd:
			@bash scripts/install.sh uninstall --disable

install-docs-service:
			@bash scripts/install.sh docs-install --enable

uninstall-docs-service:
			@bash scripts/install.sh docs-uninstall --disable

.PHONY: docs
docs:
	python docs/build_docs.py

.PHONY: labs-k3d-up labs-k3d-down
labs-k3d-up:
	@./scripts/lab_k3d.sh up --name $${K3D_NAME:-k1s-labs} --http $${K3D_HTTP:-8081} --https $${K3D_HTTPS:-8444}

labs-k3d-down:
	@./scripts/lab_k3d.sh down --name $${K3D_NAME:-k1s-labs}

.PHONY: labs-up labs-down labs-aio-up labs-aio-down
labs-up:
	docker compose -f ops/dev/labs-compose.yaml up -d

labs-down:
	docker compose -f ops/dev/labs-compose.yaml down

labs-aio-up:
	docker compose -f ops/dev/labs-aio.yaml up -d

labs-aio-down:
	docker compose -f ops/dev/labs-aio.yaml down

.PHONY: demo demo-down integ-test
demo:
	./scripts/init_demo.sh $(if $(ARGS),$(ARGS),-y --demo-configs)

.PHONY: demo-help
demo-help:
	./scripts/init_demo.sh --help

demo-down:
	./scripts/init_demo.sh --down -y

integ-test:
	AE_INTEG_RUNTIME=$${AE_INTEG_RUNTIME:-podman} pytest -q tests/integration/

.PHONY: e2e e2e-multiport
e2e:
	@bash ./scripts/e2e/multiport.sh

e2e-multiport:
	@bash ./scripts/e2e/multiport.sh
# Benchmarks -------------------------------------------------------------

.PHONY: bench-mem-k1s bench-mem-k3s bench-mem-agg

bench-mem-k1s:
	@./scripts/bench/mem_snapshot.sh --mode k1s --label $${LABEL:-manual} --duration $${DURATION:-30}

bench-mem-k3s:
	@./scripts/bench/mem_snapshot.sh --mode k3s --label $${LABEL:-manual} --duration $${DURATION:-30}

# Aggregate latest snapshot under snapshots/<LABEL>/
bench-mem-agg:
	@python scripts/bench/mem_aggregate.py $$(ls -d snapshots/$${LABEL:-manual}/* | sort | tail -n1)

.PHONY: bench-mem-matrix-k1s bench-mem-combine bench-mem-verify

bench-mem-matrix-k1s:
	@./scripts/bench/run_matrix.sh --label-suite $${LABEL_SUITE:-baseline} --app $${APP:-specs/examples/echo.yaml} --app-name $${APP_NAME:-echo} --replicas $${REPLICAS:-1,5,10} --duration $${DURATION:-30}

bench-mem-combine:
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}

# Verify a specific snapshot (or latest for LABEL) and print per-container split
bench-mem-verify:
	@SNAP=$${SNAPSHOT:-$$(test -n "$$TS" && echo snapshots/$${LABEL:-manual}/$$TS || ls -d snapshots/$${LABEL:-manual}/* | sort | tail -n1)}; \
		echo "[verify] using $$SNAP"; \
		python scripts/bench/verify_snapshot.py $$SNAP $${JSON:+--json}

.PHONY: bench-k3s-up bench-k3s-down bench-mem-matrix-k3s

bench-k3s-up:
	@./scripts/bench/k3s_up.sh --name $${K3S_NAME:-bench}

bench-k3s-down:
	@./scripts/bench/k3s_up.sh --name $${K3S_NAME:-bench} --down

bench-mem-matrix-k3s:
	@./scripts/bench/run_matrix_k3s.sh --label-suite $${LABEL_SUITE:-baseline} --manifest $${MANIFEST:-specs/examples/k3s-echo.yaml} --replicas $${REPLICAS:-1,5,10} --duration $${DURATION:-30}

.PHONY: bench-mem-rollout-k1s bench-mem-rollout-k3s bench-mem-plot

bench-mem-rollout-k1s:
	@./scripts/bench/run_rollout_k1s.sh --label-suite $${LABEL_SUITE:-baseline-roll} --app $${APP:-specs/examples/echo.yaml} --app-name $${APP_NAME:-echo} --replicas $${REPLICAS:-5} --duration $${DURATION:-30}

bench-mem-rollout-k3s:
	@./scripts/bench/run_rollout_k3s.sh --label-suite $${LABEL_SUITE:-baseline-roll} --deploy $${DEPLOY:-echo} --namespace $${NS:-default} --replicas $${REPLICAS:-5} --duration $${DURATION:-30}

bench-mem-plot:
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}

.PHONY: bench-mem-e2e-k3s-sudo
# End-to-end K3s (with sudo for snapshots to capture accurate PSS)
bench-mem-e2e-k3s-sudo:
	@./scripts/bench/k3s_up.sh --name $${K3S_NAME:-bench}
	@./scripts/bench/run_matrix_k3s.sh \
		--label-suite $${LABEL_SUITE:-baseline} \
		--manifest $${MANIFEST:-specs/examples/k3s-echo.yaml} \
		--replicas $${REPLICAS:-1,5,10} \
		--duration $${DURATION:-30} \
		--sudo
	@./scripts/bench/run_rollout_k3s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-baseline-roll} \
		--deploy $${DEPLOY:-echo} \
		--namespace $${NS:-default} \
		--replicas $${ROLL_REPLICAS:-5} \
		--duration $${DURATION:-30} \
		--sudo
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}

.PHONY: bench-mem-e2e-k1s bench-mem-e2e-k3s

# End-to-end: k1s matrix + rollout + combine + plot
bench-mem-e2e-k1s:
	@./scripts/bench/run_matrix.sh \
		--label-suite $${LABEL_SUITE:-baseline} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${REPLICAS:-1,5,10} \
		--duration $${DURATION:-30}
	@./scripts/bench/run_rollout_k1s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-baseline-roll} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${ROLL_REPLICAS:-5} \
		--duration $${DURATION:-30}
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}

.PHONY: bench-mem-docs
# Combine, plot, and rebuild docs in one go
bench-mem-docs:
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@python docs/build_docs.py

.PHONY: bench-mem-backfill
# Aggregate any snapshots missing summary.json, then rebuild combined, charts, and docs


bench-mem-backfill:
	@echo "[backfill] scanning for snapshots without summary.json" >&2
	@python scripts/bench/backfill.py
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@python docs/build_docs.py

.PHONY: docs-watch
# Rebuild docs whenever combined/combined.csv changes
docs-watch:
	@python scripts/watch_docs.py

# End-to-end: k3s matrix + rollout + combine + plot (requires k3d cluster up)
bench-mem-e2e-k3s:
	@./scripts/bench/run_matrix_k3s.sh \
		--label-suite $${LABEL_SUITE:-baseline} \
		--manifest $${MANIFEST:-specs/examples/k3s-echo.yaml} \
		--replicas $${REPLICAS:-1,5,10} \
		--duration $${DURATION:-30}
	@./scripts/bench/run_rollout_k3s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-baseline-roll} \
		--deploy $${DEPLOY:-echo} \
		--namespace $${NS:-default} \
		--replicas $${ROLL_REPLICAS:-5} \
		--duration $${DURATION:-30}
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}

.PHONY: bench-mem-idle-k1s
bench-mem-idle-k1s:
	@bash ./scripts/bench/idle_baseline.sh --label $${LABEL:-idle-baseline} --duration $${DURATION:-30} $${ARGS:-}

.PHONY: bench-mem-idle-k3s
bench-mem-idle-k3s:
	@bash ./scripts/bench/idle_baseline_k3s.sh --label $${LABEL:-idle-k3s} --duration $${DURATION:-30} $${ARGS:-}

.PHONY: secrets-seal-demo
secrets-seal-demo:
	@bash ./scripts/seal_demo_secret.sh

# ---------------------------------------------------------------------------
# Container images (controller)

.PHONY: image-docker image-podman push-docker push-podman

# Variables:
#   IMAGE ?= ghcr.io/<org>/ae-controller:<tag>
#   TAG   ?= dev
#   BASE  ?= python:3.12-slim (Dockerfile) or python:3.12-alpine (Containerfile)

IMAGE ?= k1s/ae-controller:dev
TAG   ?= dev
BASE  ?=

image-docker:
	docker build \
		-f ops/controller/Dockerfile \
		--build-arg BASE_IMAGE=$${BASE:-python:3.12-slim} \
		-t $${IMAGE:-k1s/ae-controller:$(TAG)} \
		.

image-podman:
	podman build \
		-f ops/controller/Containerfile \
		--build-arg BASE_IMAGE=$${BASE:-python:3.12-alpine} \
		-t $${IMAGE:-k1s/ae-controller:$(TAG)} \
		.

push-docker:
	@test -n "$$IMAGE" || (echo "set IMAGE=<registry/repo>:<tag>" >&2; exit 2)
	docker push $$IMAGE

# ---------------------------------------------------------------------------
# Dev helpers

.PHONY: dashboard-reload
dashboard-reload:
	@bash -eu -c '\
	  echo "[dashboard] attempting controller reload"; \
	  if [ -f state/controller.pid ] && kill -0 $$(cat state/controller.pid) 2>/dev/null; then \
	    pid=$$(cat state/controller.pid); \
	    echo "[dashboard] killing controller (pid=$$pid); supervisor will restart it"; \
	    kill $$pid || true; \
	    exit 0; \
	  fi; \
	  if [ -f state/controller_supervisor.pid ] && kill -0 $$(cat state/controller_supervisor.pid) 2>/dev/null; then \
	    echo "[dashboard] supervisor is running but controller pid not found; it will respawn shortly"; \
	    exit 0; \
	  fi; \
	  echo "[dashboard] no supervisor detected; starting supervisor on default demo port"; \
	  if [ -f state/env.sh ]; then set -a; . state/env.sh; set +a; fi; \
	  PY_BIN=$${PY_BIN:-python}; SPECS=$${AE_SPECS_DIR:-specs}; PORT=$${API_PORT:-9108}; \
	  nohup bash scripts/supervise_controller.sh "$$PY_BIN" "$$SPECS" "$$PORT" >/dev/null 2>&1 & \
	  echo "[dashboard] supervisor started (port: $$PORT)"; \
	'

.PHONY: dashboard-restart
# Fully restart the supervisor so updated state/env.sh is applied.
# - Sends SIGTERM to the supervisor (which stops the child controller),
# - waits briefly for pid/lock cleanup,
# - then re-invokes dashboard-reload to start fresh.
dashboard-restart:
	@bash -eu scripts/dev/dashboard_restart.sh
	@$(MAKE) --no-print-directory dashboard-reload

push-podman:
	@test -n "$$IMAGE" || (echo "set IMAGE=<registry/repo>:<tag>" >&2; exit 2)
	podman push $$IMAGE
