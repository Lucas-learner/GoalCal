# GoalCal

让计划适应你的生活节奏。通过目标澄清、计划草稿、可解释排期、执行签到和滚动续阶，把长期目标变成下一步行动。

正式入口：https://goalcal.zhang.sarl。匿名访问 `/` 展示中英文官网介绍页，产品应用位于 `/app/`，登录用户访问根路径会直接进入应用。当前唯一正式版本为 React Web + Python API；源码在本仓库根目录维护。原 Streamlit/ngrok 部署已经退役。

## 当前能力

- 今天、目标、习惯、日历、设置五个入口；桌面/手机布局，全局助手抽屉，中英切换。
- 目标澄清 → 可编辑草稿 → 保存/安排；阶段进度、滚动补充后续步骤。
- 候选时段、直接指定时间、日历拖拽、冲突检查与撤销。
- 开始执行、完成/部分完成/调整、过期签到与恢复。
- 固定日和每周次数习惯，可在今天和日历安排、完成与撤销当次记录。
- 可视化可用时段/保护时段/偏好、任务间隔、每日任务上限，以及多目标每周平衡排期。
- 全平台日历同步（V2.2）：支持 Apple iCloud / CalDAV、Google Calendar OAuth 原生双向同步（含专属 `GoalCal` 子日历与 Webhook 增量推送）、通用 `.ics` 双向订阅（适配 Apple / Google / Outlook / 飞书等）、来源配色/全天策略/排期避让控制、冲突检测一键重排，以及一次性 `.ics` 导入。
- 主动提醒（阶段 0 & 1）：同步日历携带可配置提前闹钟、`Outcome` 摘要与精准深链；支持早/晚聚焦、灵活习惯周中窗口、目标停滞唤醒与周复盘 SMTP 邮件摘要，并在设置页支持切换 IANA 时区。
- 用户名或邮箱登录；正式环境开放邮箱注册，密码至少 6 个字符，并支持通过绑定邮箱自助找回重置密码。

PWA 实时推送计划见 [TODO](docs/TODO.md)。

## 本地开发

Python 3.12，Node.js 20.19+ 或 22.12+。从仓库根目录运行：

```sh
python3.12 -m venv .venv
make install
make frontend
make dev
```

打开 http://127.0.0.1:8517/ 查看官网介绍页；产品应用位于 http://127.0.0.1:8517/app/，使用预览账号 `preview` / `GoalCal-preview-2026!`。预览数据在 `var/development/preview.db`，默认关闭真实 AI，自动使用规则/模板；不会读取生产环境或生产库。

需要前端热更新时，保留上述后端，再在另一终端运行 `cd frontend && npm run dev`，打开 Vite 输出的 `/app/` 地址。开发代理默认连接 8517，可在 `frontend/.env.local` 设置 `GOALCAL_DEV_BACKEND=http://127.0.0.1:其他开发端口`。

需要真实 AI 时显式执行 `.venv/bin/python tools/web_preview.py --llm-env /绝对路径/开发环境文件`。密钥只保存在被忽略的环境文件中。

不使用预览种子数据时：复制 `.env.example` 为 `.env`，运行 `make run`；另用 `.venv/bin/python -m goalcal.admin create-user <用户名>` 创建本地账号。默认开发库与生产库隔离。

## 测试与构建

```sh
make test             # Python 回归 + 前端单测、类型检查和构建
make e2e              # 自动启动独立临时库，真实浏览器验收
make eval             # 离线规则评测
```

E2E 需要本机 Google Chrome，使用专用 8537 端口。测试不使用生产账号、数据或真实 LLM。`pytest.ini` 限定 `tests/`，不会收集归档目录。

## 部署与维护

完整资料见 [文档索引](docs/README.md)。

[生产手册](docs/production.md) · [试用说明](docs/trial.md) · [系统架构](docs/系统架构图.md) · [接口契约](docs/接口契约.md) · [设计](docs/DESIGN.md) · [整合记录](docs/records/consolidation-2026-11-17.md)

生产使用 `.env.production`、`var/production/goalcal.db`、`local.goalcal-v2.*` 服务和 8521 端口。`make run` 不会自动加载生产配置。发布前先构建 `frontend/dist`，通过 `/_goalcal/health` 检查 Web 就绪状态。

## 代码结构

- `frontend/src/`：React 页面、组件、API 客户端、语言与样式；`frontend/src/intro/` 为独立公开官网，不加载应用 API 或 FullCalendar；`frontend/e2e/` 为浏览器测试。
- `goalcal/auth_proxy.py`：同源认证、静态前端、API 挂载和健康检查。
- `goalcal/web/`：API DTO、请求校验、幂等、事务、执行会话、提议、撤销及助手。
- `goalcal/domain/`、`goalcal/agent.py`、`goalcal/llm/`：复用的业务、排期、规划与语言理解。
- `goalcal/models.py`、`migrations.py`、`storage.py`：SQLite schema 16、迁移和备份。
- `deploy/`：生成可审查的 launchd/Cloudflare 配置；`tools/web_preview.py`：隔离预览。
- `app.py`、`routes/`、`goalcal/ui/`：仅保留的 Streamlit 兼容回归代码，生产不启动、不开放旧路由。
- `var/`：运行数据、日志和归档，不进入 Git；正式源码不得再放在此目录。

AI 只输出受校验的意图与计划；数据修改由业务层执行。日历按用户时区展示，数据库时间统一为 UTC。
