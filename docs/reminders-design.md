# 主动提醒与节奏推进设计（Reminders Roadmap）

> 更新日期：2026-11-25
> 关联路线图：[TODO.md](TODO.md) · [日历同步设计](calendar-sync.md) · [习惯设计](habits-design.md)

---

## 一、产品定位与核心原则

GoalCal 的核心理念是 **“让计划适应生活节奏，而不是惩罚用户”**（anti-guilt, rhythm-adaptive）。因此，主动提醒系统的设计必须避开传统打卡软件“高频弹窗、过期红点轰炸”的陷阱，遵循以下 4 条原则：

1. **合并提醒**：晚间邮件汇总待签到的目标块和当天待处理的习惯，按用户时区执行免打扰时段与每日邮件上限。
2. **发送前复查**：发送邮件前检查账号、邮箱、提醒开关、消息时效、免打扰时段、每日额度与外部日历的阻塞事件。晚间收口、灵活习惯和目标停滞邮件还会重新检查触发事项。执行会话避让、排期保护时段避让和跨通道去重属于后续设计。
3. **10 秒闭环**：提醒内容直接携带行动上下文（如 `Step.outcome` 最小可交付结果），点击链接精准直达目标块或当天卡片，不让用户再在应用里翻找；配合 `next` 参数登录回跳减少中断感。
4. **决策引擎与投递通道解耦**：后端先沉淀统一决策引擎与幂等 `notification_outbox`，再挂载多条投递通道；且零中国大陆主体合规门槛，优先复用原生系统日历、邮件与 Web 开放标准。

---

## 二、四阶段推进路线

| 阶段 | 路线内容 | 解决的核心场景 | 前置依赖 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **阶段 0** | **原生系统日历闹钟** | 已排定目标块 / 习惯的**准点开始**（iPhone / Watch / Mac / 桌面系统原生强响） | 复用 V2.2 双向同步与 `.ics` 导出 | **已实现** |
| **阶段 1** | **统一提醒引擎 + SMTP 邮件摘要** | **晚间合并未签到收口**、灵活习惯周中窗口、早间聚焦、目标停滞/周报 | SMTP 凭据（与自助找回密码共用）+ 网页端时区设置 | **已实现** |
| **阶段 2** | **PWA + VAPID Web Push** | **外部日程冲突实时预警**、块结束 `+15~30min` 轻量签到、未绑日历块前提醒 | 阶段 1 提醒引擎与 `notification_outbox` | 待开发 |
| **阶段 3** | **BYO 自定义 Webhook**（可选备忘） | 进阶用户自接 Bark（iOS 强推）、飞书、Telegram 等自定义通知端 | 阶段 1 引擎 + Fernet 凭证加密 | 暂缓（已从 TODO 移出） |

---

## 三、阶段 0 技术实现规格（原生系统日历闹钟）

阶段 0 不引入新的数据库表迁移（保持 Schema `v13`），直接强化已上线的 V2.2 双向日历同步与 `.ics` 只读订阅：

### 1. 偏好存储（`user_preferences`）
- `scope = "reminders"`，`key = "calendar_alarms"`，JSON 结构：
  ```json
  {
    "enabled": true,
    "block_minutes": 10,
    "habit_minutes": 10,
    "manual_minutes": null
  }
  ```
- 取值约束：
  - `enabled`：布尔总开关。
  - `*_minutes`：`null`（关闭）或 `0..1440` 整数（`0` 表示开始时刻，`> 0` 表示提前分钟数）。
  - 默认值：开启状态，目标块提前 10 分钟，习惯安排提前 10 分钟，手动日程关闭（避免打扰常规日程）。
- **生效条件**：仅对未来尚未结束且状态为 `planned` / `moved` 的非全天（`all_day=False`）日程生成闹钟；已完成（`done`）、部分完成（`partial`）、未完成（`missed`）、已跳过（`skipped`）及全天事件自动不带闹钟，避免无效响铃。

