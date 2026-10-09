# Shortcuts for the workshop. Run `make help` to list them.

VENV   := hacknc_venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help env install lock build sync up redeploy down reset ps logs test test-integration lint walkthrough ui-dev realm opa-test tamper-audit cover-up reset-audit reset-demo curate admissions

help:  ## List available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

env:  ## Create .env from .env.example, or add any keys .env is missing
	@test -f .env || (cp .env.example .env && echo "created .env")
	@grep -E '^[A-Z_]+=' .env.example | while IFS='=' read -r key value; do \
		grep -q "^$$key=" .env || { echo "$$key=$$value" >> .env; echo "added $$key to .env"; }; \
	done

install: env  ## Install the Python package + dev tools into hacknc_venv (exact locked versions)
	$(PIP) install -r requirements.lock -r requirements-dev.lock
	$(PIP) install --no-deps -e .

# Upgrades are deliberate: resolve the newest versions pyproject.toml allows, in the same
# Python image the services run on, then rebuild and run every test before committing.
lock:  ## Re-resolve dependencies into requirements*.lock (then: make install sync test test-integration)
	@docker run --rm -v $(CURDIR):/src:ro python:3.14.8-slim sh -c \
		'mkdir /tmp/app && cp -r /src/pyproject.toml /src/README.md /src/src /tmp/app \
		 && cd /tmp/app && pip install -q --root-user-action=ignore ".[dev]" \
		 && pip freeze --exclude hacknc' > /tmp/hacknc-freeze.txt
	@{ grep '^#' requirements.lock; \
	   grep -viE '^(coverage|iniconfig|pluggy|pytest|pytest-cov|ruff)==' /tmp/hacknc-freeze.txt; } > requirements.lock.new
	@{ grep '^#' requirements-dev.lock; \
	   grep -iE '^(coverage|iniconfig|pluggy|pytest|pytest-cov|ruff)==' /tmp/hacknc-freeze.txt; } > requirements-dev.lock.new
	@mv requirements.lock.new requirements.lock && mv requirements-dev.lock.new requirements-dev.lock
	@git diff --stat -- requirements.lock requirements-dev.lock 2>/dev/null || true

# Every Python service runs the one image `hacknc-app:dev`, built by the `ui` service. So
# `docker compose up --build <service>` alone reuses a stale image: always go through `build`.
# No build attestations: they're re-stamped on every build, so unchanged code would get a new
# image id and `sync` would needlessly recreate (and log out) everything.
export BUILDX_NO_DEFAULT_ATTESTATIONS := 1
build: env  ## Build the app image from the current code (fast when nothing changed)
	docker compose build ui

sync: build  ## Bring every container in line with the current code (recreates only what's stale)
	# Without --force-recreate, compose recreates exactly the containers whose image or config
	# changed, and starts any that are missing; Keycloak and the rest keep running.
	docker compose up -d --wait

up: build  ## Build and (re)start the whole stack, wait until healthy
	# --force-recreate: containers always pick up edited config files (realm, OPA, Collector...).
	docker compose up -d --wait --force-recreate

S ?=
redeploy: build  ## Rebuild and restart services from the current code (e.g. make redeploy S="ehr-mcp ui")
	@test -n "$(S)" || { echo 'usage: make redeploy S="<service> ..."'; exit 1; }
	docker compose up -d --no-deps --force-recreate --wait $(S)

down:  ## Stop the stack
	docker compose down

reset:  ## Stop the stack and wipe stored traces/logs
	docker compose down -v

ps:  ## Show container status
	docker compose ps

logs:  ## Follow logs of all containers (Ctrl-C to stop)
	docker compose logs -f

test:  ## Run offline unit tests
	$(PY) -m pytest

opa-test:  ## Run the Rego policy unit tests (in the OPA container image)
	docker run --rm -v $(CURDIR)/infra/opa/policies:/policies:ro openpolicyagent/opa:1.21.1 test /policies -v

test-integration:  ## Run tests against the running stack (needs `make up`)
	$(PY) -m pytest -m integration -v

lint:  ## Lint with ruff
	$(VENV)/bin/ruff check src tests

PERSONA ?= dr.morgana
walkthrough:  ## Token walkthrough (default dr.morgana; e.g. make walkthrough PERSONA=nurse.gawain)
	$(PY) -m hacknc.tools.token_walkthrough --user $(PERSONA)

ui-dev: env  ## Run the UI on the Mac with auto-reload (stop the ui container first)
	docker compose stop ui
	$(VENV)/bin/uvicorn --factory hacknc.ui.dev:app --reload --port 8000 --app-dir src

realm:  ## Regenerate the Keycloak realm JSON from src/hacknc/tools/generate_realm.py
	$(PY) -m hacknc.tools.generate_realm

reset-demo: sync  ## Before presenting: fresh audit chain, empty signature queue, checklists and partner logs
	docker compose rm -sf audit-service approval-service
	docker volume rm -f hacknc_audit-data hacknc_approval-data
	docker compose up -d --no-deps --wait audit-service approval-service
	docker compose up -d --no-deps --force-recreate --wait ehr-mcp orchestrator partners

reset-audit: build  ## Start a fresh, empty audit chain (e.g. after the tamper demo)
	docker compose rm -sf audit-service
	docker volume rm -f hacknc_audit-data
	docker compose up -d --no-deps --wait audit-service

tamper-audit:  ## DEMO: edit the audit DB like a rogue admin (ARGS="--seq 3 --field reason --value x")
	docker compose exec audit-service python -m hacknc.audit_service.tamper $(ARGS)

cover-up:  ## FINALE: an insider rewrites the blocked attempt in the audit DB (then reload the report)
	docker compose exec audit-service python -m hacknc.audit_service.tamper --cover-up

curate:  ## Pick ~12 patients from the raw Synthea download into data/curated/
	$(PY) -m hacknc.tools.curate_synthea

admissions:  ## Regenerate the 11 generated admissions in data/overlay/generated/ (deterministic)
	$(PY) -m hacknc.tools.generate_admissions
