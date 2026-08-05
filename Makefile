# Project Makefile — the canonical "what command runs which step."
#
# Each component has its own README with the unabridged form of these
# commands and the flags you might want to tweak. The targets here are
# the happy path; if you want non-default flags, run the underlying
# command directly.
#
# Usage:
#   make help

.DEFAULT_GOAL := help

PSQL ?= psql
PYTHON ?= python

# Loaded from .env if present so DATABASE_URL etc. are available.
ifneq (,$(wildcard .env))
    include .env
    export
endif

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

.PHONY: install
install: ## Install Python deps for all three components into the active venv.
	pip install -r crawler/requirements.txt
	pip install -r indexer/pipeline/requirements.txt
	pip install -r indexer/service/requirements.txt
	pip install -r search/requirements.txt

.PHONY: install-dev
install-dev: install ## Install the above + dev/test deps.
	pip install -r crawler/requirements-dev.txt
	pip install -r indexer/requirements-dev.txt
	pip install -r search/requirements-dev.txt

.PHONY: migrate
migrate: ## Apply all SQL migrations in order (requires psql on host).
	$(PSQL) "$(DATABASE_URL)" -f sql/0001_initial_schema.sql
	$(PSQL) "$(DATABASE_URL)" -f sql/0002_readme_columns.sql
	$(PSQL) "$(DATABASE_URL)" -f sql/0003_repository_embeddings.sql
	$(PSQL) "$(DATABASE_URL)" -f sql/0004_refresh_watermarks.sql
	$(PSQL) "$(DATABASE_URL)" -f sql/0005_crawl_state.sql
	$(PSQL) "$(DATABASE_URL)" -f sql/0006_repository_guides.sql
	$(PSQL) "$(MIGRATE_REWRITE_DATABASE_URL)" -f sql/0007_search_lanes.sql
	$(PSQL) "$(MIGRATE_REWRITE_DATABASE_URL)" -f sql/0008_search_tsv_light.sql

# 0007/0008 rewrite the repositories table (generated tsvector columns),
# which runs for minutes — that needs a session-mode connection on
# Supabase, same reasoning as HNSW_DATABASE_URL below.
MIGRATE_REWRITE_DATABASE_URL ?= $(subst :6543/,:5432/,$(DATABASE_URL))

.PHONY: migrate-compose
migrate-compose: ## Apply migrations via the postgres container (no host psql needed).
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-gitsearch} < sql/0001_initial_schema.sql
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-gitsearch} < sql/0002_readme_columns.sql
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-gitsearch} < sql/0003_repository_embeddings.sql
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-gitsearch} < sql/0004_refresh_watermarks.sql
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-gitsearch} < sql/0005_crawl_state.sql
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-gitsearch} < sql/0006_repository_guides.sql
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-gitsearch} < sql/0007_search_lanes.sql
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-gitsearch} < sql/0008_search_tsv_light.sql

.PHONY: reset-db
reset-db: ## DESTRUCTIVE: drop every project table, then re-apply migrations. Wipes the corpus.
	@echo "This will DROP ALL DATA in $(DATABASE_URL) and re-create empty tables."
	@echo "Press Ctrl-C within 5s to abort."
	@sleep 5
	$(PSQL) "$(DATABASE_URL)" -c "DROP TABLE IF EXISTS repository_guides, repository_embeddings, crawl_state, refresh_watermarks, repositories CASCADE;"
	$(MAKE) migrate

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

.PHONY: crawl
crawl: ## Run a full metadata crawl (~25 min for ~280K repos). Use once to populate.
	cd crawler && $(PYTHON) -m src.main

.PHONY: crawl-incremental
crawl-incremental: ## Refresh only repos pushed since the last crawl (see ADR 0015).
	cd crawler && $(PYTHON) -m src.main --incremental

.PHONY: readmes
readmes: ## Fetch READMEs for the top-N repos. Resumable. Defaults to top 20K.
	cd crawler && $(PYTHON) -m src.readme_pass --top-n 20000

.PHONY: index
index: ## Embed repos and write to repository_embeddings. Resumable.
	cd indexer && $(PYTHON) -m pipeline.main --top-n 20000

.PHONY: audit
audit: ## Read-only report: what's uploaded vs still pending in each stage.
	cd $(CURDIR) && $(PYTHON) scripts/audit_corpus.py

# --- HNSW build tuning ----------------------------------------------------
# The graph must fit in maintenance_work_mem or the build falls back to a
# disk-merge phase that small hosted instances effectively never finish.
# 768MB holds the full corpus (~244K x 384-dim) and is safe on a 2GB
# instance; on a 1GB instance pass HNSW_MAINTENANCE_WORK_MEM=512MB and
# expect a much slower build (or temporarily bump the instance size).
HNSW_MAINTENANCE_WORK_MEM ?= 768MB
# Index DDL runs for minutes and relies on session-scoped SETs, so it needs
# a session-mode connection. On Supabase that's pooler port 5432 — the
# transaction pooler on 6543 (what DATABASE_URL normally uses) drops SETs
# between statements and enforces a statement timeout that kills the build.
# The subst is a no-op when DATABASE_URL isn't on 6543 (e.g. local compose).
HNSW_DATABASE_URL ?= $(subst :6543/,:5432/,$(DATABASE_URL))