### 2. 已启用日历通道映射（与预留通道）
- **Apple iCloud / CalDAV 与通用 `.ics` 订阅**：在 `VEVENT` 内生成 RFC 5545 `VALARM`（`ACTION:DISPLAY`、`TRIGGER:-PT{minutes}M` 或 `TRIGGER:PT0S`、`DESCRIPTION` 携带标题与步骤 `outcome` 摘要）。
- **Google Calendar**：在 `events.insert` / `events.patch` 载荷中设置 `reminders: { useDefault: false, overrides: [{ method: "popup", minutes }] }`；当关闭或事件已结束/完成时显式清空 `overrides: []`。
- **Microsoft Outlook / Graph（内部预留代码、未做且未启用）**：代码层面预留了 `isReminderOn` 与 `reminderMinutesBeforeStart` 字段映射；实际 Outlook 用户现阶段通过通用 `.ics` 订阅触发 `VALARM`。

### 3. 描述增强与精准深链（Deep Link）
- 对目标执行块（`kind="block"`），在日历描述中补充 `Outcome: <Step.outcome>`，让用户从手机日历弹窗或手表通知中一眼看清本次最小产出。
- 链接格式升级为 `https://<host>/app/calendar?date=YYYY-MM-DD&event={event_id}`：
  - `.ics` 订阅导出由请求头 `_public_base_url(request)` 推导域名；
  - V2.2 后台推送支持通过环境变量 `GOALCAL_PUBLIC_URL` 注入站点根地址；
  - 前端 `CalendarPage` 解析 `?date=` 与 `?event=`，进入页面后直接跳转到对应日期并自动弹出该事件（或习惯实例）详情弹窗，实现从日历提醒点进来的快速签到与调整。

### 4. 防同步回弹与实时更新
- `canonical_event_hash` 保持仅对 `(title, start_utc, end_utc, status, all_day)` 计算，外部日历客户端对闹钟的本地静音、延后（Snooze）或关闭绝不会引发反向重同步。
- 当用户在 `设置 → 提醒与通知` 中保存更改（`PUT /api/v1/calendar/sync/alarms`）时，后端事务立即对未来 180 天内尚未结束的开放目标块/习惯/手动日程重新入队 `sync_outbox`（`force=True`），使新闹钟配置自动同步至远端双向日历；`.ics` 订阅链接的 `ETag` 也会随字节变化自动更新。

---

## 四、阶段 1 技术实现规格（统一提醒引擎 + SMTP 邮件摘要）

### 1. 数据模型与偏好存储（Schema `v14`）
- **新增表 `notification_outbox`**：`user_id`、`channel`、`kind`、`dedupe_key`（`(user_id, dedupe_key)` 唯一约束保证幂等）、`recipient`、`subject`、`payload_json`、`scheduled_for`、`attempts` / `max_attempts`、`next_attempt_at`、`sent_at`、`status`（`pending | sent | skipped | dead`）、`last_error`。
- **邮件偏好 `user_preferences`**：`scope="reminders"`、`key="email_digests"`，支持总开关、早/晚时间点、晚间合并收口、灵活习惯周中窗口、目标停滞唤醒、周日复盘、免打扰时段（`quiet_start` – `quiet_end`）及每日邮件上限（`1..10`）。
- **账户时区切换**：扩展 `PATCH /api/v1/me`（及 `auth.update_timezone`）支持网页端「通用与账号」直接切换 IANA 时区，保障所有提醒严格按本地时区触发。

### 2. 决策引擎与发送前二次校验（`goalcal/domain/reminder_engine.py`）
- **5 条纯函数规则**（同一时段不轰炸，按规则幂等合并成单封邮件）：
  1. `evening_wrapup`：在 `evening_time` 后 20 分钟窗口内检查最近 7 天未解决目标块与当日待办固定/已排习惯，合并发送 1 封温和收口邮件（无未决项时不发）。
  2. `morning_focus`：在 `morning_time` 窗口内推送今日前几项已排目标块（附 `Step.outcome`）与当日习惯。
  3. `weekly_habit_nudge`：每周 `remaining_days <= remaining_count + 1` 时合并提醒灵活周目标习惯，每周至多 1 次（`dedupe_key = email:weekly_habit_nudge:<monday>`）。
  4. `goal_stall`：进行中目标含待办步骤、未来无排期且最近 6 天无打卡/推进活动时温和提醒，每周至多 1 次。
  5. `weekly_review`：周日 `weekly_review_time` 窗口内发送本周完成块数与习惯次数汇总。
