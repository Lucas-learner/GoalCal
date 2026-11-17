# Streamlit compatibility regression code

This package, root `app.py`, `routes/`, and `.streamlit/` are retained for legacy regression coverage. Production uses `frontend/` and `goalcal/web/`; the default service does not start Streamlit or expose its routes.

Explicit local compatibility runs may use `python -m goalcal.service app --legacy-ui` with `GOALCAL_ROOT_APP=0` and an isolated database. Never enable this on the current production instance.
