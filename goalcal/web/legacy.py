"""Compatibility guards shared with the retained Streamlit entry point."""
from sqlalchemy import select


def require_idle(session, *, event_id=None, step_id=None, goal_id=None):
    from .models import WebExecution
    from ..models import Step
    rows = session.scalars(select(WebExecution).where(WebExecution.status == 'running'))
    for row in rows:
        if row.id == session.info.get('web_execution_checkin'):
            continue
        if (row.event_id == event_id or row.step_id == step_id
                or (goal_id is not None and session.get(Step,row.step_id).goal_id == goal_id)):
            raise ValueError('这一步正在执行，请先在新版“今天”完成签到或选择“需要调整”。')
