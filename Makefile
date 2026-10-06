export DATABASE_URL ?= postgresql://app:app-dev-only@127.0.0.1:55218/app
PY = ./venv/bin/python
BASE = http://127.0.0.1:8218

bootstrap:      ## local virtualenv + database container
	python3 -m venv venv && ./venv/bin/pip install -q -r requirements-dev.txt
	docker compose up -d db
seed:           ## migrate and load the synthetic world (no-op if already seeded)
	$(PY) -m cr.seed --if-empty
dev:            ## API with reload on :8218, jobs run by a local worker
	($(PY) -m core.jobs cr.api &) && $(PY) -m uvicorn cr.api:app --reload --port 8218
test:
	$(PY) -m pytest --cov=cr --cov=core --cov-report=term-missing
demo:           ## the full stack in Docker, then the demo scenario against it
	docker compose up --build -d --wait && $(PY) -m core.scenario $(BASE)
load-test:      ## run `make demo` first
	$(PY) -m core.loadtest $(BASE) gc-director-demo 32 10 "GET /v1/projects/092bcfe4-4089-531a-a7af-551bc78a73fe/forecast" "GET /v1/projects/092bcfe4-4089-531a-a7af-551bc78a73fe/critical-path" "GET /v1/projects/092bcfe4-4089-531a-a7af-551bc78a73fe/risks" 
reset:          ## drop all data (volume included)
	docker compose down -v
.PHONY: bootstrap seed dev test demo load-test reset
