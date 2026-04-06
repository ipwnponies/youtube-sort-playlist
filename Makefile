.DEFAULT_GOAL := help

.PHONY: help
help: ## Print help
	@grep -E '^[^.]\w+( \w+)*:.*##' $(MAKEFILE_LIST) | \
		sort | \
		awk 'BEGIN {FS = ":.*## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

.PHONY: venv
venv:  ## Create virtualenv
	uv sync --group dev --no-install-project >/dev/null
	uv run pre-commit install

.PHONY: update
update: venv  ## Add new videos to Watch Later
	uv run playlist_updates.py update -f

.PHONY: sort
sort: venv  ## Sort videos in 'Sort Watch Later' playlist
	uv run playlist_updates.py sort

.PHONY: test
test:
	uv run mypy playlist_updates.py

.PHONY: clean
clean: ## Clean working directory
	find . -iname '*.pyc' | xargs rm -f
	rm -rf ./.venv ./venv
