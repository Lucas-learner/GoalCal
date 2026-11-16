from datetime import datetime
from zoneinfo import ZoneInfo
from .storage import connect


def consume(user_id):
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    with connect() as con:
        con.execute('INSERT INTO ai_usage VALUES (?, ?, 1) ON CONFLICT(user_id,day) DO UPDATE SET count=count+1', (user_id, day))
