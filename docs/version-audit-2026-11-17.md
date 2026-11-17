> 本文记录归并前的审计状态。后续实施结果见 [整合记录](consolidation-2026-11-17.md)，当前操作依据为 [生产手册](production.md)。

# GoalCal 新旧版本与落地核查

核查日期：2026-11-17。范围：外层主仓库、独立迭代仓库、本机 launchd 服务与数据库版本、线上公开页面、自动化回归及前端构建。本文为核查报告，不代表已执行代码整合或服务切换。

## 结论

**新版已经上线运行，但尚未整合到外层主仓库；目前存在仓库、运行入口、开发工具和文档四个层面的版本分叉。**

- 线上入口为 <https://goalcal.zhang.sarl>，本次浏览器确认了新版中英双语登录页、邮箱注册、至少 6 位密码；注册页没有邀请码输入框。
- 真正的新版代码位于 `var/iterations/20261117-011010/workspace/`。该目录有独立 `.git`，不是外层仓库的普通已跟踪目录，也不是共享历史的 Git worktree。
- 外层主仓库 `.gitignore:22` 忽略整个 `var/`，因此外层 Git 的提交和推送不会包含新版代码。新版仓库本次检查没有配置 Git remote。
- 外层主仓库仍为 Streamlit 旧版，当前提交 `136701d`（2026-10-27）；新版仓库当前提交 `83ac3a6`（2026-11-17 15:52），工作区干净。
- 新旧应用与隧道都在运行，数据库独立。未发现新域名配置误指向旧版数据库的证据，但不能据此认定旧用户数据已经迁移。

以下以 **V1** 表示外层主仓库，以 **V2** 表示上述独立迭代仓库；文件路径未另行说明时相对于对应仓库。

## 实际部署对照

| 项目 | V1 | V2 |
|---|---|---|
| 工作目录 | `/Users/macmini/projects/GoalCal` | `/Users/macmini/projects/GoalCal/var/iterations/20261117-011010/workspace` |
| 主界面 | Streamlit Chat / Goals / Calendar / Settings | React + Vite：Today / Goals / Calendar / Settings，全局助手抽屉 |
| 后端 | Starlette 登录代理 + Python 领域层 | 同一网关增加 `/api/v1`，复用领域层，保留 Streamlit 兼容入口 |
| 网关 / Streamlit | 8501 / 8502 | 8521 / 8522 |
| 隧道 | ngrok | Cloudflare，`goalcal.zhang.sarl → 127.0.0.1:8521` |
| launchd 前缀 | `local.goalcal` | `local.goalcal-v2` |
| 环境文件 | `.env.production` | `var/v2-production/.env` |
| 数据库 | `var/goalcal-production.db` | `var/v2-production/goalcal-v2.db` |
| 实测 schema | 6 | 8 |
| 首页 / 注册开关 | 旧版流程 | `GOALCAL_ROOT_APP=1`、`GOALCAL_OPEN_REGISTRATION=1` |

两个应用及两个隧道的 launchd 状态均为 running，8501、8502、8521、8522 的 `/_stcore/health` 均为 200。两套 backup 任务均已加载、最近退出码为 0；定时备份任务在非执行时显示 not running 属正常现象。本次未做备份恢复演练。

注意：V2 的 `/_stcore/health` 仍代理到 Streamlit，单独检测此接口不能证明 React 构建或新版 API 完整可用，应增加新版专用健康检查。

## 发现与优先级

### P1：正式新版藏在被忽略的迭代目录，尚无统一代码来源

V2 的 `frontend/` 有 39 个 Git 跟踪文件；相比 V1，Python 包新增 11 个文件、另有 21 个已有文件内容不同，涉及 Web API、国际化、目标澄清、认证、迁移、排期与部署。V1 没有 `goalcal/web/`、`goalcal/i18n.py`、`goalcal/llm/intake.py` 和 `frontend/`。

V2 的历史从独立快照提交 `61399c4` 开始；不能把两边当作普通同源分支直接合并。外层还有用户原有暂存修改、未暂存的 `docs/DESIGN.md` 修改、未跟踪的 iOS demo 与架构文件。整合时需要保留这些内容并逐项对比。

