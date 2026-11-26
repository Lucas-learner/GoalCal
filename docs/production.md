# GoalCal Web 生产维护手册

更新：2026-11-26。正式入口 https://goalcal.zhang.sarl。源码工作区为 `/Users/macmini/projects/GoalCal`，应用与备份任务从已验证的发布目录启动。旧版 ngrok 和 `local.goalcal.*` 已退役。

## 实例与路径

| 项目 | 当前配置 |
|---|---|
| 网关 | `127.0.0.1:8521`，Starlette/Uvicorn，同进程 API + React 静态文件 |
| 公网 | Cloudflare Tunnel，指定域名转发 8521；其他域名 404 |
| 环境文件 | 仓库根目录 `.env.production`，权限 600 |
| 数据库 | `var/production/goalcal.db`，schema 16，SQLite WAL |
| 日志 / 备份 | `var/production/logs/` / `var/production/backups/` |
| 当前发布 | `var/releases/brand-logo-footer-fix-v16-20261126/`，包含源码、`frontend/dist/` 和 `SHA256SUMS`（同 schema 上一版本：`var/releases/brand-logo-v16-20261126/`；`v15` 跨 schema 回滚基准：`var/releases/remove-missed-btn-v15-20261125/`，对应切换前备份路径记录在 `var/releases/weekly-plan-v16-20261125/CUTOVER_BACKUP`） |
| 后台服务 | `local.goalcal-v2.app`、`.tunnel`、`.backup` |
| 健康检查 | `http://127.0.0.1:8521/_goalcal/health` |

生产只启动 Web 网关，不再启动 Streamlit。`/_stcore/health` 已不适用。新健康接口同时检查数据库 schema 和前端 HTML 存在，故障返回 503；仍需实际页面和 API 验证。

## 环境与首次部署

```sh
cd /Users/macmini/projects/GoalCal
python3.12 -m venv .venv
make install
make frontend
cp .env.production.example .env.production  # 仅首次配置；已有生产环境不要覆盖
chmod 600 .env.production
export GOALCAL_ENV_FILE="$PWD/.env.production"
.venv/bin/python -m goalcal.admin migrate
```

模板中的数据库路径必须改为本机绝对路径。`GOALCAL_ROOT_APP=1` 使首页进入新版，`GOALCAL_OPEN_REGISTRATION=1` 开放邮箱注册。关闭公开注册后，使用邀请码或本机管理员创建账号。`GOALCAL_LEGACY_UI=0` 禁止旧路由；正常 service 命令强制关闭兼容 UI。AI 开关、模型与密钥显式配置；不输出或提交真实环境文件。

当前已有生产数据，日常升级不重新创建数据库，不从旧 v6 库覆盖新库。

## Cloudflare 与 launchd

Cloudflare Tunnel 的凭据留在主机原凭据目录，不进入 Git。以下命令只生成配置，不安装服务：

```sh
.venv/bin/python deploy/generate.py \
  --domain goalcal.zhang.sarl --tunnel-id YOUR-TUNNEL-UUID \
  --credentials "$HOME/.cloudflared/YOUR-TUNNEL-UUID.json" \
  --gateway-port 8521 --label-prefix local.goalcal-v2 \
  --env-file "$PWD/.env.production" --output-dir "$PWD/deploy/generated" \
  --python "$PWD/.venv/bin/python" --protocol http2
cloudflared tunnel --config "$PWD/deploy/generated/cloudflared.yml" ingress validate
```

`--gateway-port` 必须与环境文件 `GOALCAL_GATEWAY_PORT` 一致。检查生成的绝对路径后，把三个 plist 安装到 `~/Library/LaunchAgents/`；首次加载使用 `launchctl bootstrap gui/$(id -u) <plist绝对路径>`。服务名称保留 `local.goalcal-v2`，防止与已退役的 V1 标签混淆。

应用/隧道异常自动重启；备份登录加载时和每天 03:15 运行，保留 14 天 daily 备份。用户退出 macOS、断电、断网仍会影响可用性。隧道只绑定网关的 loopback 端口。

