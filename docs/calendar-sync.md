# 日历同步设计与演进

> 更新日期：2026-11-23
> Schema 版本：`v12`

GoalCal 采用「**先通过 ICS 双向订阅覆盖全平台，数据与接口底座兼容后续原生双向协议**」的分阶段同步架构。

---

## 一、已实现能力（第一阶段：ICS 双向订阅）

### 1. GoalCal → 外部日历（只读 `.ics` 订阅导出）
- 入口：`设置 → 日历同步 → 导出到外部日历`
- 公开订阅地址：`GET / HEAD /calendar/feed/{token}.ics`
  - 每用户独立 256-bit 随机 URL-safe Token，支持随时停用或一键重置（旧链接立即返回 404）。
  - 响应携带 `Content-Type: text/calendar; charset=utf-8`、`Cache-Control: private, no-cache`、`ETag`（支持 `If-None-Match` → `304 Not Modified`）、`X-Robots-Tag: noindex, nofollow`，并附带 `REFRESH-INTERVAL;VALUE=DURATION:PT15M` 与 `X-PUBLISHED-TTL:PT15M` 刷新建议。
  - 前端同时提供 `https://` 复制链接和 `webcal://` 一键唤起系统日历按钮。
- 导出内容与防循环机制：
  - 默认包含：**目标执行块（`kind=block`）+ 习惯安排（`kind=habit`）+ 手动添加的日程（`kind=external` 且非导入/同步来源）**，可在设置页按项开关。
  - **严格排除** `source in {"import", "sync"}` 或 `sync_source_id != NULL` 的外部日程以及已取消（`status=skipped`）事件，彻底避免双向订阅造成循环重复。
  - 导出窗口为近 30 天至未来 180 天；已完成事项标题自动加 `✓ ` 前缀，描述包含目标/阶段/状态及 GoalCal 日历页链接。

### 2. 外部日历 → GoalCal（自动定时拉取 + 手动刷新）
- 入口：`设置 → 日历同步 → 自动同步外部日历`
- 支持绑定多个外部日历的 `https://`、`http://` 或 `webcal://` / `webcals://` 订阅链接（单账号上限 10 个，支持 15 分钟至每天 1 次频率）。
- 安全与并发边界：
  - 所有外部拉取经由 [safe_fetch.py](../goalcal/safe_fetch.py) 执行：禁止内网/回环/保留 IP、DNS 固定解析防重绑定、强制校验 TLS、禁用环境代理、限制重定向次数（≤3）、响应体上限 5 MB、总超时 15 秒。
  - 网络请求统一在 **SQLite 写锁之外**执行（`http.py:prepare` 及后台同步任务），不阻塞其他用户写入。
  - 后台协程（`auth_proxy.py` lifespan，可通过 `GOALCAL_SYNC_WORKER=0` 关闭）每分钟检查到期且启用的订阅源，配合 `ETag` / `Last-Modified` 条件请求减少无效传输，并用进程内源级互斥防止与手动同步并发竞争。
- 增量更新与清理：
  - 每个订阅源拉入的日程打上 `source="sync"` 和 `sync_source_id`，与其他订阅源及手动 `.ics` 导入相互隔离。
  - 重复同步按 `(sync_source_id, uid)` 就地更新，自动跳过 `STATUS:CANCELLED` 与 `EXDATE` 实例，并自动清理源日历中已删除的未来事件。
  - 删除订阅源时，安全解除关联外键并一键清理该来源的全部同步日程。
  - 与已确认习惯占用的冲突时段自动跳过（同手动 `.ics` 导入规则）。

### 3. 手动导入（`.ics` 文件）
- 保留在 `设置 → 日历同步 → 手动导入` 区域，与自动订阅源相互独立（`source="import", sync_source_id IS NULL`）。

---

## 二、数据模型（Schema `v12`）

- `calendar_feeds`：用户只读导出配置（`user_id` 唯一、`token` 唯一、`enabled`、`include_blocks`、`include_habits`、`include_manual`、`rotated_at`、`last_accessed_at`）。
- `calendar_sources`：外部同步源（`user_id`、`provider`、`direction`、`name`、`url`、`enabled`、`sync_interval_minutes`、`last_synced_at`、`last_status`、`last_error`、最近同步统计、`etag`、`last_modified`、`sync_token`）。
- `events.sync_source_id`：可空外键指向 `calendar_sources(id)`（`ON DELETE CASCADE`），用于按源隔离同步事件。

---

## 三、第二阶段演进路径（原生双向读写）

当前代码通过 `goalcal/domain/calendar_sync.py` 中的 `SyncProvider` 协议与 `calendar_sources` 统一模型为第二阶段预留了直接扩展位：

1. **新增原生 Provider**：在 `PROVIDERS` 注册 `google`（Google Calendar API v3 + OAuth 2.0）、`caldav`（Apple iCloud / Fastmail / Nextcloud CalDAV）或 `outlook`（Microsoft Graph），复用 `calendar_sources.sync_token` 与定时同步框架。
2. **与第一阶段共存与平滑切换**：
   - ICS 订阅继续保留为通用只读/兜底能力（学校课表、节假日、无 OAuth 权限的共享日历）。
   - 用户切换至原生连接后，删除旧 ICS 源会自动按 `sync_source_id` 清理旧源日程；停用只读导出链接即可避免与原生子日历写入重复。
