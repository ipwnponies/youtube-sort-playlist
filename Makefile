venv: requirements.txt requirements-dev.txt
	bin/venv-update venv= -p python3 venv install= -r requirements-dev.txt
	venv/bin/pre-commit autoupdate
	venv/bin/pre-commit install

.PHONY: run
run: venv
	venv/bin/python playlist_updates.py

.PHONY: clean
clean:
	find . -iname '*.pyc' | xargs rm -f
	rm -rf ./venv
