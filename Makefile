# tripl — convenience commands. Run `make` (or `make help`) for the list.
#
# The Python packages use uv (backend/, cli/ and mcp-server/, each with its own
# uv.lock); the frontend uses bun (frontend/, bun.lock). Targets cd into the
# right subproject, so you can run everything from the repo root. These wrap
# the commands documented in CONTRIBUTING.md and run by
# .github/workflows/ci.yml — nothing here changes how the tools are invoked, it
# just puts the common flows one keystroke away.

ROOT := $(patsubst %/,%,$(dir $(realpath $(firstword $(MAKEFILE_LIST)))))
BACKEND := $(ROOT)/backend
FRONTEND := $(ROOT)/frontend
CLI := $(ROOT)/cli
MCP := $(ROOT)/mcp-server
# How CI runs the CLI's and the MCP server's tools: the dev dependency group,
# with --locked failing when uv.lock no longer matches pyproject.toml.
UV_DEV := uv run --locked --group dev

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

##@ Setup
.PHONY: install install-be install-fe install-cli install-mcp install-hooks
install: install-be install-fe install-cli install-mcp install-hooks ## Install every package's deps and the git hooks

# --extra dev is required: uv does NOT install optional-dependency extras by
# default, so a bare `uv sync` leaves a fresh clone with no ruff, no mypy, no
# pytest and no pre-commit — none of the tooling the gates below rely on.
install-be: ## Install backend deps incl. dev extras (uv sync --extra dev)
	cd $(BACKEND) && uv sync --extra dev

install-fe: ## Install frontend deps (bun install)
	cd $(FRONTEND) && bun install

install-cli: ## Install the CLI's deps incl. the dev group
	cd $(CLI) && uv sync --locked --group dev

install-mcp: ## Install the MCP server's deps incl. the dev group
	cd $(MCP) && uv sync --locked --group dev

# The hooks are versioned in .beads/hooks: pre-commit carries this repository's
# ruff gate (format, then check, on the staged files), outside beads' section
# markers (which beads preserves across upgrades), so pointing core.hooksPath
# there is the only per-clone step.
# The path is relative on purpose: git resolves it from the top of the working
# tree, so a moved or re-cloned checkout keeps its hooks, which the absolute
# path `bd hooks install` writes does not. Beads is the maintainers' issue
# tracker and optional: the hooks run its steps only where `bd` is installed.
install-hooks: ## Point git at the versioned hooks in .beads/hooks (the pre-commit ruff gate)
	git -C $(ROOT) config core.hooksPath .beads/hooks

##@ Dev
.PHONY: dev dev-fe
dev: ## Run the full stack via docker compose (watch mode)
	docker compose -f $(ROOT)/compose.dev.yaml up --watch

dev-fe: ## Run just the frontend dev server (Vite :5173)
	cd $(FRONTEND) && bun run dev

##@ API types
.PHONY: sync-types
# One step for every copy of the API schema: backend/openapi.json, the docs
# site's website/openapi/tripl.openapi.json and frontend/src/types/api.gen.ts.
sync-types: ## Regenerate backend/openapi.json, the docs-site spec and api.gen.ts from the live schema
	$(ROOT)/bin/sync-api-types.sh

##@ Quality gates
.PHONY: check lint lint-be lint-fe lint-cli lint-mcp format \
	typecheck typecheck-be typecheck-fe typecheck-cli typecheck-mcp \
	test test-be test-fe test-cli test-mcp test-scripts build-fe check-api-types \
	check-bundle

# The CI gates that need no services: every package's lint, type check and
# tests, the API types check and the bundle budget. CI runs more than this:
# the warehouse conformance, search relevance, Postgres concurrency and
# migration round-trip jobs need real databases, e2e needs the dev stack, and
# the image builds need Docker.
check: lint typecheck test check-api-types check-bundle ## Run the local CI gates (lint, types, tests, API types, bundle budget)

lint: lint-be lint-fe lint-cli lint-mcp ## Lint every package

lint-be: ## Lint backend (ruff check + format --check)
	cd $(BACKEND) && uv run ruff check && uv run ruff format --check

lint-fe: ## Lint frontend (oxlint plus the project rule tests; zero warnings)
	cd $(FRONTEND) && bun run lint

lint-cli: ## Lint the CLI (ruff check + format --check)
	cd $(CLI) && $(UV_DEV) ruff check && $(UV_DEV) ruff format --check

lint-mcp: ## Lint the MCP server (ruff check + format --check)
	cd $(MCP) && $(UV_DEV) ruff check && $(UV_DEV) ruff format --check

format: ## Auto-format the Python packages (ruff format)
	cd $(BACKEND) && uv run ruff format
	cd $(CLI) && $(UV_DEV) ruff format
	cd $(MCP) && $(UV_DEV) ruff format

typecheck: typecheck-be typecheck-fe typecheck-cli typecheck-mcp ## Type-check every package

typecheck-be: ## Type-check backend (mypy, strict)
	cd $(BACKEND) && uv run mypy

typecheck-fe: ## Type-check frontend (tsc -b, TypeScript 7)
	cd $(FRONTEND) && bunx tsc -b

typecheck-cli: ## Type-check the CLI (mypy, strict)
	cd $(CLI) && $(UV_DEV) mypy src

typecheck-mcp: ## Type-check the MCP server (mypy)
	cd $(MCP) && $(UV_DEV) mypy src

test: test-be test-fe test-cli test-mcp test-scripts ## Run every package's tests and the bin/ scripts' tests

test-be: ## Run backend tests (pytest). Extra args: make test-be ARGS="-k diff -v"
	cd $(BACKEND) && uv run pytest $(ARGS)

test-fe: ## Run frontend tests (vitest run). Extra args: make test-fe ARGS=BranchesTab
	cd $(FRONTEND) && bun run test $(ARGS)

test-cli: ## Run the CLI's tests (pytest). Extra args: make test-cli ARGS="-k install"
	cd $(CLI) && $(UV_DEV) pytest $(ARGS)

test-mcp: ## Run the MCP server's tests (pytest). Extra args as for test-cli
	cd $(MCP) && $(UV_DEV) pytest $(ARGS)

# The release script on throwaway repositories, and the checks CI runs after
# its gates (bin/check-junit.py) and on the API reference
# (bin/check-openapi-docs.py).
test-scripts: ## Test bin/release.sh and CI's check scripts
	$(ROOT)/bin/test-release.sh
	$(ROOT)/bin/test-ci-checks.sh

build-fe: ## Production build of the frontend (tsc -b + vite build)
	cd $(FRONTEND) && bun run build

# backend/openapi.json is pinned by the backend tests; these are CI's other
# two checks: the docs site's copy matches it, and
# frontend/src/types/api.gen.ts was regenerated from it. A failure of the
# second leaves the regenerated file in place: review and commit it.
check-api-types: ## Check the docs-site spec and frontend api.gen.ts match backend/openapi.json
	python3 $(ROOT)/bin/check-openapi-docs.py
	cd $(FRONTEND) && bun run gen:api && git diff --exit-code -- src/types/api.gen.ts

# Reads the built dist/, so it builds first. The budgets live in
# frontend/scripts/check-bundle-budget.mjs.
check-bundle: build-fe ## Check the first-load JavaScript stays within its budget
	cd $(FRONTEND) && bun run check:bundle

##@ Database
.PHONY: migrate migration
migrate: ## Apply DB migrations (alembic upgrade head)
	cd $(BACKEND) && uv run alembic upgrade head

migration: ## Create a migration: make migration m="add plan diff fields"
	cd $(BACKEND) && uv run alembic revision --autogenerate -m "$(m)"