.PHONY: build-hnsw
build-hnsw: ## (Re)build the HNSW index after bulk embedding. Search stays up but slow (unindexed) while it runs.
	# A cancelled/failed CONCURRENTLY build leaves an INVALID index that
	# IF NOT EXISTS would silently keep, so always drop and build fresh.
	# Serial build (parallel workers need shared memory hosted instances
	# don't provide, and the serial in-memory build is already fast).
	$(PSQL) "$(HNSW_DATABASE_URL)" -v ON_ERROR_STOP=1 \
		-c "SET statement_timeout = 0;" \
		-c "SET maintenance_work_mem = '$(HNSW_MAINTENANCE_WORK_MEM)';" \
		-c "SET max_parallel_maintenance_workers = 0;" \
		-c "DROP INDEX CONCURRENTLY IF EXISTS idx_repository_embeddings_hnsw;" \
		-c "CREATE INDEX CONCURRENTLY idx_repository_embeddings_hnsw \
	        ON repository_embeddings \
	        USING hnsw (embedding vector_cosine_ops) \
	        WITH (m = 16, ef_construction = 64);"

.PHONY: build-hnsw-halfvec
build-hnsw-halfvec: ## Build the half-precision HNSW expression index the search service queries (ADR 0018). Same recipe as build-hnsw.
	# Embeddings are L2-normalised (the embedding service passes
	# normalize_embeddings=True), so inner product ranks identically to
	# cosine and halfvec_ip_ops is the cheaper operator. The index is on
	# an expression, so queries must ORDER BY
	# (embedding::halfvec(384)) <#> $$q::halfvec(384) to use it — which
	# is exactly what search/service/db.py does.
	$(PSQL) "$(HNSW_DATABASE_URL)" -v ON_ERROR_STOP=1 \
		-c "SET statement_timeout = 0;" \
		-c "SET maintenance_work_mem = '$(HNSW_MAINTENANCE_WORK_MEM)';" \
		-c "SET max_parallel_maintenance_workers = 0;" \
		-c "DROP INDEX CONCURRENTLY IF EXISTS idx_repository_embeddings_hnsw_halfvec;" \
		-c "CREATE INDEX CONCURRENTLY idx_repository_embeddings_hnsw_halfvec \
	        ON repository_embeddings \
	        USING hnsw ((embedding::halfvec(384)) halfvec_ip_ops) \
	        WITH (m = 16, ef_construction = 64);"

.PHONY: drop-hnsw-fp32
drop-hnsw-fp32: ## Drop the fp32 HNSW index once halfvec recall parity is verified live. Guarded: run with CONFIRM=yes.
	@if [ "$(CONFIRM)" != "yes" ]; then \
		echo "This drops idx_repository_embeddings_hnsw (the fp32 HNSW index)."; \
		echo "Only do this after the halfvec-backed service is live and verified."; \
		echo "Re-run with: make drop-hnsw-fp32 CONFIRM=yes   (rebuild: make build-hnsw)"; \
		exit 1; \
	fi
	$(PSQL) "$(HNSW_DATABASE_URL)" -v ON_ERROR_STOP=1 \
		-c "SET statement_timeout = 0;" \
		-c "DROP INDEX CONCURRENTLY IF EXISTS idx_repository_embeddings_hnsw;"

# ---------------------------------------------------------------------------
# Long-lived services (when running from the host, not docker compose)
# ---------------------------------------------------------------------------

.PHONY: serve-embed
serve-embed: ## Run the embedding service on :8001.
	cd indexer && uvicorn service.server:app --host 0.0.0.0 --port 8001

.PHONY: serve-search
serve-search: ## Run the search service on :8002.
	cd search && uvicorn service.server:app --host 0.0.0.0 --port 8002

# ---------------------------------------------------------------------------
# Tests / eval
# ---------------------------------------------------------------------------

.PHONY: test
test: ## Run unit tests for all three components.
	cd crawler && pytest
	cd indexer && pytest
	cd search && pytest

.PHONY: eval
eval: ## Run the search eval harness against a running search service.
	cd search && $(PYTHON) -m eval.run

# ---------------------------------------------------------------------------
# Compose convenience
# ---------------------------------------------------------------------------

.PHONY: up
up: ## Start postgres + embedding + search via docker compose.
	docker compose up -d

.PHONY: down
down: ## Stop the compose stack.
	docker compose down

.PHONY: logs
logs: ## Tail logs from all services.
	docker compose logs -f
