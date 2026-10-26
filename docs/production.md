# 本机公网多用户运行手册

> 当前公网试用采用 ngrok 免费固定网址，详见 [ngrok 入口和维护说明](ngrok.md)。本文的 Cloudflare 域名发布流程留待后续。

应用和数据留在这台 Mac。当前试用由浏览器经 HTTPS → ngrok → 127.0.0.1:8501 访问；Cloudflare Tunnel 是下文保留的后续部署方案。首版面向 5–20 人邀请试用。必须完成最后的外网验收后才能视为部署完成。

## 安装与首次账号

需要 Python 3.12、接入 Cloudflare 的域名、Cloudflare 账号及 cloudflared。以下命令在项目目录运行；虚拟环境和配置必须使用绝对路径。不要将开发数据库配置复制到生产环境。

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
cp .env.production.example .env.production
chmod 600 .env.production
export GOALCAL_ENV_FILE="$PWD/.env.production"
```

编辑 `.env.production` 的绝对数据库路径及 AI 设置。共享 AI 密钥仅留在此文件；默认关闭 AI。数据库默认在 `var/goalcal-production.db`。本机已有的 `.env` 不会在设置了 `GOALCAL_ENV_FILE` 后加载。生产禁止相对数据库路径。

```sh
.venv/bin/python -m goalcal.admin migrate
.venv/bin/python -m goalcal.admin create-user owner --tz Asia/Shanghai
.venv/bin/python -m goalcal.admin invite
.venv/bin/python -m goalcal.service app
```

密码由终端隐藏输入，无默认密码；用户名 3–32 个字母、数字或下划线，密码 12–256 字符。邀请码仅打印一次，数据库只保存 SHA-256 摘要，默认 7 天、一次有效。`invite --days 3` 可更改有效期。不要把邀请码输出写入日志。

## 账号管理

```sh
.venv/bin/python -m goalcal.admin disable alice
.venv/bin/python -m goalcal.admin enable alice
.venv/bin/python -m goalcal.admin reset-password alice
```

禁用、启用、重置密码都会撤销原会话。公网入口 8501 是登录网关，Streamlit 只监听本机 8502。网关用 `HttpOnly`、`SameSite=Lax`、公网 HTTPS 下 `Secure` 的同源 Cookie 保存随机会话令牌，最长 12 小时；硬刷新可恢复登录。退出时服务端撤销令牌并清除 Cookie。聊天、候选和草稿仍保存在数据库中，重新登录后可以恢复。每次操作检查会话，禁用后静止页面上已经显示的内容会在下一次操作时退出。不要直接将 8502 暴露到公网，也不要将会话令牌放入 URL 或浏览器本地存储。

登录限流持久保存：单用户名每 15 分钟最多 5 次失败；全站同一窗口最多 100 次登录尝试（保护本机密码哈希资源）。成功登录清除该用户名计数。注册时可以设置 IANA 时区，例如 `America/New_York`。

## 固定域名与正式隧道

先安装 cloudflared（例如 `brew install cloudflared`），然后：

```sh
cloudflared tunnel login
cloudflared tunnel create goalcal
cloudflared tunnel route dns goalcal cal.example.com
.venv/bin/python deploy/generate.py --domain cal.example.com --tunnel-id YOUR-TUNNEL-UUID --credentials "$HOME/.cloudflared/YOUR-TUNNEL-UUID.json"
cloudflared tunnel --config "$PWD/deploy/generated/cloudflared.yml" ingress validate
```

生成配置只映射指定域名到本机 8501，其他请求返回 404。保留 TLS 证书验证、Streamlit XSRF/CORS 防护及 Cloudflare WebSockets 支持；不要设置 `noTLSVerify`，不要关闭 XSRF。Cloudflare 控制台开启 HTTPS 强制跳转；不要配置页面缓存应用或 WebSocket 路径。不要把数据库目录、日志或凭据发布为静态文件。

参考 [Cloudflare 正式隧道](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)、[隧道配置](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/)、[Streamlit 服务配置](https://docs.streamlit.io/develop/api-reference/configuration/config.toml)。

## 登录后自动运行与异常恢复

`deploy/generate.py` 生成 app、tunnel、backup 三个用户级 launchd plist。检查其中绝对路径，再安装：

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp deploy/generated/local.goalcal.*.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.goalcal.app.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.goalcal.tunnel.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.goalcal.backup.plist"
```

