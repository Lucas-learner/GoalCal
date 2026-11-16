from __future__ import annotations

from goalcal import db


def test_init_db_switches_to_a_new_url(tmp_path):
    first = db.init_db(f"sqlite:///{tmp_path / 'a.db'}")
    second = db.init_db(f"sqlite:///{tmp_path / 'b.db'}")

    assert first is not second
    assert str(second.url).endswith("b.db")


def test_init_db_creates_tables(tmp_path):
    from sqlalchemy import inspect

    engine = db.init_db(f"sqlite:///{tmp_path / 'schema.db'}")
    tables = set(inspect(engine).get_table_names())
    assert {"users", "goals", "steps", "events", "checkins", "state_logs", "reschedule_logs", "interactions"} <= tables
