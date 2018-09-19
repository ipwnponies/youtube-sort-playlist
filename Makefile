.DEFAULT_GOAL := help

.PHONY: help
help: ## Print help
	@grep -E '^[^.]\w+( \w+)*:.*##' $(MAKEFILE_LIST) | \
		sort | \
		awk 'BEGIN {FS = ":.*## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

.PHONY: venv
venv:  ## Create virtualenv
	bin/venv-update venv= -p python3 venv install= -r requirements-dev.txt -r requirements.txt bootstrap-deps= -r requirements-bootstrap.txt >/dev/null
	venv/bin/pre-commit install

.PHONY: update
update: venv  ## Add new videos to Watch Later
	venv/bin/python playlist_updates.py update -f

.PHONY: sort
sort: venv  ## Sort videos in 'Sort Watch Later' playlist
	venv/bin/python playlist_updates.py sort --dry-run

.PHONY: test
test:
	venv/bin/mypy playlist_updates.py

.PHONY: clean
clean: ## Clean working directory
	find . -iname '*.pyc' | xargs rm -f
	rm -rf ./venv