应用和隧道异常退出后自动拉起；备份在登录加载时和每天 03:15 执行。用户退出登录、主机断电或断网会停止服务。macOS 系统设置 → 节能，开启接电时防止自动睡眠；也可由主机管理员执行 `sudo pmset -c sleep 0`。无需关闭显示器睡眠。FileVault 解锁和用户登录之前，用户级服务不会运行。

日志位于数据库同级 `logs/`，应用、隧道输出按 2 MB 轮转、各保留 5 个历史文件。业务日志只记录操作类别、内部账号 ID 和通用错误，不记录密码、邀请码、密钥、订阅 URL 或日历正文。目录应仅当前 macOS 用户可访问。

## 停止、升级与回滚

```sh
launchctl bootout "gui/$(id -u)/local.goalcal.tunnel"
launchctl bootout "gui/$(id -u)/local.goalcal.app"
launchctl bootout "gui/$(id -u)/local.goalcal.backup"
.venv/bin/python -m goalcal.admin backup
```

记录当前 git 版本及依赖锁定文件，更新代码后安装 `requirements.lock`，执行 `python -m goalcal.admin migrate` 和测试，然后按上文 bootstrap。迁移自动创建 `pre-migration-*.db`；迁移失败会回滚并阻止启动。不要在运行期间迁移或替换数据库。`PRAGMA user_version` 大于当前代码版本时拒绝启动。

回滚时停止三个服务，恢复原代码与锁定依赖，再用对应版本备份恢复数据库。旧代码不得直接打开新版 schema。

## 备份与恢复

SQLite 使用 WAL、外键约束、10 秒 busy timeout。写事务不等待外部 AI；写事务中的续批/细化回退模板，AI 自建目标没有内置模板时按大纲下一阶段提供通用行动步骤；独立的只读规划可以请求 AI。每用户每天 30 次实际 API 请求（失败也计数），北京时间换日，SDK 自动重试关闭；再次尝试消耗新额度。超额走规则/模板，无法生成有效改计划时保留旧计划。

```sh
.venv/bin/python -m goalcal.admin backup
# 先停止 app、tunnel、backup，再恢复：
.venv/bin/python -m goalcal.admin restore /absolute/path/to/backup.db --app-stopped
```

通过 SQLite online backup 获取一致性副本，保存最近 14 天的 `daily-*`；迁移前、恢复前和导入前的安全快照不会自动清理。恢复检查 integrity 和外键，并删除全部旧登录会话。恢复会先保存当前数据库。不要直接复制正在运行的 `.db`，更不要漏掉 WAL。建议定期将备份另行复制到其他磁盘；当前方案不保护整机磁盘损坏。