影响：在外层继续开发、提交、打包或从外层远端重新克隆，不能得到当前上线新版。生产服务依赖的目录不能作为“可清理的 var 临时文件”处理。

处理：先保全双方提交与未提交修改，再选定唯一主仓库，把 V2 源码、前端锁文件、测试和文档纳入正式历史；运行数据和凭据继续独立忽略。不要用整体取消 `var/` 忽略的方式解决。

### P1：旧版仍运行，运维命令容易操作错误版本

`~/Library/LaunchAgents/local.goalcal.*.plist` 与 `local.goalcal-v2.*.plist` 同时存在且应用、隧道都已加载。V2 仍使用外层 `.venv/bin/python`，但 WorkingDirectory 指向 V2。因此新版对外层 Python 环境也存在运行依赖。

V1 的 `deploy/generated/` 仍生成 ngrok 服务；两边 `docs/production.md`、`docs/trial.md` 仍引导设置外层 `.env.production` 并操作 `local.goalcal.*`。照旧说明备份、重启、重置密码或查反馈，会作用于旧实例。

V1 的 `migrations.py` 只支持 schema 6，V2 已到 schema 8。不能把旧二进制直接指向新库作为回滚，也不能将旧库覆盖新库以完成迁移。

处理：写明唯一生产服务、目录、环境文件、端口、数据库和备份位置；对旧实例明确选择保留为只读历史入口或在数据核对后下线。停止服务与迁移用户数据属于独立实施步骤，本次未执行。

### P1：主仓库默认测试命令实际混入新版，无法正常收集

V1 执行 `.venv/bin/python -m pytest -q` 得到 **34 个 collection errors**：pytest 递归进入被 Git 忽略的 `var/iterations/.../workspace/tests`，产生同名模块冲突，以及 `No module named 'goalcal.web'`。

依据：两边 `pytest.ini` 都只有 `[pytest]` 和 `pythonpath = .`；V1 `Makefile:12` 与 README 都使用不限定测试目录的命令。Git ignore 不等于 pytest 排除目录。

处理：为 pytest 设置 `testpaths = tests`，必要时排除 `var`、前端依赖目录；正式脚本统一显式测试目录。不要删除线上迭代目录来消除收集错误。

### P1：V2 前端开发代理实际连接 V1

V2 `frontend/vite.config.ts:9`、`:10` 把 `/api` 和 `/_goalcal` 固定代理到 8501；当前 V2 正式端口为 8521，预览工具端口为 8517。

只读实测：

- `8501 GET /api/v1/me` → `200 text/html`（旧 Streamlit 页面）。
- `8521 GET /api/v1/me` → `401 application/json`（新版正确的未登录响应）。

影响：当前机器直接 `npm run dev` 会把 React 页面与旧服务拼在一起，出现 API 解析或登录问题。这是可验证的新旧版本混用；生产静态部署不经过 Vite 开发代理，不受此配置直接影响。

处理：开发代理改为可配置的独立开发后端，配套启动说明与隔离数据库；不要默认连接生产库。

### P2：启动、环境模板和部署脚本尚未完整表达新版

- 两边 `Makefile:9` 仍直接启动 `streamlit run app.py`，没有新版 API/前端构建流程；`.streamlit/config.toml` 的独立默认端口为 8501，和登录网关容易冲突。`make run` 不能作为新版启动方式。
- V2 `goalcal/service.py` 仍同时启动 Streamlit 和网关，这是当前兼容设计；`app.py`、`routes/`、`goalcal/ui/` 不能仅凭“旧技术栈”就删除。V2 的 `goalcal/web/legacy.py` 还提供运行中任务的兼容保护。
- V2 前端入口要求已有 `frontend/dist/index.html`，否则返回 503；README、Makefile、生产手册没有 `npm ci` / `npm run build` 及发布产物步骤。
- 两边 `.env.production.example` 相同，数据库示例指向 V1，缺少 V2 首页、公开注册、网关和上游端口的配置说明。
- 外层真实 `.env` 设置相对数据库路径，却缺少 `GOALCAL_PRODUCTION=false`；默认加载会报 `ValueError: Production requires an absolute SQLite database URL`。本次仅执行配置导入即可复现，未启动或写库。
- V2 `deploy/generate.py` 已增加独立标签、环境文件、端口、输出目录等参数，但手册仍用 V1 命令。`--gateway-port` 配置隧道转发，应用监听端口另取环境变量，文档需要强调两者一致。
- V2 `tools/web_preview.py` 通过 `ROOT.parents[3] / '.env'` 定位外层配置。整合目录后这个假设失效，应改为显式可选配置路径。
- `deploy/setup-owner.command` 沿用旧 `.env.production`、8501 和 12 字符提示，不能直接作为当前公开注册版本的操作入口。

