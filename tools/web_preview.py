"""Run the Web app against a seeded development database (LLM opt-in via --llm-env)."""
import argparse
from pathlib import Path
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

parser = argparse.ArgumentParser()
parser.add_argument('--llm-env', type=Path, help='Explicit opt-in LLM configuration file')
parser.add_argument('--database', type=Path, help='Dedicated preview database; never use production data')
parser.add_argument('--port', type=int, default=8517)
parser.add_argument('--open-registration', action='store_true')
args = parser.parse_args()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
path = (args.database or ROOT / 'var' / 'development' / 'preview.db').expanduser().resolve()
if 'production' in path.parts or path.name in {'goalcal-v2.db', 'goalcal-production.db'}:
    parser.error('Use a dedicated preview database, not a production path')
os.environ.update(
    GOALCAL_DB_URL=f'sqlite:///{path}',
    GOALCAL_PRODUCTION='false',
    GOALCAL_GATEWAY_PORT=str(args.port),
    GOALCAL_UPSTREAM_PORT='8518',
    GOALCAL_ROOT_APP='1',
    GOALCAL_OPEN_REGISTRATION='1' if args.open_registration else '0',
)
if args.llm_env:
    if not args.llm_env.is_file():
        parser.error('LLM configuration file does not exist')
    os.environ['GOALCAL_ENV_FILE'] = str(args.llm_env.resolve())
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
print(f'Preview: http://127.0.0.1:{args.port}/app/ (preview / GoalCal-preview-2026!)', flush=True)
subprocess.run([sys.executable, '-m', 'goalcal.service', 'app'], cwd=ROOT, check=True)
