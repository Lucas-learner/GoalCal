"""Explicit user feedback stays in the scoped business DB, never runtime logs."""
import json
import re
from sqlalchemy import select
from .models import Interaction
from . import metrics, telemetry
from .timeutil import now_utc

CATEGORIES = ('功能问题', 'AI 理解或计划问题', '速度或连接问题', '建议或其他')


def submit(session, category, message, operation_id=None):
    principal = session.info.get('principal')
    if principal is None:
        raise ValueError('Authenticated feedback session required')
    message = message.strip()
    if category not in CATEGORIES or not 5 <= len(message) <= 2000:
        raise ValueError('Choose a category and enter 5–2000 characters of feedback')
    if operation_id and not re.fullmatch(r'[a-f0-9]{16}', operation_id):
        raise ValueError('Invalid diagnostic reference')
    row = metrics.record(session, principal.user_id, 'user_feedback', now_utc(),
                         payload={'category': category, 'message': message,
                                  'operation_id': operation_id, 'status': 'open'})
    telemetry.emit('feedback_submitted', user_id=principal.user_id,
                   feedback_id=row.id, category=category, operation_id=operation_id)
    return row.id


def list_feedback(session, *, limit=30, feedback_id=None):
    query = select(Interaction).where(Interaction.kind == 'user_feedback')
    if feedback_id is not None:
        query = query.where(Interaction.id == feedback_id)
    rows = session.scalars(query.order_by(Interaction.id.desc()).limit(min(max(limit, 1), 100)))
    return [{'id': row.id, 'user_id': row.user_id, 'time': row.ts_wall.isoformat(),
             **json.loads(row.payload)} for row in rows]


def resolve(session, feedback_id):
    row = session.scalar(select(Interaction).where(Interaction.id == feedback_id,
                                                 Interaction.kind == 'user_feedback'))
    if row is None:
        raise ValueError('Feedback not found')
    payload = json.loads(row.payload)
    payload['status'] = 'resolved'
    payload['resolved_at'] = now_utc().isoformat()
    row.payload = json.dumps(payload, ensure_ascii=False)
    session.flush()
    telemetry.emit('feedback_resolved', feedback_id=row.id, user_id=row.user_id)