Cloudflare 隧道监督器每 30 秒读取本机 `127.0.0.1:20241/metrics` 的可用连接数（显式固定 metrics 端口，仅 loopback，不经过系统代理）。连续 180 秒没有可用连接，或探测接口持续失败时，先停止隧道子进程，再退出交由 launchd KeepAlive 重启。首次探测在启动 30 秒后执行，因此冷启动最早约 210 秒才会触发重启；每次重启都有完整宽限期，避免网络未恢复时频繁重启。任一次连接恢复即清零故障计时；部分连接断开但仍有可用连接时不重启。

维护事件写入滚动日志 `var/production/logs/application.log`，搜索 `tunnel_watchdog_` 可查看启动、断线、恢复和自动重启记录。隧道原始输出仍在 `tunnel.log`。此监测处理本机隧道断线，不能在主机关机、退出登录或网络完全中断时保持公网服务，也不替代外部可用性监控。更新监督器代码后执行 `launchctl kickstart -k "gui/$(id -u)/local.goalcal-v2.tunnel"` 即可启用，无需修改 plist 或重启应用。

## 日常命令

```sh
export GOALCAL_ENV_FILE="$PWD/.env.production"
.venv/bin/python -m goalcal.admin backup
.venv/bin/python -m goalcal.admin diagnostics --days 7
.venv/bin/python -m goalcal.admin feedback
.venv/bin/python -m goalcal.admin feedback --id 123
.venv/bin/python -m goalcal.admin feedback --resolve 123
.venv/bin/python -m goalcal.admin reset-password 用户名或邮箱
launchctl print "gui/$(id -u)/local.goalcal-v2.app"
curl -fsS http://127.0.0.1:8521/_goalcal/health
```

不要使用旧 `.env.production` 路径或 `local.goalcal.app` 标签。管理员密码通过终端隐藏输入；会话使用 HttpOnly 同源 Cookie，公开 HTTPS 请求设置 Secure。来源校验和幂等键继续启用。

## 发布与回滚

1. 根据应用 LaunchAgent 的 `WorkingDirectory` 核对实际运行版本。盘点线上和工作区已经可用的功能，将发布范围与线上版本比较，再构建独立目录并在其中运行 `make test`、`make e2e`。
2. 记录基础提交与产物校验值，在线备份数据库，保留上个版本的源码和构建。涉及 schema 升级时先在备份副本演练。发布旧版本前核对代码与正式库的 schema，避免通过旧代码触发误迁移。
3. 停止应用与定时备份任务，保存最终切换备份。完成迁移后，将应用与备份 LaunchAgent 的 `WorkingDirectory` 都指向新发布目录。
4. 重新加载原标签，检查本机及公网健康、登录页和相关 API。生产环境文件与 Cloudflare 隧道沿用现有配置。
5. 同 schema 回滚使用相应发布目录并保留当前库。跨 schema 回滚需停服并恢复相应备份或使用兼容程序。

恢复命令需要停服：`.venv/bin/python -m goalcal.admin restore /绝对路径/备份.db --app-stopped`。恢复会撤销登录会话；备份之后的数据需另行恢复，不能声称回滚无损保留所有新写入。

当前线上版本为 **Schema `v16`（全新「地平线 G」品牌 Logo、官网页脚品牌色一致修复、全站自适应 SVG/ICO/Apple Touch Icon 图标、官网/应用/登录页统一矢量标识 + 中英文公开官网介绍页 `/` + 多目标全局每周平衡排期 Phase 1）**（发布于 `var/releases/brand-logo-footer-fix-v16-20261126/`；同 schema 上一版 `var/releases/brand-logo-v16-20261126/`）。若需同 Schema `v16` 回滚，可直接切回 `var/releases/brand-logo-v16-20261126/` 并保留当前数据库；若需跨 Schema 回滚至 `v15`，须先停服并使用 `var/releases/weekly-plan-v16-20261125/CUTOVER_BACKUP` 恢复数据库，再切回 `var/releases/remove-missed-btn-v15-20261125/`。若 `GOALCAL_CREDENTIAL_KEY` 未显式设置，生产环境使用数据库同目录 `0600` 权限 `.credential_key`。

本次归并的快照、旧数据与旧配置位置见 [整合记录](records/consolidation-2026-11-17.md)。旧数据只归档，不合并进正式新版库。
