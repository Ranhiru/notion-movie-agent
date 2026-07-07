# Dev task runner. All tools run through `uv run` so they use the pinned dev group
# (see [dependency-groups] in pyproject.toml). `make check` is what the git pre-push
# hook runs; it does only static checks — no live/credentialed runs.

.PHONY: help format lint typecheck check hooks secrets

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

format: ## Auto-format the code (ruff)
	uv run ruff format

lint: ## Lint, autofixing what is safe (ruff)
	uv run ruff check --fix

typecheck: ## Static type check (basedpyright)
	uv run basedpyright

check: ## CI/pre-push: verify formatting + lint + types (no writes, no creds)
	uv run ruff format --check
	uv run ruff check
	uv run basedpyright

hooks: ## Install the git hooks (points core.hooksPath at .githooks)
	git config core.hooksPath .githooks
	@echo "core.hooksPath -> .githooks  (pre-push now runs 'make check')"

secrets: ## Phase 10: project every .env key into secrets/* for docker compose
	sh scripts/gen-secrets.sh
