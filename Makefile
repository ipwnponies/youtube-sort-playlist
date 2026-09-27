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

.PHONY: test
test: ## Run unit tests
	uv run python -m unittest discover -s tests -t .

.PHONY: check
check: lint typecheck test ## Run non-mutating repository checks

.PHONY: update-lock
update-lock: ## Refresh the uv lockfile
	uv lock

.PHONY: update
update: ## Add new videos to Watch Later
	uv run --locked playlist_updates.py update --auto-batch

.PHONY: sort
sort: ## Sort videos in 'Sort Watch Later' playlist
	uv run --locked playlist_updates.py sort

.PHONY: clean
clean: ## Remove local virtualenv artifacts
	rm -rf .venv venv
