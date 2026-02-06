.PHONY: install test lint run loop dev-up dev-down down apply-sample status-sample logs-sample haproxy-update haproxy-watch install-systemd uninstall-systemd install-docs-service uninstall-docs-service start-here k8s-smoke
.PHONY: dev-min dev-etcd k1s-core k1s-edge k1s-core-edge k1s-edge-core
.PHONY: k1s-core-caddy dev-min-caddy dev-etcd-caddy dev-local
.PHONY: shim-helm-demo

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

down:
	@bash scripts/stop_all.sh

loop:
	python -m ae.controller --loop --specs $${AE_SPECS_DIR:-specs} --metrics-port 9108 --watch

run:
	python -m ae.controller --once --specs $${AE_SPECS_DIR:-specs}

dev-min:
	@./scripts/dev/run_profile.sh dev-min

dev-etcd:
	@./scripts/dev/run_profile.sh dev-etcd

k1s-core:
	@./scripts/dev/run_profile.sh k1s-core

k1s-edge:
	@./scripts/dev/run_profile.sh k1s-edge

k1s-core-edge:
	@AE_TRANSPORT_BACKEND=nats-core ./scripts/dev/run_profile.sh k1s-core

k1s-edge-core:
	@EDGE_PROFILE=k1s-core ./scripts/dev/run_profile.sh k1s-edge

k1s-core-caddy:
	@CORE_CADDY=1 ./scripts/dev/run_profile.sh k1s-core

dev-min-caddy:
	@CORE_CADDY=1 ./scripts/dev/run_profile.sh dev-min

dev-etcd-caddy:
	@CORE_CADDY=1 ./scripts/dev/run_profile.sh dev-etcd

dev-local:
	@AE_DEV_LOCAL=1 ./scripts/dev/ensure_dev_local.sh

apply-sample:
	python -m ae.cli apply -f specs/examples/echo.yaml

status-sample:
	python -m ae.cli status echo --wide --events

logs-sample:
		python -m ae.cli logs echo --tail 50

shim-helm-demo:
	@echo "[shim-helm-demo] PORT=$${PORT:-8445} TOKEN=$${TOKEN:-helm-demo} RUNTIME=$${RUNTIME:-stub}"
	@PORT=$${PORT:-8445} TOKEN=$${TOKEN:-helm-demo} RUNTIME=$${RUNTIME:-stub} \
		bash scripts/helm_shim_demo.sh

# Render and validate Kubernetes YAML for examples (no cluster required)
k8s-smoke:
	@echo "[k8s-smoke] exporting echo -> /tmp/echo-k8s.yaml"
	@PYTHONPATH=src python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --ingress-class traefik --validate > /tmp/echo-k8s.yaml
	@echo "[k8s-smoke] exporting envfrom-and-projection -> /tmp/envfrom-k8s.yaml"
	@PYTHONPATH=src python -m ae.cli export-k8s -f specs/examples/envfrom-and-projection.yaml --namespace demo --emit-configs --emit-secrets --validate > /tmp/envfrom-k8s.yaml
	@echo "[k8s-smoke] running portability check (strict)"
	@PYTHONPATH=src python -m ae.cli k8s-check -f specs/examples/echo.yaml --policy strict || true

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

