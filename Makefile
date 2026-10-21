PY ?= python
VENV ?= /Users/lucas/anaconda3/envs/goalcal/bin/python

.PHONY: install run test eval reset fmt

install:
	$(VENV) -m pip install -r requirements.txt

run:
	$(VENV) -m streamlit run app.py

test:
	$(VENV) -m pytest -q

eval:
	$(VENV) bench/run_eval.py --parser rules --out bench/results_rules.csv

reset:
	rm -f goalcal.db
	@echo "local db removed"
