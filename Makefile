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
