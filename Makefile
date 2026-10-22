PY ?= python

.PHONY: install run test eval reset fmt

install:
	$(PY) -m pip install -r requirements.txt

run:
	$(PY) -m streamlit run app.py

test:
	$(PY) -m pytest -q

eval:
	$(PY) bench/run_eval.py --parser rules --out bench/results_rules.csv

reset:
	rm -f goalcal.db
	@echo "local db removed"
