"""Run the iteration against an isolated, seeded local database (no model billing unless --llm)."""
import argparse
from pathlib import Path
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

parser = argparse.ArgumentParser()
parser.add_argument('--llm', action='store_true', help='Load LLM config from main repo .env')
args = parser.parse_args()

ROOT = Path(__file__).resolve().parents[1]
MAIN_ENV = ROOT.parents[3] / '.env'
sys.path.insert(0, str(ROOT))
path = ROOT / 'var' / 'web-preview.db'
os.environ.update(
    GOALCAL_DB_URL=f'sqlite:///{path}',
    GOALCAL_PRODUCTION='false',
    GOALCAL_GATEWAY_PORT='8517',
    GOALCAL_UPSTREAM_PORT='8518',
)
if args.llm and MAIN_ENV.exists():
    os.environ['GOALCAL_ENV_FILE'] = str(MAIN_ENV)
    os.environ.pop('LLM_ENABLED', None)
else:
    os.environ['GOALCAL_ENV_FILE'] = '/dev/null'
    os.environ['LLM_ENABLED'] = 'false'
from goalcal import auth, db
from goalcal.domain import goals
from goalcal.models import Event
from goalcal.web.core import Service

if not path.exists():
    db.init_db()
    uid = auth.create_user('preview', 'GoalCal-preview-2026!', admin=True)
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    with db.session_scope() as session:
        goal = goals.create_goal(session, uid, '发布第一条短视频',
            deadline=(local+timedelta(days=30)).date(), outcome='完成并发布一条有个人表达的短视频',
            step_defs=[{'title':'整理拍摄清单','est_min':45,'milestone':'准备','outcome':'一份可执行的镜头清单'},
                       {'title':'拍摄第一组素材','est_min':60,'milestone':'准备'}],
            outline=['准备','制作','发布'])
        goal.pinned = True
        goals.create_goal(session, uid, '雅思备考', deadline=(local+timedelta(days=90)).date(),
            step_defs=[{'title':'完成一套阅读练习','est_min':60,'milestone':'基础'}], outline=['基础','强化','模拟'])
        goals.create_goal(session, uid, '恢复跑步',
            step_defs=[{'title':'轻松跑步三十分钟','est_min':30,'milestone':'起步','kind':'physical'}], outline=['起步'])
        svc = Service(session, uid, 'Asia/Shanghai', now)
        svc.save_preferences({**svc.preferences(),
            'available_windows':[{'days':list(range(7)),'start':'07:00','end':'23:00'}],
            'protected_windows':[{'days':list(range(7)),'start':'12:00','end':'13:00'}],
            'preferred_windows':[],
            'buffer_minutes':10, 'max_goal_minutes_per_day':240})
        meeting = local.replace(hour=16,minute=0,second=0,microsecond=0)
        session.add(Event(user_id=uid, title='项目沟通', start_utc=meeting.astimezone(timezone.utc).replace(tzinfo=None),
            end_utc=(meeting+timedelta(minutes=45)).astimezone(timezone.utc).replace(tzinfo=None), kind='external',source='seed'))
print('Preview: http://127.0.0.1:8517/app/ (preview / GoalCal-preview-2026!)', flush=True)
subprocess.run([sys.executable, '-m', 'goalcal.service', 'app'], cwd=ROOT, check=True)
