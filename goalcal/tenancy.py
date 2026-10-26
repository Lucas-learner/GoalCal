"""Authenticated ORM boundary. Never reuse a Session across users.

All app reads, relationship loads, ORM bulk writes and flushes go through here.
Raw SQL and bulk inserts are intentionally unavailable to application sessions.
Local maintenance uses db.session_scope explicitly, outside this boundary.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from sqlalchemy import event, select, inspect
from sqlalchemy.orm import Session, with_loader_criteria
from . import auth
from .db import get_engine
from .models import (
    User, Goal, Step, Event, Checkin, StateLog, RescheduleLog, Interaction,
    UserPreference, Conversation, Message,
)

current_session = ContextVar('goalcal_session', default=None)


class AccessDenied(ValueError):
    pass


class TenantSession(Session):
    pass


def criteria(uid):
    goals = select(Goal.__table__.c.id).where(Goal.__table__.c.user_id == uid)
    steps = select(Step.__table__.c.id).where(Step.__table__.c.goal_id.in_(goals))
    events = select(Event.__table__.c.id).where(Event.__table__.c.user_id == uid)
    conversations = select(Conversation.__table__.c.id).where(Conversation.__table__.c.user_id == uid)
    return {
        User: User.id == uid, Goal: Goal.user_id == uid,
        Step: Step.goal_id.in_(goals), Event: Event.user_id == uid,
        Checkin: Checkin.step_id.in_(steps), StateLog: StateLog.user_id == uid,
        RescheduleLog: RescheduleLog.event_id.in_(events), Interaction: Interaction.user_id == uid,
        UserPreference: UserPreference.user_id == uid,
        Conversation: Conversation.user_id == uid, Message: Message.conversation_id.in_(conversations),
    }


@event.listens_for(TenantSession, 'do_orm_execute')
def scope_query(state):
    if not state.is_orm_statement or state.is_insert:
        raise AccessDenied('Only scoped ORM operations are permitted')
    if state.is_update:
        # Bulk FK/ownership edits could move rows across tenants. Application
        # bulk updates only clear references or update event titles.
        for column, value in (state.statement._values or {}).items():
            name = getattr(column, 'key', str(column))
            if name == 'user_id' or name == 'id':
                raise AccessDenied('Cannot reassign ownership')
            if name.endswith('_id') and getattr(value, 'value', value) is not None:
                from sqlalchemy.sql.elements import Null
                if not isinstance(value, Null):
                    raise AccessDenied('Use validated object references')
    for model, condition in criteria(state.session.info['principal'].user_id).items():
        state.statement = state.statement.options(with_loader_criteria(model, condition, include_aliases=True))
    if state.is_update or state.is_delete:
        state.session.info['wrote'] = True


def validate_object(session, row):
    uid = session.info['principal'].user_id
    if isinstance(row, User):
        raise AccessDenied('Account changes require the account service')
    if hasattr(row, 'user_id') and row.user_id != uid:
        raise AccessDenied('Record belongs to another account')
    state = inspect(row)
    references = {
        'goal_id': Goal, 'step_id': Step, 'event_id': Event,
        'new_event_id': Event, 'conversation_id': Conversation,
    }
    for field in ('user_id', *references):
        if field in state.attrs and state.persistent and state.attrs[field].history.deleted:
            old = state.attrs[field].history.deleted[0]
            # Validate OLD references as well as new ones (no laundering objects).
            model = references.get(field)
            if model and old is not None and session.get(model, old) is None:
                raise AccessDenied('Invalid original reference')
            if field == 'user_id' and old != uid:
                raise AccessDenied('Cannot reassign ownership')
    for field, model in references.items():
        value = getattr(row, field, None)
        if value is not None and session.get(model, value) is None:
            raise AccessDenied('Record reference is unavailable')


@event.listens_for(TenantSession, 'before_flush')
def validate_flush(session, *_):
    auth.authenticate(session.info['token'])
    with session.no_autoflush:
        for row in session.new | session.dirty | session.deleted:
            validate_object(session, row)
    session.info['wrote'] = True


@contextmanager
def session_scope(token):
    principal = auth.authenticate(token)
    session = TenantSession(get_engine(), expire_on_commit=False,
                            info={'principal': principal, 'token': token})
    ctx = current_session.set(session)
    try:
        yield session
        auth.authenticate(token)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        current_session.reset(ctx)
        session.close()
