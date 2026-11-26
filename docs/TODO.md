# GoalCal 待办清单（TODO）

> 更新日期：2026-11-26
> 当前已上线版本：中英文公开官网介绍页 + 主动提醒阶段 0/1 + 自助密码重置 + AI 阶段提示 + 多目标全局每周平衡排期 Phase 1（`goalcal.zhang.sarl` / Cloudflare Tunnel `goalcal-v2` / 正式数据库 `var/production/goalcal.db` Schema `v16`）

---

## 一、待推进事项

- [ ] **提醒阶段 2 — PWA + Web Push 实时推送**（详见 [主动提醒设计](reminders-design.md)）
  - 先观察阶段 0 & 1 实际使用效果后再定；含 `manifest.webmanifest`、Service Worker、`push_subscriptions`、外部日历冲突实时预警与块结束轻签到。

---

## 二、近期已完成里程碑

- [x] **中英文公开官网介绍页（`/`）**
  - 已在 [frontend/src/intro/](../frontend/src/intro/) 与 [auth_proxy.py](../goalcal/auth_proxy.py) 实现独立构建入口（`frontend/intro.html`，不加载应用 API 或 FullCalendar）：匿名访问 `/` 展示中英文真实产品截图官网，已登录用户自动跳转 `/app/`。

- [x] **多目标全局每周平衡排期（Phase 1）**
  - 已上线确定性周排期引擎（[weekly_planner.py](../goalcal/domain/weekly_planner.py)）、Schema `v16` `web_weekly_plans`、日历周视图幽灵块预览与批量采纳/一键撤销（基于置顶 `pinned`、截止日期、剩余工作量与防饿死加权）。

- [x] **Cloudflare HTTPS 强制跳转加固**
  - 已在 Cloudflare 控制台开启 **Always Use HTTPS**。
- [x] **自助「忘记密码」邮件找回**
  - 已上线 Schema `v15` `password_reset_tokens`（SHA-256 摘要、30 分钟一次性失效、防枚举与限流保护、重置后自动吊销旧会话），并通过 Resend SMTP 发送双语重置链接邮件（`/_goalcal/forgot-password`、`/_goalcal/reset-password`）。
- [x] **AI 拆解 / 重规划长耗时阶段提示**
  - 新增通用 [AiProgressHint.tsx](../frontend/src/components/AiProgressHint.tsx) 组件，接入新建目标校准/拆解/重生成与助手对话（含重规划专属阶段与计时反馈）。

- [x] **ICS 双向订阅同步与原生双向读写同步（V2.0–V2.2）**
  - 已实现 Apple iCloud / CalDAV、Google Calendar、Microsoft Outlook 双向同步、双向 `.ics` 订阅、来源配色/全天策略/排期避让与同步冲突一键重排，详见 [日历同步设计](calendar-sync.md)。
- [x] **网页端修改时区**
  - 已在 `SettingsPage`「通用与账号」支持 IANA 时区选择器，并扩展 `PATCH /api/v1/me` 与 `auth.update_timezone` 实时生效。
- [x] **主动提醒阶段 0 & 1**
  - 已上线原生日历闹钟（`VALARM` / Google `reminders` / Outlook `isReminderOn` + 深链）、Schema `v14` `notification_outbox`、5 条反内疚节奏规则、Resend SMTP 邮件摘要与设置页「提醒与通知」独立分组，详见 [主动提醒设计](reminders-design.md)。

历史归并记录与旧实例下线证据见 [归并记录](records/consolidation-2026-11-17.md)。
