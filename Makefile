PY ?= .venv/bin/python

.PHONY: install run dev frontend test test-python test-frontend e2e eval

install:
	$(PY) -m pip install -r requirements.lock
	cd frontend && npm ci

run:
	$(PY) -m goalcal.service app

dev:
	$(PY) tools/web_preview.py

frontend:
	cd frontend && npm run build

test: test-python test-frontend

test-python:
	$(PY) -m pytest -q

test-frontend:
	cd frontend && npm test && npm run build

e2e:
	cd frontend && npm run test:e2e

eval:
	GOALCAL_ENV_FILE=/dev/null GOALCAL_PRODUCTION=false LLM_ENABLED=false $(PY) bench/run_eval.py --parser rules --out bench/results_rules.csv
