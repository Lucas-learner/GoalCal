# 日历同步设计与演进（V2.2）

> 更新日期：2026-11-24
> Schema 版本：`v13`

GoalCal 已完成 **V1（双向 `.ics` 订阅）→ V2.0（排期冲突联动与来源控制）→ V2.1（Apple iCloud / CalDAV 原生双向同步）→ V2.2（Google Calendar / Microsoft Outlook OAuth 实时双向同步）** 的全链路接入。

---

## 一、三层同步能力概览

### 1. 原生双向同步（Apple iCloud / CalDAV + Google Calendar + Outlook）
- 入口：`设置 → 日历同步 → 原生双向同步`
- 账号接入与专属子日历：
  - **Apple iCloud / Fastmail / 自定义 CalDAV**：通过应用专用密码验证（服务端通过 `goalcal/crypto.py` Fernet 对称加密保存，绝不回传前端或进入日志），自动发现日历并创建 **`GoalCal` 专属双向子日历**（`is_goalcal_calendar=True, writable=True`），无需在 iCloud 开启「公开共享日历」。
  - **Google Calendar**：基于 OAuth 2.0 + PKCE（`/_goalcal/oauth/google/start` / `callback`）连接，支持增量 `syncToken` 拉取、`events.watch` Webhook 实时推送及自动创建 `GoalCal` 子日历。
  - **Outlook / Microsoft 365**：基于 Microsoft Graph OAuth 2.0 + PKCE、`/calendarView/delta` 增量同步及 `/subscriptions` Webhook 推送。
- 异步推送与防循环：
  - **Outbox 异步队列（`sync_outbox`）**：在 `goalcal/web/http.py` 的 SQLite 写事务内通过 `before_flush` / `after_flush` 捕获可导出日程的创建、改期、完成和取消，仅写入轻量的 outbox 记录；网关后台 `_sync_outbox_loop`（每 3 秒）与 `_calendar_sync_loop`（每 60 秒）在 **SQLite 写锁之外**完成全部外部 HTTPS 请求，并支持指数退避重试。
  - **状态映射与防乒乓回弹**：通过 `event_sync_links` 记录 `(event_id, source_id, remote_uid, remote_href, remote_etag, last_synced_hash)`。外部拉回时比对 `last_synced_hash` 过滤自身推送；在苹果/Google/Outlook 中拖拽改期 `GoalCal` 子日历中的执行块会回写本地时间，删除则仅将本次执行块标记为 `skipped` 并把步骤释放回「尚未安排」（绝不误删目标步骤）。
  - 授权失效（401 / `invalid_grant`）时自动把 `calendar_connections.status` 置为 `reauth_required`，在设置页展示「需重新授权」提示。

### 2. 智能排期联动与来源控制（V2.0）
- **单源精细配置**：每个外部日历（无论原生连接还是 ICS 订阅）均支持独立设置：
  - `color`（标识符色，日历页筛选与事件色点标识）
  - `blocks_scheduling`（开启时参与排期避让与冲突检测；关闭时仅展示，如节假日/课表/赛事）
  - `all_day_mode`（`ignore` 忽略全天 / `show` 仅展示 / `busy` 视为全天忙碌）
- **冲突检测与一键重排**：`/today` 与 `/calendar/events` 自动返回 `sync_conflicts`；若外部新同步日程与未完成 GoalCal 目标块重叠，今日页与日历页顶部立即提示并提供 **一键「找新时间 / 重新找时间」**，直达排期候选弹窗。
- **日历页来源筛选**：存在同步日程时，日历页自动展示来源色点筛选条与全天槽（仅当存在全天日程时展开）。

### 3. 通用 `.ics` 订阅与一次性文件导入（兜底能力）
- **只读 `.ics` 导出**：`GET / HEAD /calendar/feed/{token}.ics`（256-bit 随机 Token、ETag/304、内容范围可选、严格排除 `import`/`sync`/`skipped` 事件）。
- **外部 `.ics` / `webcal://` 定时拉取**：支持多源独立配置、条件请求（`ETag` / `Last-Modified`）、`STATUS:CANCELLED` / `EXDATE` 与已移除事件清理。
- **手动 `.ics` 文件导入**：收纳在「从外部日历同步 → 一次性导入本地 .ics 文件」折叠区，适配无订阅地址的静态课表或邮件附件。

---

## 二、数据模型（Schema `v13`）

- `calendar_connections`：加密凭证容器（`provider`、`account_label`、`server_url`、`username`、`encrypted_secrets`、`token_expires_at`、`status`、`last_error`）。
- `calendar_sources`：同步日历配置（在 v12 基础上新增 `connection_id`、`remote_calendar_id`、`color`、`blocks_scheduling`、`all_day_mode`、`writable`、`is_goalcal_calendar`、`webhook_channel_id`、`webhook_resource_id`、`webhook_secret`、`webhook_expires_at`）。
- `events.all_day`：布尔标记全天日程。
- `event_sync_links`：双向映射与哈希防循环表（`(source_id, remote_uid)` 与 `(event_id, source_id)` 唯一）。
- `sync_outbox`：异步出站推送队列（`action`、`payload_json`、`attempts`、`next_attempt_at`、`status`）。

---

## 三、OAuth 环境变量说明（可选）

- Apple iCloud / CalDAV **开箱即用**，无需任何第三方平台密钥。
- 如需开启 Google Calendar / Microsoft Outlook 一键 OAuth，只需在 `.env.production`（或环境变量）配置对应凭据并配置回调白名单：
  - Google：`GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`（回调地址：`https://<域名>/_goalcal/oauth/google/callback`）
  - Outlook：`MICROSOFT_CLIENT_ID`、`MICROSOFT_CLIENT_SECRET`、`MICROSOFT_TENANT_ID`（默认 `common`；回调地址：`https://<域名>/_goalcal/oauth/outlook/callback`）
  - 加密主密钥：`GOALCAL_CREDENTIAL_KEY`（未显式设置时，生产环境会自动在数据库同目录生成 `0600` 权限的 `.credential_key`）
