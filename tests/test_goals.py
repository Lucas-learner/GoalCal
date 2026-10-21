from datetime import date

from goalcal.domain import goals as svc
from goalcal.models import User


def _user(session):
    u = User(name="t")
    session.add(u)
    session.flush()
    return u


def test_goal_has_ten_leaf_steps(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Publish my first short video", deadline=date(2026, 11, 20))
    assert len(g.steps) == 10
    assert all(s.est_min <= 90 for s in g.steps)
    assert all(s.milestone for s in g.steps)


def test_progress_is_weighted_by_estimated_minutes(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Learn guitar")
    first = sorted(g.steps, key=lambda s: s.seq)[0]
    svc.complete_step(session, first)
    expected = first.est_min / sum(s.est_min for s in g.steps)
    assert abs(svc.progress(g) - expected) < 1e-6


def test_next_step_skips_done(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Run a half marathon")
    steps = sorted(g.steps, key=lambda s: s.seq)
    svc.complete_step(session, steps[0])
    assert svc.next_step(g).id == steps[1].id


def test_goal_completes(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Finish my side project")
    for s in list(g.steps):
        svc.complete_step(session, s)
    assert g.status == "done"
    assert svc.progress(g) == 1.0
