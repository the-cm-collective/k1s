.PHONY: install test lint run loop dev-up dev-down apply-sample status-sample logs-sample

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
	python -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch

run:
	python -m ae.controller --once --specs specs/

apply-sample:
	python -m ae.cli apply -f specs/examples/echo.yaml

status-sample:
	python -m ae.cli status echo --wide --events

logs-sample:
	python -m ae.cli logs echo --tail 50

.PHONY: docs
docs:
	python docs/build_docs.py

.PHONY: demo demo-down integ-test
demo:
	./scripts/init_demo.sh $(if $(ARGS),$(ARGS),-y --demo-configs)

.PHONY: demo-help
demo-help:
	./scripts/init_demo.sh --help

demo-down:
	./scripts/init_demo.sh --down -y

integ-test:
	AE_DOCKER_TEST=1 pytest -q tests/integration/
# Benchmarks -------------------------------------------------------------

.PHONY: bench-mem-k1s bench-mem-k3s bench-mem-agg

bench-mem-k1s:
	@./scripts/bench/mem_snapshot.sh --mode k1s --label $${LABEL:-manual} --duration $${DURATION:-30}

bench-mem-k3s:
	@./scripts/bench/mem_snapshot.sh --mode k3s --label $${LABEL:-manual} --duration $${DURATION:-30}

# Aggregate latest snapshot under snapshots/<LABEL>/
bench-mem-agg:
	@python scripts/bench/mem_aggregate.py $$(ls -d snapshots/$${LABEL:-manual}/* | sort | tail -n1)

.PHONY: bench-mem-matrix-k1s bench-mem-combine

bench-mem-matrix-k1s:
	@./scripts/bench/run_matrix.sh --label-suite $${LABEL_SUITE:-baseline} --app $${APP:-specs/examples/echo.yaml} --app-name $${APP_NAME:-echo} --replicas $${REPLICAS:-1,5,10} --duration $${DURATION:-30}

bench-mem-combine:
	@python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}

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
