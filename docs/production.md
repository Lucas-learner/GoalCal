# GoalCal Web 生产维护手册

更新：2026-11-17。正式入口 https://goalcal.zhang.sarl，唯一代码目录 `/Users/macmini/projects/GoalCal`。旧版 ngrok 和 `local.goalcal.*` 已退役；历史文档不再作为操作手册。

## 实例与路径

| 项目 | 当前配置 |
|---|---|
| 网关 | `127.0.0.1:8521`，Starlette/Uvicorn，同进程 API + React 静态文件 |
| 公网 | Cloudflare Tunnel，指定域名转发 8521；其他域名 404 |
| 环境文件 | 仓库根目录 `.env.production`，权限 600 |
| 数据库 | `var/production/goalcal.db`，schema 8，SQLite WAL |
| 日志 / 备份 | `var/production/logs/` / `var/production/backups/` |
| 静态构建 | `frontend/dist/`，由锁定依赖构建，不提交 Git |
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

1. 确认提交、`make test`、`make e2e` 通过；避免在运行中的生产目录直接进行未经验证的编辑。
2. 记录当前提交和环境配置，在线备份数据库，保留旧构建；构建到独立目录并检查后再替换产物。
3. 停止应用 `launchctl bootout gui/$(id -u)/local.goalcal-v2.app`，安装已验证源码/构建；需要 schema 更新时执行迁移并保留 pre-migration 备份。
4. 使用 `launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/local.goalcal-v2.app.plist"` 启动；检查健康、公开登录页及核心业务。只改应用无需重启隧道。
5. 同 schema 回滚使用对应提交/构建并保留当前库；schema 升级后的回滚需要兼容程序或停服恢复对应备份。V1 schema 6 程序不能读取当前 v8 库。

恢复命令需要停服：`.venv/bin/python -m goalcal.admin restore /绝对路径/备份.db --app-stopped`。恢复会撤销登录会话；备份之后的数据需另行恢复，不能声称回滚无损保留所有新写入。

本次归并的快照、旧数据与旧配置位置见 [整合记录](consolidation-2026-11-17.md)。旧数据只归档，不合并进正式新版库。
