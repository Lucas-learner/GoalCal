from datetime import datetime
from zoneinfo import ZoneInfo
from .storage import connect
from .config import settings


def consume(user_id):
    if settings.ai_daily_limit < 1:
        raise RuntimeError("AI requests disabled")
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    with connect() as con:
        cursor = con.execute('INSERT INTO ai_usage VALUES (?, ?, 1) ON CONFLICT(user_id,day) DO UPDATE SET count=count+1 WHERE count < ? RETURNING count', (user_id, day, settings.ai_daily_limit))
        if cursor.fetchone() is None:
            raise RuntimeError('Daily AI request limit reached')
