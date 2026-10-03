# tripl — convenience commands. Run `make` (or `make help`) for the list.
#
# The backend uses uv (backend/, uv.lock); the frontend uses bun
# (frontend/, bun.lock). Targets cd into the right subproject, so you can
# run everything from the repo root. These wrap the exact commands documented in
# CONTRIBUTING.md — nothing here changes how the tools are invoked, it just puts
# the common flows one keystroke away.

ROOT := $(patsubst %/,%,$(dir $(realpath $(firstword $(MAKEFILE_LIST)))))
BACKEND := $(ROOT)/backend
FRONTEND := $(ROOT)/frontend

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

##@ Setup
.PHONY: install install-be install-fe install-hooks
install: install-be install-fe install-hooks ## Install backend + frontend deps and the git hooks

# --extra dev is required: uv does NOT install optional-dependency extras by
# default, so a bare `uv sync` leaves a fresh clone with no ruff, no mypy, no
# pytest and no pre-commit — none of the tooling the gates below rely on.
install-be: ## Install backend deps incl. dev extras (uv sync --extra dev)
	cd $(BACKEND) && uv sync --extra dev

install-fe: ## Install frontend deps (bun install)
	cd $(FRONTEND) && bun install

# beads owns the hooks: it points core.hooksPath at .beads/hooks, which git then
# uses INSTEAD of .git/hooks. That path is an absolute, machine-specific value in
# .git/config, so a clone (or a moved working copy) can end up pointing at a
# directory that does not exist — at which point NO hook runs at all, beads' own
# sync included. Re-running the installer repairs it.
#
# The ruff format + check step lives in .beads/hooks/pre-commit, outside beads'
# section markers (which beads preserves across upgrades). That file is versioned,
# so this target is the only per-clone step needed to get the hook.
install-hooks: ## Point git at the versioned hooks (beads sync + ruff format on staged backend files)
	bd hooks install --beads

##@ Dev
.PHONY: dev dev-fe
dev: ## Run the full stack via docker compose (watch mode)
	docker compose -f $(ROOT)/compose.dev.yaml up --watch

dev-fe: ## Run just the frontend dev server (Vite :5173)
	cd $(FRONTEND) && bun run dev

##@ API types
.PHONY: sync-types
sync-types: ## Regenerate backend/openapi.json + frontend api.gen.ts from the live schema
	$(ROOT)/bin/sync-api-types.sh

##@ Quality gates
.PHONY: check lint lint-be lint-fe format typecheck typecheck-be typecheck-fe test test-be test-fe build-fe
check: lint typecheck test ## Run every gate (lint + typecheck + tests), CI parity

lint: lint-be lint-fe ## Lint backend + frontend

lint-be: ## Lint backend (ruff check + format --check)
	cd $(BACKEND) && uv run ruff check && uv run ruff format --check

lint-fe: ## Lint frontend (oxlint plus the project rule tests; zero warnings)
	cd $(FRONTEND) && bun run lint

format: ## Auto-format backend (ruff format)
	cd $(BACKEND) && uv run ruff format

typecheck: typecheck-be typecheck-fe ## Type-check backend + frontend

typecheck-be: ## Type-check backend (mypy, strict)
	cd $(BACKEND) && uv run mypy

typecheck-fe: ## Type-check frontend (tsc -b, TypeScript 7)
	cd $(FRONTEND) && bunx tsc -b

test: test-be test-fe ## Run backend + frontend tests

test-be: ## Run backend tests (pytest). Extra args: make test-be ARGS="-k diff -v"
	cd $(BACKEND) && uv run pytest $(ARGS)

test-fe: ## Run frontend tests (vitest run). Extra args: make test-fe ARGS=BranchesTab
	cd $(FRONTEND) && bun run test $(ARGS)

build-fe: ## Production build of the frontend (tsc -b + vite build)
	cd $(FRONTEND) && bun run build

##@ Database
.PHONY: migrate migration
migrate: ## Apply DB migrations (alembic upgrade head)
	cd $(BACKEND) && uv run alembic upgrade head

migration: ## Create a migration: make migration m="add plan diff fields"
	cd $(BACKEND) && uv run alembic revision --autogenerate -m "$(m)"

##@ Docs
.PHONY: openapi-docs
openapi-docs: ## Regenerate the docs-site OpenAPI spec (website/openapi)
	$(ROOT)/bin/dump-openapi.sh