Python 环境 `pip check` 通过，未发现必须立刻升级依赖才能运行的证据；依赖升级与此次仓库归并应分别验证。

### P2：当前文档同时包含过期说明和已完成的 TODO

| 文件 | 当前问题 | 更新方向 |
|---|---|---|
| 两边 `README.md` | 内容相同，仍指 ngrok、Chat 首页、英文 UI、邀请码、旧启动路径 | 以 V2 为准重写入口、技术栈、功能矩阵、运行/构建/测试、目录结构 |
| 两边 `docs/production.md` | 仍说 Cloudflare 留待以后；命令指旧服务与旧库 | 记录当前 Cloudflare、8521/8522、V2 环境和 schema 8；补发版与回滚 |
| 两边 `docs/trial.md` | ngrok 提示页、邀请码、至少 12 位密码 | 对齐线上公开邮箱注册、中英界面、至少 6 位密码与新版反馈路径 |
| 两边 `docs/ngrok.md` | 标题为“当前公网入口” | 标为旧版历史/回退说明，不再作为默认试用入口 |
| 两边 `docs/DESIGN.md` | 仍描述 Streamlit 暖色主题、Chat 740px 等 | 以 V2 `frontend/src/styles/tokens.css` 和当前组件更新；保存 V1 设计的历史定位 |
| 两边 `docs/系统架构图.md` 及 PDF | 仍是 11-03 Streamlit 架构 | 加入 React、REST API、新版数据表、Cloudflare 和兼容层；Markdown 与 PDF 同步再生成 |
| 两边 `docs/接口契约.md` | 主要为旧 Action/领域函数 | 保留领域契约，补 `/api/v1`、DTO、认证、Origin、幂等键、版本冲突、撤销与执行会话 |
| `docs/conversation-redesign.md` | 以旧 Chat/DialogContext 为主要入口 | 区分仍复用的 L1 与新版助手 `web/assistant.py`、`WebThread`、刷新/跨日清理行为 |
| V2 `docs/iteration/PLAN.md`、`CONTRACT.md`、`DESIGN-SPEC.md` | 仍称“不做生产部署”、旧版留 `/`、新版仅可选 `/app/`；引用已删除 `design-preview` 文件 | 标记为迭代当时约束并补当前落地状态；修复失效依据链接 |
| V2 `docs/TODO.md:35` | “新版界面接入试用反馈”仍未勾选 | 已有 Settings 表单、`POST /api/v1/feedback`、前后端测试，应标完成 |
| V2 `docs/TODO.md:34` | 写成待补 `PATCH /me` | 该接口已存在用于语言设置；准确写成待扩展时区字段和 UI |
| `docs/ui-ux-optimization-2026-11-16.md` | 仍以 Streamlit 为现状、iOS demo 为参考 | 标记旧版评估，把剩余需求映射到 V2 组件，避免重新实现已落地部分 |
| `docs/archive/`、开发回顾、`bench/prompt_eval/REPORT_*` | 历史记录本身允许保留旧版本信息 | 保留日期和适用版本，不批量替换历史网址或把旧评测冒充新版结果 |
| `GoalCalendar_IOS_demo/` | 外层未跟踪，V2 跟踪 76 文件；不属于线上 Web 运行链路 | 明确为独立参考/demo，决定长期放置位置；不能把其已实现功能视为 Web 已实现 |

V1 暂存区 `git diff --cached --check` 还报告 `tests/test_natdate.py:230`、`tests/test_refine.py:470`、`tests/test_rules.py:317` 的文件末尾多余空行，属于低优先级清理。