- **Send-time Guard & 异步投递**：`auth_proxy.py` 后台 `_reminder_loop` 每 60 秒执行 `plan_due_notifications_once` + `process_notification_outbox_once`；所有 SMTP 网络请求（`goalcal/mailer.py`）严格在 SQLite 写锁之外执行，发送前会重新检查免打扰时段、每日上限、外部日程忙碌阻塞状态（忙碌或免打扰时延后 15 分钟）以及目标/习惯是否已被用户提前处理（已解决则标 `skipped` 放弃发送）。

### 3. 设置中心架构重构（「提醒与通知」独立分组）
- 设置页新增 **`提醒与通知`**（`/app/settings?section=reminders`，`GET /api/v1/reminders`、`PUT /api/v1/reminders/email`），将 **通道一（系统日历闹钟）** 与 **通道二（邮件节奏摘要 + 最近提醒记录）** 统一收拢：
  - 若用户尚未连接任何双向日历或 `.ics` 订阅，通道一展示清晰的新手引导与「前往日历同步」入口，消除误解；
  - 若服务端尚未配置 SMTP 环境变量（`SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM`），仍可提前保存偏好；SMTP 连通性仅限管理员在服务器本地通过 `goalcal.domain.reminder_engine.send_test_reminder_email` 命令行自测，不对普通终端用户暴露测试发信入口。

### 4. 邮件发信通道选型与扩容预案
- **当前生产选型（已上线）**：**Resend 免费版 + 自有子域 `goalcal.zhang.sarl`**（Tokyo `ap-northeast-1` 节点，发件人 `GoalCal <noreply@goalcal.zhang.sarl>`，标准 SMTP `smtp.resend.com:465` SSL，已在 Cloudflare DNS 仅 DNS 模式下完成 DKIM / SPF / DMARC 认证）。
- **当前免费容量评估**：免费额度 **100 封/天 · 3,000 封/月**。按默认 opt-in 与合并触发规则（开启后每人实际日均约 0.6–1.0 封），约可覆盖 **60–100 名开启邮件提醒的日活用户**（对应总注册约 150–300 人）；接入阶段 2 PWA 推送分流后可支撑更多用户。
- **迁移触发阈值**：现阶段不提前迁移；当 Resend 后台监测到**单日发信量连续超过 70 封**或开启邮件提醒的日活接近 60–80 人时再触发扩容/切换。由于 `goalcal/mailer.py` 基于标准 SMTP 实现，**无需改业务代码**，仅修改 `.env.production` 与 Cloudflare DNS 即可切换。
- **备选迁移方案**：
  - **Amazon SES**：按量 **$0.10 / 1,000 封**，大规模性价比最高（首次开通需申请移出沙箱）。
  - **阿里云邮件推送 DirectMail**：约 **¥2 / 1,000 封**，面向国内 QQ / 163 / 企业邮箱送达稳定。
  - **Resend Pro**：**$20 / 月** 含 50,000 封（取消日上限，超额 `$0.90 / 1,000 封`），升级零迁移成本。
  - **Brevo**：免费版每天 300 封、Starter **$9 / 月** 含 5,000 封。
- **明确不采用的路线备忘**：
  - 自建微信公众号（主体认证年费、`.sarl` 非可备案域名及 5s 回调限制导致运维成本过高，已明确放弃）；
  - Server酱等第三方桥接工具仅作为阶段 3 可选 BYO Webhook 出口，不作为全站默认通道。

---

## 五、后续阶段规划摘要（阶段 2 & 备忘）

### 阶段 2：PWA + VAPID Web Push
- 前端补充 `manifest.webmanifest` 与 Service Worker（`Cache-Control: no-store` 与正确 scope/CSP 策略），后端新增 `push_subscriptions` 表并通过 Web Push 协议投递。
- 重点服务实时场景：外部日历新日程触发 `sync_conflicts` 冲突预警、块结束 `+15~30min` 轻量签到提示，以及未绑定系统日历用户的块前提醒（与阶段 0 闹钟互斥去重，避免双响）。

### 阶段 3：BYO 自定义 Webhook（备忘 / 暂不排期）
- 若未来面向极客用户开放，可在设置页提供可选 Webhook 出口（URL/密钥经 `goalcal/crypto.py` Fernet 加密存储），同一套 `notification_outbox` 事件可按格式模板投递到 Bark、飞书机器人、Telegram Bot 或用户自建服务，不增加普通用户上手门槛。
