from __future__ import annotations

from sqlalchemy import func, select

from goalcal.models import Event, Goal, Step
from goalcal.seed import seed_if_empty


def test_seed_creates_demo_data(session):
    user = seed_if_empty(session, "Asia/Shanghai")

    assert user.name == "demo"
    assert session.scalar(select(func.count()).select_from(Event)) == 7
    goal = session.scalar(select(Goal))
    assert goal is not None and goal.title == "Publish my first short video"
    steps = list(session.scalars(select(Step).order_by(Step.seq)))
    assert len(steps) == 10
    assert [s.status for s in steps[:2]] == ["done", "done"]


def test_seed_is_idempotent(session):
    first = seed_if_empty(session, "Asia/Shanghai")
    counts = (
        session.scalar(select(func.count()).select_from(Event)),
        session.scalar(select(func.count()).select_from(Goal)),
        session.scalar(select(func.count()).select_from(Step)),
    )

    second = seed_if_empty(session, "Asia/Shanghai")
    assert second.id == first.id
    assert counts == (
        session.scalar(select(func.count()).select_from(Event)),
        session.scalar(select(func.count()).select_from(Goal)),
        session.scalar(select(func.count()).select_from(Step)),
    )