[SQLite WAL](https://www.sqlite.org/wal.html) 和 [online backup](https://www.sqlite.org/backup.html)。

## 显式迁入开发数据

新用户默认空白；旧开发账号没有登录凭据，不会自动绑定到新用户。先创建一个空白目标账号，再执行：

```sh
.venv/bin/python -m goalcal.admin import-legacy /absolute/path/to/development.db --source-user 1 --target owner
```

源数据库在线备份保留，原文件不改业务内容；导入重新映射全部关联 ID，并拒绝跨用户引用及已有业务数据的目标账号。整次导入事务失败则不改变目标账号。避免在导入期间操作该目标账号。

## ICS 限制

上传和下载最多 5 MiB；下载总时限 15 秒、最多 3 次重定向。仅标准端口 HTTP/HTTPS，拒绝非公网地址和含认证信息 URL。DNS 的全部 A/AAAA 记录检查通过后连接到固定 IP，Host/SNI 保持原域名，TLS 正常验证，不读取环境代理。每次重定向重新校验。

一次导入最多 5,000 条事件（包括展开结果），整次重复展开最多扫描 10,000 个实例；超过则全部回滚。支持 DAILY/WEEKLY 的常用重复字段，拒绝按秒/分钟等复杂扩展，沿用原有 8 周展开窗口。

## 验收清单（必须在实际环境执行）

本机自动化：`.venv/bin/python -m pytest -q`。包括原有功能、显式登录 UI、注册/邀请码并发、会话失效、账号隔离、AI 并发额度、SSRF/重定向/固定 IP、导入限制、五用户并发和备份恢复。

实际部署还需人工填写日期、设备、网络及结果：

- [ ] 域名 HTTPS 有效；127.0.0.1:8501 无公网直接监听。
- [ ] 中国大陆外部移动网络（关闭家中 Wi-Fi）登录、注册、聊天、目标、日历拖动、ICS 上传和网址导入。
- [ ] 两个浏览器账号相互看不到记录，退出/重登后无旧聊天或草稿。
- [ ] WebSocket 断线重连，超时后重新登录。
- [ ] 终止 Streamlit 子进程后 launchd 恢复；终止 cloudflared 后恢复。
- [ ] Mac 重新登录后三个任务启动；备份生成且保留策略正常。
- [ ] 停机后从备份恢复，记录完整，原会话失效。
- [ ] 连续移动网络访问稳定，达到试用可用性要求。

域名和 Cloudflare 账号由主机所有者提供；大陆网络可用性以实测为准。未完成上述实测时，不标记公网部署完成。

## 本次实现的本机验证记录（2026-10-23）

- Python 3.12 独立 `.venv`，`requirements.lock` 固定全部已安装依赖，`pip check` 通过。
- 自动化测试 342 项通过，包含原有业务、登录 UI、账号隔离、并发及安全测试。
- 临时用户级 launchd 服务已实测：启动、127.0.0.1:8501 健康检查、杀掉 Streamlit 子进程、自动恢复及再次健康检查。测试服务已卸载。
- 三个生成 plist 通过 `plutil -lint`，示例正式隧道 ingress 配置通过 `cloudflared ... ingress validate`；验证用占位配置已删除。
- 在线备份、恢复后会话撤销、开发数据迁入及迁移失败回滚已通过自动化测试。
- 本机已有 cloudflared 可执行文件；未发现 `~/.cloudflared` 登录证书、隧道凭据或配置。尚未创建首次正式账号、安装正式 launchd 服务、发布实际域名或进行大陆外部移动网络验收。

## 本机安装状态（2026-10-23）

已生成权限为 600 的 `.env.production`，使用独立绝对路径生产数据库，沿用本机已有的服务器 AI 配置（每日每用户 30 次）。已初始化数据库并生成在线备份；未导入开发数据。

已安装并加载 `~/Library/LaunchAgents/local.goalcal.app.plist` 和 `local.goalcal.backup.plist`，登录后启动；应用异常退出自动恢复，每日 03:15 在线备份。应用 `http://127.0.0.1:8501` 健康检查通过，仅监听 127.0.0.1。备份服务首次执行成功。Mac 接电系统睡眠已为 0。

首次账号通过双击 `deploy/setup-owner.command` 创建，用户名 `owner`，密码由主机所有者在终端隐藏输入两次。此步骤不生成默认密码。

正式隧道仍待主机所有者完成 Cloudflare 登录并确定域名后安装；无需重新安装本机应用和备份服务。

当前 owner 账号已由主机所有者创建，账号启用；Cloudflare 浏览器登录成功，但账号域名列表为空。正式域名和隧道仍待确定，生产服务及备份继续在本机运行。
