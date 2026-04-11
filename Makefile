.DEFAULT_GOAL := help

.PHONY: help
help: ## Print help
	@grep -E '^[^.]\w+( \w+)*:.*##' $(MAKEFILE_LIST) | \
		sort | \
		awk 'BEGIN {FS = ":.*## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

.PHONY: venv
venv:  ## Sync the uv-managed environment and install hooks
	uv sync --group dev --no-install-project >/dev/null
	uv run pre-commit install

.PHONY: fix
fix: ## Run autofixes across the repository
	uv run pre-commit run --all-files

.PHONY: lint
lint: ## Run lint checks without editing files
	uv run ruff check .

.PHONY: typecheck
typecheck: ## Run mypy against the application entrypoint
	uv run mypy playlist_updates.py

.PHONY: check
check: ## Run non-mutating repository checks
	uv run ruff check .
	uv run mypy playlist_updates.py

.PHONY: update-lock
update-lock: ## Refresh the uv lockfile
	uv lock

.PHONY: update
update: venv  ## Add new videos to Watch Later
	uv run playlist_updates.py update -f

.PHONY: sort
sort: venv  ## Sort videos in 'Sort Watch Later' playlist
	uv run playlist_updates.py sort

.PHONY: test
test: ## Run application tests when a test suite exists
	@:

.PHONY: clean
clean: ## Remove local virtualenv artifacts
	rm -rf .venv venv