.PHONY: docs docs-export docs-wiki-export
DOCS_OUT_DIR ?= docs/export
docs:
	@SNAP_ROOT=$${SNAP_ROOT:-snapshots}; \
	if find "$$SNAP_ROOT" -maxdepth 2 -name summary.json -print -quit 2>/dev/null | grep -q .; then \
	  python scripts/bench/mem_combine.py $${GLOB:-$$SNAP_ROOT/*/*}; \
	else \
	  echo "[docs] no snapshots under $$SNAP_ROOT; skipping mem_combine"; \
	fi
	@if test -f $${CSV:-combined/combined.csv}; then \
	  python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}; \
	else \
	  echo "[docs] missing combined/combined.csv; skipping chart regeneration"; \
	fi
	@python docs/build_docs.py

docs-export:
	@DOCS_OUT_DIR=$(DOCS_OUT_DIR) DOCS_NON_INTERACTIVE=1 $(MAKE) docs

docs-wiki-export:
	@DOCS_WIKI_OUT_DIR=$${WIKI_OUT:-docs/wiki} DOCS_NON_INTERACTIVE=1 python docs/export_wiki.py

.PHONY: labs-k3d-up labs-k3d-down
labs-k3d-up:
	@./scripts/lab_k3d.sh up --name $${K3D_NAME:-k1s-labs} --http $${K3D_HTTP:-8081} --https $${K3D_HTTPS:-8444}

labs-k3d-down:
	@./scripts/lab_k3d.sh down --name $${K3D_NAME:-k1s-labs}

.PHONY: labs-up labs-down labs-aio-up labs-aio-down labs-apishim-env
labs-up:
	@./scripts/ensure_dev_env.sh
	@./scripts/ensure_apishim_env.sh >/dev/null
	@LABS_TOKEN=$$(awk -F= '/^AE_LABS_TOKEN=/{print $$2}' state/profiles/labs/apishim.env); \
	  if [ -n "$$LABS_TOKEN" ]; then DOCS_LABS_TOKEN="$$LABS_TOKEN" python docs/build_docs.py || true; fi
	docker compose -f ops/dev/labs-compose.yaml up -d

labs-down:
	docker compose -f ops/dev/labs-compose.yaml down

.PHONY: apishim-smoke
apishim-smoke:
	@echo "[apishim-smoke] starting shim on 127.0.0.1:8445 with token=smoke"
	@AE_APISHIM_ENABLE=1 AE_APISHIM_TOKEN=smoke PYTHONPATH=src \
	  python -m ae.apishim serve --host 127.0.0.1 --port 8445 --token smoke >/tmp/apishim-smoke.log 2>&1 & \
	  pid=$$!; \
	  sleep 2; \
	  curl -fsS -H "Authorization: Bearer smoke" http://127.0.0.1:8445/healthz >/dev/null; \
	  rc=$$?; \
	  kill $$pid >/dev/null 2>&1 || true; \
	  wait $$pid >/dev/null 2>&1 || true; \
	  if [ $$rc -eq 0 ]; then echo "[apishim-smoke] ok"; else echo "[apishim-smoke] FAILED (see /tmp/apishim-smoke.log)"; fi; \
	  exit $$rc

labs-aio-up:
	@./scripts/ensure_apishim_env.sh
	@./scripts/ensure_dev_env.sh
	@LABS_TOKEN=$$(awk -F= '/^AE_LABS_TOKEN=/{print $$2}' state/profiles/labs/apishim.env); \
	  if [ -n "$$LABS_TOKEN" ]; then DOCS_LABS_TOKEN="$$LABS_TOKEN" python docs/build_docs.py || true; fi
	@LABS_PROFILE_ARGS=""; \
	  if [ "$${AE_STATE_BACKEND:-sqlite}" = "etcd" ]; then LABS_PROFILE_ARGS="--profile etcd"; fi; \
	  if [ "$${AE_LABS_USE_POSTGRES:-0}" = "1" ]; then \
	  if [ -z "$${AE_APISHIM_DSN:-}" ] && [ -z "$${AE_STATE_DSN:-}" ]; then \
	    export AE_APISHIM_DSN="postgresql://shim:shim@postgres:5432/shim"; \
	    export AE_STATE_DSN="postgresql://shim:shim@postgres:5432/shim"; \
	  fi; \
	  docker compose $$LABS_PROFILE_ARGS --profile postgres -f ops/dev/labs-aio.yaml up -d postgres; \
	  for i in $$(seq 1 30); do \
	    if docker compose --profile postgres -f ops/dev/labs-aio.yaml exec -T postgres pg_isready -U shim -d shim >/dev/null 2>&1; then \
	      break; \
	    fi; \
	    sleep 1; \
	  done; \
	  docker compose $$LABS_PROFILE_ARGS --profile postgres -f ops/dev/labs-aio.yaml up -d; \
	else \
	  docker compose $$LABS_PROFILE_ARGS -f ops/dev/labs-aio.yaml up -d; \
	fi

labs-aio-down:
	docker compose -f ops/dev/labs-aio.yaml down

labs-apishim-env:
	@if [ ! -f state/profiles/labs/apishim.env ]; then \
	  echo "[labs] state/profiles/labs/apishim.env not found; run make labs-aio-up first."; \
	  exit 1; \
	fi
	@echo "[labs] apishim tokens (dev only):"
	@cat state/profiles/labs/apishim.env

.PHONY: demo demo-down integ-test
demo:
	@TOKEN=$${AE_LABS_TOKEN:-D34DB33F}; \
	  SOPS_AGE_KEY_FILE=$${SOPS_AGE_KEY_FILE:-$$HOME/.config/ae/keys.txt} \
	  AE_ALLOW_PLAINTEXT_SECRETS=$${AE_ALLOW_PLAINTEXT_SECRETS:-1} \
	  AE_RUNTIME_BACKEND=$${AE_RUNTIME_BACKEND:-podman} \
	  ./scripts/init_demo.sh $(if $(ARGS),$(ARGS),-y -d --labs --labs-token "$$TOKEN")

.PHONY: demo-help
demo-help:
	./scripts/init_demo.sh --help

demo-down:
	./scripts/init_demo.sh --down -y

.PHONY: reg-cache-reset
reg-cache-reset:
	./scripts/init_demo.sh --reset-registry-cache -y

.PHONY: demo-hardened
demo-hardened:
	./scripts/init_demo.sh --demo-hardened -y -d

# Reset demo/labs state so a fresh init only reconciles the curated demo apps
.PHONY: demo-reset
demo-reset:
	@echo "[demo-reset] stopping controller/docs and dev stacks"
	@bash scripts/stop_all.sh
	@{ command -v docker >/dev/null 2>&1 && docker compose -f ops/dev/labs-aio.yaml down >/dev/null 2>&1 || true; }
	@{ command -v podman >/dev/null 2>&1 && podman compose -f ops/dev/labs-aio.yaml down >/dev/null 2>&1 || true; }
	@{ command -v docker >/dev/null 2>&1 && docker compose -f ops/dev/labs-compose.yaml down >/dev/null 2>&1 || true; }
	@{ command -v podman >/dev/null 2>&1 && podman compose -f ops/dev/labs-compose.yaml down >/dev/null 2>&1 || true; }
	@echo "[demo-reset] clearing dynamic Caddy sites"
	@rm -f state/caddy/*.caddy 2>/dev/null || true
	@echo "[demo-reset] removing controller DB (state/profiles/demo/controller.db)"
	@rm -f state/profiles/demo/controller.db 2>/dev/null || true
	@rm -f state/controller.db 2>/dev/null || true
	@{ if [ -f state/env.sh ]; then \
	  . state/env.sh >/dev/null 2>&1 || true; \
	  if [ -n "$$AE_STATE_DB" ] && [ "$$AE_STATE_DB" != "state/profiles/demo/controller.db" ]; then \
	    echo "[demo-reset] removing controller DB ($$AE_STATE_DB)"; \
	    rm -f "$$AE_STATE_DB" 2>/dev/null || true; \
	  fi; \
	fi; }
	@echo "[demo-reset] removing shim DB (state/apishim.db)"
	@rm -f state/apishim.db 2>/dev/null || true
	@echo "[demo-reset] pruning ae.app volumes (docker/podman)"
	@{ command -v docker >/dev/null 2>&1 && docker volume ls -q --filter label=ae.app | xargs -r docker volume rm >/dev/null 2>&1; } || true
	@{ command -v podman >/dev/null 2>&1 && podman volume ls -q --filter label=ae.app | xargs -r podman volume rm >/dev/null 2>&1; } || true
	@echo "[demo-reset] removing curated specs directory (state/profiles/demo/specs)"
	@rm -rf state/profiles/demo/specs 2>/dev/null || true
	@rm -rf state/demo-specs 2>/dev/null || true
	@echo "[demo-reset] done"

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

.PHONY: bench-mem-debug
# Short sanity-check run for rootless, rootful, and k1nd with debug artifacts.
bench-mem-debug:
	@./scripts/bench/run_debug_refresh.sh

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
		--label-suite $${LABEL_SUITE_ROLL:-$${LABEL_SUITE:-baseline}} \
		--deploy $${DEPLOY:-echo} \
		--namespace $${NS:-default} \
		--replicas $${ROLL_REPLICAS:-2,5} \
		--duration $${DURATION:-30} \
		--sudo
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}

.PHONY: bench-mem-e2e-k1s bench-mem-e2e-k3s

# End-to-end: k1s matrix + rollout + combine + plot
bench-mem-e2e-k1s:
	@./scripts/bench/podman_rootless_fix.sh
	@PYTHONPATH=$${PYTHONPATH:-src} AE_COLLECT_PODMAN_SUDO=$${AE_COLLECT_PODMAN_SUDO:-0} ./scripts/bench/run_matrix.sh \
		--label-suite $${LABEL_SUITE:-baseline} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${REPLICAS:-1,5,10} \
		--duration $${DURATION:-30}
	@PYTHONPATH=$${PYTHONPATH:-src} AE_COLLECT_PODMAN_SUDO=$${AE_COLLECT_PODMAN_SUDO:-0} ./scripts/bench/run_rollout_k1s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-$${LABEL_SUITE:-baseline}} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${ROLL_REPLICAS:-2,5} \
		--duration $${DURATION:-30}
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}

.PHONY: bench-podman-rootful-socket
# Ensure rootful Podman socket is available (systemd socket or service fallback).
bench-podman-rootful-socket:
	@./scripts/bench/podman_rootful_socket.sh

.PHONY: bench-mem-e2e-k1s-sudo
# End-to-end: k1s matrix + rollout, but escalate snapshots with --sudo
bench-mem-e2e-k1s-sudo:
	@$(MAKE) bench-podman-rootful-socket
	@./scripts/bench/engines_clear.sh --confirm
	@PYTHONPATH=$${PYTHONPATH:-src} AE_COLLECT_PODMAN_SUDO=$${AE_COLLECT_PODMAN_SUDO:-1} ./scripts/bench/run_matrix.sh \
		--label-suite $${LABEL_SUITE:-baseline} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${REPLICAS:-1,5,10} \
		--duration $${DURATION:-30} \
		--sudo
	@PYTHONPATH=$${PYTHONPATH:-src} AE_COLLECT_PODMAN_SUDO=$${AE_COLLECT_PODMAN_SUDO:-1} ./scripts/bench/run_rollout_k1s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-$${LABEL_SUITE:-baseline}} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${ROLL_REPLICAS:-2,5} \
		--duration $${DURATION:-30} \
		--sudo
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}

.PHONY: bench-mem-cri bench-mem-cri-quick
# End-to-end: CRI backend (containerd) matrix + rollout + combine + plot
bench-mem-cri:
	@./scripts/bench/run_cri_refresh.sh

# Quick CRI run (override DURATION/REPLICAS/ROLL_REPLICAS as needed)
bench-mem-cri-quick:
	@DURATION=$${DURATION:-10} \
	 REPLICAS=$${REPLICAS:-1,5,10} \
	 ROLL_REPLICAS=$${ROLL_REPLICAS:-2,5} \
	 ./scripts/bench/run_cri_refresh.sh

.PHONY: bench-mem-e2e-k1nd
# End-to-end: k1nd (k1s-in-Docker via labs-aio compose) matrix + rollout + combine + plot
# - Ensures the compose stack with controller + caddy is running
# - Uses Docker on the host for preflights and container cgroup metrics
# - Skips guard auto-start to avoid spawning a host controller
bench-mem-e2e-k1nd:
	@scripts/bench/k1nd_sanitize.sh pre
	@$(MAKE) labs-aio-up
	# Use a writable, isolated state DB for host-side CLI during k1nd runs
	@AE_CLI_IN_CONTAINER=$${AE_CLI_IN_CONTAINER:-1} AE_COLLECT_ENGINE=$${AE_COLLECT_ENGINE:-docker} AE_ENGINE_STRICT=$${AE_ENGINE_STRICT:-1} AE_SERIAL_SERVICE_ROLLOUT=$${AE_SERIAL_SERVICE_ROLLOUT:-1} AE_STATE_DB=$${AE_STATE_DB:-/tmp/k1s-bench-$$(id -un).db} AE_RUNTIME_BACKEND=docker SKIP_GUARDS=1 bash ./scripts/bench/run_matrix.sh \
		--label-suite $${LABEL_SUITE:-baseline} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${REPLICAS:-1,5,10} \
		--duration $${DURATION:-30}
	@AE_CLI_IN_CONTAINER=$${AE_CLI_IN_CONTAINER:-1} AE_COLLECT_ENGINE=$${AE_COLLECT_ENGINE:-docker} AE_ENGINE_STRICT=$${AE_ENGINE_STRICT:-1} AE_SERIAL_SERVICE_ROLLOUT=$${AE_SERIAL_SERVICE_ROLLOUT:-1} AE_STATE_DB=$${AE_STATE_DB:-/tmp/k1s-bench-$$(id -un).db} AE_RUNTIME_BACKEND=docker SKIP_GUARDS=1 bash ./scripts/bench/run_rollout_k1s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-$${LABEL_SUITE:-baseline}} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${ROLL_REPLICAS:-2,5} \
		--duration $${DURATION:-30}
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@scripts/bench/k1nd_sanitize.sh post

.PHONY: bench-mem-e2e-k1nd-sudo
# Same as bench-mem-e2e-k1nd but runs snapshots with sudo to capture full PSS
bench-mem-e2e-k1nd-sudo:
	@scripts/bench/k1nd_sanitize.sh pre
	@$(MAKE) labs-aio-up
	@AE_CLI_IN_CONTAINER=$${AE_CLI_IN_CONTAINER:-1} AE_COLLECT_ENGINE=$${AE_COLLECT_ENGINE:-docker} AE_ENGINE_STRICT=$${AE_ENGINE_STRICT:-1} AE_SERIAL_SERVICE_ROLLOUT=$${AE_SERIAL_SERVICE_ROLLOUT:-1} AE_STATE_DB=$${AE_STATE_DB:-/tmp/k1s-bench-$$(id -un).db} AE_RUNTIME_BACKEND=docker SKIP_GUARDS=1 bash ./scripts/bench/run_matrix.sh \
		--label-suite $${LABEL_SUITE:-baseline} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${REPLICAS:-1,5,10} \
		--duration $${DURATION:-30} \
		--sudo
	@AE_CLI_IN_CONTAINER=$${AE_CLI_IN_CONTAINER:-1} AE_COLLECT_ENGINE=$${AE_COLLECT_ENGINE:-docker} AE_ENGINE_STRICT=$${AE_ENGINE_STRICT:-1} AE_SERIAL_SERVICE_ROLLOUT=$${AE_SERIAL_SERVICE_ROLLOUT:-1} AE_STATE_DB=$${AE_STATE_DB:-/tmp/k1s-bench-$$(id -un).db} AE_RUNTIME_BACKEND=docker SKIP_GUARDS=1 bash ./scripts/bench/run_rollout_k1s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-$${LABEL_SUITE:-baseline}} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${ROLL_REPLICAS:-2,5} \
		--duration $${DURATION:-30} \
		--sudo
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@scripts/bench/k1nd_sanitize.sh post

.PHONY: bench-mem-e2e-k1nd-quick
# Fast profile: in-container CLI, no warm, shorter snapshots, fewer waits
bench-mem-e2e-k1nd-quick:
	@scripts/bench/k1nd_sanitize.sh pre
	@$(MAKE) labs-aio-up
	@AE_CLI_IN_CONTAINER=1 AE_BENCH_QUICK=1 SKIP_IDLE=$${SKIP_IDLE:-1} PRUNE_OLD=$${PRUNE_OLD:-1} SKIP_EXISTING=$${SKIP_EXISTING:-1} \
		AE_COLLECT_ENGINE=$${AE_COLLECT_ENGINE:-docker} \
		AE_ENGINE_STRICT=$${AE_ENGINE_STRICT:-1} \
		AE_SERIAL_SERVICE_ROLLOUT=$${AE_SERIAL_SERVICE_ROLLOUT:-1} \
		AE_STATE_DB=$${AE_STATE_DB:-/tmp/k1s-bench-$$(id -un).db} \
		AE_RUNTIME_BACKEND=docker SKIP_GUARDS=1 bash ./scripts/bench/run_matrix.sh \
		--label-suite $${LABEL_SUITE:-baseline} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${REPLICAS:-1,5,10} \
		--duration $${DURATION:-10} \
		--sudo
	@AE_CLI_IN_CONTAINER=1 AE_BENCH_QUICK=1 AE_COLLECT_ENGINE=$${AE_COLLECT_ENGINE:-docker} AE_ENGINE_STRICT=$${AE_ENGINE_STRICT:-1} AE_SERIAL_SERVICE_ROLLOUT=$${AE_SERIAL_SERVICE_ROLLOUT:-1} SKIP_EXISTING=$${SKIP_EXISTING:-1} \
		AE_STATE_DB=$${AE_STATE_DB:-/tmp/k1s-bench-$$(id -un).db} \
		AE_RUNTIME_BACKEND=docker SKIP_GUARDS=1 bash ./scripts/bench/run_rollout_k1s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-$${LABEL_SUITE:-baseline}} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${ROLL_REPLICAS:-2,5} \
		--duration $${DURATION:-10} \
		--sudo
	@scripts/bench/k1nd_sanitize.sh post

.PHONY: bench-mem-e2e-k1nd-resume-rollout
# Resume only the rollout stage (use the same LABEL_SUITE as the previous matrix stage)
bench-mem-e2e-k1nd-resume-rollout:
	@scripts/bench/k1nd_sanitize.sh pre
	@$(MAKE) labs-aio-up
	@AE_CLI_IN_CONTAINER=$${AE_CLI_IN_CONTAINER:-1} AE_COLLECT_ENGINE=$${AE_COLLECT_ENGINE:-docker} AE_ENGINE_STRICT=$${AE_ENGINE_STRICT:-1} AE_SERIAL_SERVICE_ROLLOUT=$${AE_SERIAL_SERVICE_ROLLOUT:-1} SKIP_EXISTING=$${SKIP_EXISTING:-1} \
	 AE_STATE_DB=$${AE_STATE_DB:-/tmp/k1s-bench-$$(id -un).db} \
	 AE_RUNTIME_BACKEND=docker SKIP_GUARDS=1 bash ./scripts/bench/run_rollout_k1s.sh \
		--label-suite $${LABEL_SUITE_ROLL:-$${LABEL_SUITE:-baseline}} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${ROLL_REPLICAS:-2,5} \
		--duration $${DURATION:-30}
		--label-suite $${LABEL_SUITE_ROLL:-$${LABEL_SUITE:-baseline}} \
		--app $${APP:-specs/examples/echo.yaml} \
		--app-name $${APP_NAME:-echo} \
		--replicas $${ROLL_REPLICAS:-2,5} \
		--duration $${DURATION:-10}
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@scripts/bench/k1nd_sanitize.sh post

.PHONY: bench-mem-e2e-k1nd-down
# Same as bench-mem-e2e-k1nd but tears down the compose stack afterwards
bench-mem-e2e-k1nd-down:
	@$(MAKE) bench-mem-e2e-k1nd
	@$(MAKE) labs-aio-down

.PHONY: bench-mem-e2e-all bench-mem-e2e-minimal
bench-mem-e2e-all:
	@DURATION=$${DURATION:-30} \
	 REPLICAS=$${REPLICAS:-1,5,10} \
	 ROLL_REPLICAS=$${ROLL_REPLICAS:-2,5} \
	 LABEL_ROOTFUL=$${LABEL_ROOTFUL:-r$$(date +%Y%m%d)+podman+rootful+cg2} \
	 LABEL_ROOTLESS=$${LABEL_ROOTLESS:-r$$(date +%Y%m%d)+podman+rootless+cg2} \
	 LABEL_K1ND=$${LABEL_K1ND:-r$$(date +%Y%m%d)+docker+k1nd} \
	 BENCH_MANIFEST=$${APP:-specs/examples/echo.yaml} \
	 ./scripts/bench/run_e2e_suite.sh --mode full --manifest $${APP:-specs/examples/echo.yaml}

bench-mem-e2e-minimal:
	@DURATION=$${DURATION:-10} \
	 REPLICAS=$${REPLICAS:-1} \
	 ROLL_REPLICAS=$${ROLL_REPLICAS:-2} \
	 LABEL_ROOTFUL=$${LABEL_ROOTFUL:-r$$(date +%Y%m%d)+podman+rootful+cg2} \
	 BENCH_MANIFEST=$${APP:-specs/examples/echo.yaml} \
	 ./scripts/bench/run_e2e_suite.sh --mode minimal --manifest $${APP:-specs/examples/echo.yaml}

.PHONY: bench-watch-runtime
bench-watch-runtime:
	@echo "[bench-watch] capturing podman/container debug output; press Ctrl+C to stop"
	@sudo env HOME=/root XDG_RUNTIME_DIR=/run/user/0 DBUS_SESSION_BUS_ADDRESS= CONTAINER_HOST= PODMAN_HOST= \
		AE_PODMAN_BIN=$${AE_PODMAN_BIN:-podman} \
		AE_BENCH_WATCH_APP=$${APP:-echo} \
		AE_BENCH_WATCH_INTERVAL=$${INTERVAL:-10} \
		AE_BENCH_WATCH_DIR=$${OUT_DIR:-state/bench-env/debug} \
		AE_BENCH_CONTROLLER_LOG=$${CONTROLLER_LOG:-state/bench-env/controller.log} \
		scripts/bench/runtime_watch.sh

.PHONY: bench-mem-e2e-baselines bench-mem-e2e-baselines-sudo
# Run all baseline suites (k1s rootless, k1s rootful, k1nd, k3d),
# with engine cleanup between, then rebuild charts/docs and print summary.
# Interactive runs will prompt once for sudo unless ALLOW_SUDO is set.
bench-mem-e2e-baselines:
	@bash ./scripts/bench/run_all_baselines.sh

# CI-friendly variant: enable sudo steps non-interactively (requires NOPASSWD or cached credentials).
bench-mem-e2e-baselines-sudo:
	@ALLOW_SUDO=1 bash ./scripts/bench/run_all_baselines.sh

.PHONY: bench-mem-docs
# Combine, plot, and rebuild docs in one go
bench-mem-docs:
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@python docs/build_docs.py

.PHONY: bench-fix-perms
# Normalize ownership/permissions for result artifacts (useful after sudo runs)
# - If run with sudo, uses SUDO_USER to assign back to the invoking user
# - Always tries to set permissive read/execute for directories and read for files
bench-fix-perms:
	@OWN=$${SUDO_USER:-$$(id -un)}; GRP=$$(id -gn $$OWN); \
	 for d in charts combined snapshots docs/site; do \
	   if [ -e "$$d" ]; then \
	     echo "[bench-fix-perms] processing $$d as $$OWN:$$GRP" >&2; \
	     if [ "$$OWN" != "$$(id -un)" ]; then \
	       chown -R "$$OWN:$$GRP" "$$d" >/dev/null 2>&1 || true; \
	     fi; \
	     find "$$d" -type d -exec chmod u+rwx,go+rx {} + >/dev/null 2>&1 || true; \
	     find "$$d" -type f -exec chmod u+rw,go+r {} + >/dev/null 2>&1 || true; \
	   fi; \
	 done; \
	 echo "[bench-fix-perms] done"

.PHONY: bench-snapshots-clean bench-snapshots-clean-sudo
# Remove invalid snapshots from the last N hours; optionally include quick-* labels.
# Usage:
#   make bench-snapshots-clean HOURS=24
#   make bench-snapshots-clean NO_QUICK=1
#   make bench-snapshots-clean DRY_RUN=1
#   sudo make bench-snapshots-clean-sudo HOURS=24
bench-snapshots-clean:
	@python scripts/bench/cleanup_recent_snapshots.py --hours $${HOURS:-24} $${NO_QUICK:+--no-quick} $${DRY_RUN:+--dry-run}

bench-snapshots-clean-sudo:
	@sudo python scripts/bench/cleanup_recent_snapshots.py --hours $${HOURS:-24} $${NO_QUICK:+--no-quick} $${DRY_RUN:+--dry-run}

.PHONY: bench-state-clean dev-state-clean
# Remove benchmark-only state (state/bench-*)
bench-state-clean:
	@./scripts/bench/clean_state.sh --bench

# Wipe full state/ directory (requires CONFIRM=1)
dev-state-clean:
	@bash scripts/stop_all.sh
	@CONFIRM=$${CONFIRM:-0} ./scripts/bench/clean_state.sh --dev $${CONFIRM:+--confirm}

.PHONY: bench-mem-backfill
# Aggregate any snapshots missing summary.json, then rebuild combined, charts, and docs


bench-mem-backfill:
	@echo "[backfill] scanning for snapshots without summary.json" >&2
	@python scripts/bench/backfill.py
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@python docs/build_docs.py

.PHONY: bench-engines-clear
# Danger: stop and remove ALL Docker and Podman containers (rootful) to avoid leakage into benchmarks.
# Usage: run with sudo and explicit confirmation:
#   sudo make bench-engines-clear CONFIRM=1
bench-engines-clear:
	@./scripts/bench/engines_clear.sh $${CONFIRM:+--confirm}

.PHONY: bench-mem-backfill-oci
# Backfill: insert detected OCI runtime (e.g., crun/runc) into snapshot meta and labels,
# then recombine results and regenerate charts (and optionally docs).
#
# Variables:
#   LABEL   - snapshot label directory to target (e.g., r20251110+podman+rootless+cg2*);
#             if empty, processes all snapshots.
#   GLOB    - explicit snapshot path/glob (overrides LABEL), e.g., 'snapshots/r*/2025*'.
#   OCI     - override runtime name; if empty, auto-detects via podman/docker info.
#   REBUILD_DOCS - set to 1 to rebuild docs after charts (default 0).
bench-mem-backfill-oci:
	@SNAP_GLOB=$${GLOB:-$$( if test -n "$$LABEL"; then printf '%s' "snapshots/$$LABEL/*"; else printf '%s' "snapshots/*/*"; fi )}; \
		echo "[oci-backfill] targeting $$SNAP_GLOB (override with GLOB=...)" >&2; \
		python scripts/bench/label_backfill.py "$$SNAP_GLOB" --insert-into-label $${OCI:+--oci $$OCI}
	@python scripts/bench/mem_combine.py $${GLOB_COMBINE:-snapshots/*/*}
	@python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@{ test "$$REBUILD_DOCS" = "1" && python docs/build_docs.py || true; }

.PHONY: bench-mem-backfill-oci-latest
# Detect the most recent label directory under snapshots/ and backfill just that label.
# Pass through OCI and REBUILD_DOCS as in bench-mem-backfill-oci.
bench-mem-backfill-oci-latest:
	@LBL=$${LABEL:-$$(ls -1td snapshots/* 2>/dev/null | head -n1 | awk -F/ '{print $$2}')}; \
		if [ -z "$$LBL" ]; then echo "[oci-backfill-latest] no snapshots found" >&2; exit 0; fi; \
		echo "[oci-backfill-latest] latest label=$$LBL" >&2; \
		$(MAKE) bench-mem-backfill-oci LABEL="$$LBL*" $${OCI:+OCI=$$OCI} $${REBUILD_DOCS:+REBUILD_DOCS=$$REBUILD_DOCS}

.PHONY: bench-mem-finalize-sudo
# Finalize benchmarks after mixed non-root/root runs (run with sudo):
# - Backfill OCI runtime into labels and metadata across ALL snapshots
# - Recombine results and regenerate charts with a wider history window
# - Rebuild docs with a one-week staleness threshold
# - Normalize permissions back to the invoking user
#
# Usage:
#   sudo make bench-mem-finalize-sudo
# Optional env:
#   PLOT_LATEST=500 (default) to include more history in legacy timelines
#   DOCS_CHART_STALENESS_HOURS=168 (default) to tweak staleness window
bench-mem-finalize-sudo:
	@echo "[finalize] backfilling OCI across all snapshots" >&2
	@sudo env PATH="$${PATH}" PYTHONPATH="$${PYTHONPATH:-}" python scripts/bench/label_backfill.py "snapshots/*/*" --insert-into-label || true
	@echo "[finalize] combining snapshots" >&2
	@sudo env PATH="$${PATH}" PYTHONPATH="$${PYTHONPATH:-}" python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}
	@echo "[finalize] plotting charts (PLOT_LATEST=$${PLOT_LATEST:-500})" >&2
	@sudo env PATH="$${PATH}" PYTHONPATH="$${PYTHONPATH:-}" PLOT_LATEST=$${PLOT_LATEST:-500} python scripts/bench/plot_overhead.py $${CSV:-combined/combined.csv} $${OUTDIR:-charts}
	@echo "[finalize] building docs (DOCS_CHART_STALENESS_HOURS=$${DOCS_CHART_STALENESS_HOURS:-168})" >&2
	@sudo env PATH="$${PATH}" PYTHONPATH="$${PYTHONPATH:-}" DOCS_CHART_STALENESS_HOURS=$${DOCS_CHART_STALENESS_HOURS:-168} python docs/build_docs.py
	@$(MAKE) bench-fix-perms

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
		--replicas $${ROLL_REPLICAS:-2,5} \
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
# Build a wheel into dist/
.PHONY: wheel
wheel:
	python -m build -w

# Build controller container image
.PHONY: docker-build-controller
docker-build-controller:
	docker build -f ops/images/controller.Dockerfile -t ae/controller:dev .

# Run controller container (bind specs and state)
.PHONY: docker-run-controller
docker-run-controller:
	docker run --rm -it \
	  -v $(PWD)/specs:/specs:ro \
	  -v $(PWD)/state:/state \
	  -e AE_STATE_DB=/state/controller.db \
	  -p 9108:9108 \
	  ae/controller:dev