### P2：E2E 尚不能代表最新生产流程

V2 `frontend/e2e/iteration.spec.ts:3` 的登录前置条件硬编码英文 `Username` 标签，登录后却期待中文“今天”；当前登录流程会保存登录页语言，且公开注册模式标签为 `Username or email`。测试环境没有显式固定语言和注册模式。

该套件以预览账号和 8517 为前提，当前 TODO 也明确注册和根路径需要补浏览器测试。应在临时数据库上覆盖新版公开注册、邮箱登录、语言切换、根路径跳转、目标澄清与新版偏好流程。本次没有在生产账号上执行会创建/修改数据的 E2E，不能把单元测试通过表述为完整线上业务验收通过。

## 新版哪些已落地，哪些还不能算完成

源码与本次回归确认已实现：React 页面与助手抽屉、目标澄清和草稿、候选排期/直接拖入日历、撤销、开始执行/签到、滚动补充步骤、时间偏好、中英界面、邮箱注册登录、反馈接口与表单。新版仍保留兼容 Streamlit，这一点本身不是误接旧版。

仍未完整落地或未完成整合：主仓库归并、旧服务生命周期和用户数据迁移决策、统一部署/回滚文档、网页时区修改、SMTP 自助找回、重复事件/长期习惯、通知和双向日历同步。具体产品缺口应以实现和测试为准，不能直接照抄尚未维护的 TODO。

构建验证在 `/private/tmp/goalcal-audit-20261117-dist` 执行，未覆盖正在提供服务的 `frontend/dist`。新构建的 HTML、JS、CSS 共 4 个文件与 V2 部署目录逐文件 SHA-256 完全一致，说明部署目录中的前端产物与当前源码一致。未登录线上受保护页面，因此未直接从公网下载并校验登录后的静态资源。

## 本次验证记录

| 检查 | 结果 |
|---|---|
| 外层默认 `.venv/bin/python -m pytest -q` | 34 个收集错误，新旧测试目录混入 |
| V1 `GOALCAL_ENV_FILE=/dev/null .venv/bin/python -m pytest -q tests` | 660 passed，24.02 秒 |
| V2 `GOALCAL_ENV_FILE=/dev/null <外层 .venv/bin/python> -m pytest -q tests` | 716 passed，30.02 秒 |
| V2 `npm test` | 5 个测试文件、21 tests passed；有 React Router 升级提示，无失败 |
| V2 `tsc --noEmit` | 通过 |
| V2 Vite 构建到临时目录 | 通过；4 个产物与当前部署目录 hash 一致 |
| Python `pip check` | No broken requirements found |
| V1 规则评测，关闭 LLM | 88/88 |
| 线上真实浏览器 | 登录页、注册页、邮箱/密码要求和语言入口确认 |
| 服务与数据库 | 新旧 4 个端口健康；旧库 v6、新库 v8；只读取 schema |

自动化测试使用测试库并关闭真实 LLM；本次未评估真实模型输出质量、外部网络长期稳定性、完整备份恢复或所有登录后功能。外层 `origin/main` 是本地缓存的远端引用，本次未拉取 GitHub，因此不把它当作 GitHub 实时分支状态。

## 建议实施顺序

1. 保全 V1 的暂存/未暂存内容及 V2 独立历史，明确唯一主仓库，完成源代码与文档整合并配置远端备份。
2. 修复测试目录、开发代理、Makefile 和环境模板；统一新版的本地启动、构建与验证流程。
3. 更新 README、生产手册、试用说明、架构与 API 契约；将旧部署和设计标为历史，核对 TODO。
4. 在隔离环境完成最新 E2E 和迁移演练；分别处理应用代码位置、数据库位置及用户数据迁移，记录回滚边界。
5. 切换生产到稳定正式目录，再根据旧数据保留决定处理 V1 和 ngrok；清理前确认 V2 不再依赖迭代目录或外层虚拟环境。

本次仅新增此报告；没有合并仓库、修改服务配置、重启/停止服务、覆盖构建产物或修改生产业务数据。
