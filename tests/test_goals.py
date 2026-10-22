from datetime import date, datetime

from sqlalchemy import select

from goalcal.domain import goals as svc
from goalcal.models import Checkin, Event, User


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


def test_create_blank_goal_then_add_steps(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "My own plan", with_template=False)
    assert g.status == "active"
    assert len(g.steps) == 0
    assert svc.progress(g) == 0

    svc.add_step(session, g, "First custom step")
    session.refresh(g)
    assert len(g.steps) == 1
    assert g.template_key == "custom"


def test_add_custom_step(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Learn guitar")
    step = svc.add_step(session, g, "Practice chord changes", est_min=30, energy=0, kind="physical")
    session.refresh(g)

    assert len(g.steps) == 11
    assert step.seq == 11
    assert step.title == "Practice chord changes"
    assert (step.est_min, step.energy, step.kind) == (30, 0, "physical")


def test_add_step_clamps_values(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Learn guitar")
    step = svc.add_step(session, g, "  tiny  ", est_min=1, energy=9, kind="deep")
    assert step.est_min == 5
    assert step.energy == 2


def test_delete_step_removes_checkins(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Learn guitar")
    step = sorted(g.steps, key=lambda s: s.seq)[0]
    svc.complete_step(session, step, raw_text="done once")
    step_id = step.id
    linked = Event(
        user_id=u.id,
        title="Linked external meeting",
        start_utc=step.goal.created_at,
        end_utc=step.goal.created_at,
        kind="external",
        step_id=step.id,
    )
    session.add(linked)
    session.flush()

    svc.delete_step(session, step)
    session.refresh(g)
    session.refresh(linked)

    assert len(g.steps) == 9
    assert session.scalars(select(Checkin).where(Checkin.step_id == step_id)).all() == []
    assert linked.step_id is None


def test_archive_goal_hides_it_from_active_list(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Learn guitar")
    step = sorted(g.steps, key=lambda s: s.seq)[0]
    step.status = "scheduled"

    svc.archive_goal(session, g)
    assert g.status == "archived"
    assert svc.active_goals(session, u.id) == []


def test_replace_steps_rebuilds_plan(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "复习IBA6104", deadline=date(2026, 10, 29))
    old_ids = [step.id for step in g.steps]
    svc.complete_step(session, g.steps[0], raw_text="old progress")

    plan = [
        {
            "seq": 1,
            "milestone": "范围",
            "title": "整理考试范围",
            "outcome": "资料清单",
            "est_min": 25,
            "energy": 0,
            "kind": "admin",
        },
        {
            "seq": 2,
            "milestone": "练习",
            "title": "做一套往年题",
            "outcome": "一套答案",
            "est_min": 60,
            "energy": 2,
            "kind": "deep",
        },
    ]
    svc.replace_steps(session, g, plan)

    assert g.template_key == "llm"
    assert [step.title for step in g.steps] == ["整理考试范围", "做一套往年题"]
    assert session.scalars(select(Checkin).where(Checkin.step_id.in_(old_ids))).all() == []


def test_update_step_edits_fields_and_future_block_title(session):
    u = _user(session)
    g = svc.create_goal(session, u.id, "Learn guitar")
    step = sorted(g.steps, key=lambda s: s.seq)[0]
    block = Event(
        user_id=u.id,
        title=f"{g.title}: {step.title}",
        start_utc=datetime(2026, 10, 22, 1),
        end_utc=datetime(2026, 10, 22, 2),
        kind="block",
        step_id=step.id,
    )
    session.add(block)
    session.flush()

    svc.update_step(
        session,
        step,
        title="Practice scales slowly",
        milestone="Foundations",
        outcome="Play one octave without stopping",
        est_min=30,
        energy=2,
        kind="physical",
    )

    assert step.title == "Practice scales slowly"
    assert step.milestone == "Foundations"
    assert step.outcome == "Play one octave without stopping"
    assert step.est_min == 30
    assert step.energy == 2
    assert step.kind == "physical"
    assert block.title == f"{g.title}: Practice scales slowly"
