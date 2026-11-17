# GoalCal 界面设计与用户体验优化方案

> 参考基准：`GoalCalendar_IOS_demo` 交互设计 × 当前 `GoalCal` Web 端实现（`app.py`、`goalcal/ui/*`、`goalcal/domain/*`）
> 日期：2026-11-16

---

## 一、核心诊断：当前 GoalCal 缺哪几种“体验感”？

GoalCal 目前在视觉基调上已经建立起很克制、舒服的风格（`goalcal/ui/theme.py` 的暖白底 `#faf9f6` + 深青主色 `#0f766e` + 细线边框），但从**产品体验与用户心理**看，它目前更像“四个各自独立的功能工作台”，而不是一个会推着人往前走的“目标推进伙伴”：

1. **只见列表，不见“推进感”**：目标拆完后，在 Goals 页是一串文本和进度条，用户一眼看不出“已经推进了多少块、还剩几块卡在哪里”。
2. **后台很聪明，前台没说出来**：后端调度器 `Candidate`（`goalcal/domain/scheduler.py`）算出了 `reasons`（推荐该时段的原因），但前端渲染完全没有展示，用户只看到冷冰冰的时间段，缺乏点下 `Book` 的说服力。
3. **跨页闭环断在最后一厘米**：用户在 Calendar 页点开一个目标块，目前只能“删除”；想顺手“标为完成”或“改到别的时间”，必须跳回 Goals 或回到 Chat 重新打字。
4. **“人话输入”与“极客表单”割裂**：Chat 页追求自然语言极简，但一进 Settings 页就要手写 `09-12,14-18`、`12-13` 这种字符串配置。

---

## 二、分页面优化建议（结合 `GoalCalendar_IOS_demo` 的设计思路）

### 1. Chat 页：从“极简问答框”变成“有说服力、可追溯的决策流”

对应文件：`goalcal/ui/chat_page.py`、`goalcal/ui/chat.py`、`goalcal/ui/current.py`
借鉴来源：`GoalCalendar_IOS_demo/GoalCalendarApp/Views/ChatView.swift`（`ProposalCard`）、`ChatViewModel.swift`

* **痛点 A：推荐时段卡片“哑巴式展示”**
  * **现状**：`chat.py` 中的 `render_proposals` 只渲染了 `1. 14:00–14:45 · 45 min` + `Book` 按钮，后端 `scheduler.py` 算出的 `Candidate.reasons` 完全被丢弃。
  * **优化方向**：借鉴 iOS `ProposalCard`，把每个候选做成**微卡片**：上方是加粗时间与时长，下方用一行浅灰小字直接写明**第一条推荐理由**（例如“上午精力最佳 · 前后留有缓冲”）。**用户愿意把真实时间交给你，前提是他能看懂你为什么选这里。**

* **痛点 B：空白首页没有利用已有上下文**
  * **现状**：`current.py` 的 `current_status` 已经查出了 `goal_title`、`next_step_minutes`、`next_event_title`，但 `chat_page.py` 的 `home_suggestions` 渲染出来的仍是泛化的 `"Find time for my goal"`。
  * **优化方向**：把真实状态轻柔地融进建议文案里（例如 `Schedule “写完引言” (30 min)`、`What's after “周会”?`）。保留只有 3 条建议的极简布局，让用户一打开就觉得“它记得我的事”。

* **痛点 C：目标拆解等待时的“假死焦虑”**
  * **现状**：LLM 拆解目标或重排时耗时较长，Streamlit 只有右上角一个小转圈。
  * **优化方向**：借鉴 iOS `ChatViewModel.send` 的渐进式 `statusText` 提示——在对话流底部放一个带阶段说明的反馈条（比如拆解草稿耗时超过几秒时提示 `"Breaking down your goal into steps… usually takes 15–30s"`），降低用户误以为卡死而重复操作的概率。

* **痛点 D：已处理的提议“消失无痕”**
  * **现状**：`_render_pending`、`_render_goal_draft`、`_render_pending_action`、`_render_event_draft` 都挂在消息流最底部，用户点完确认/取消就消失了，往上翻聊天记录只能看到一句系统回复，看不到当时确认的草稿或时间选项。
  * **优化方向**：借鉴 iOS `ChatMessage.resolved` 机制，把“曾经确认过/取消过的提案摘要”以折叠态或浅灰 `Confirmed / Dismissed` 标签留在消息流里，形成清晰可追溯的操作轨迹。

---

### 2. Goals 页：用“像素块进度感”替代“表单堆砌感”

对应文件：`goalcal/ui/goals_page.py`、`goalcal/ui/progress.py`
借鉴来源：`GoalCalendar_IOS_demo/GoalCalendarApp/Views/GoalListView.swift`（`StepStrip`）、`GoalDetailView.swift`

* **借鉴 iOS `StepStrip`：增加“可视化完成拼图”**
  * **现状**：现在只是一条细 `st.progress` 进度条，6 步完成 2 步和 12 步完成 4 步视觉上几乎没区别；而且左侧多目标目录 `_render_directory` 中，进度条贴在按钮下方，缺少 `done/total` 数字和截止日紧迫度。
  * **优化方向**：借鉴 iOS `StepStrip`，在主目标卡顶部进度条下方渲染一排等宽小圆角色块——**已完成（绿/深青）、已排期（浅青/蓝）、待安排（浅灰）、已跳过（淡橙）**。每次完成一步就像填满一格拼图，即时成就感远强于纯百分比数字。

* **给 `All steps` 做减法，提升扫描效率**
  * **现状**：`progress.py` 的 `render_goal` 展开 `All steps` 后，每一条 `todo` 步骤下面都直接平铺三个全宽按钮（`Find time` / `Complete` / `More`），10 个步骤会展开 30 个按钮，视觉噪音极高。
  * **优化方向**：借鉴 iOS `GoalDetailView` 的紧凑行结构——每行只保留**左侧状态点击区 + 中间标题与时长/排期时间 + 右侧轻量操作入口**；编辑表单和删除确认继续收进二级菜单，让整个计划在一屏内即可轻松扫完。

* **把“我现在状态不好”做成顺手入口**
  * **现状**：GoalCal 的核心卖点是“状态感知（tired / busy / uninspired）”，但 Goals 页和 Calendar 页都没有状态入口，用户想根据状态重排还得切回 Chat 打字说 `"I'm tired"`。
  * **优化方向**：在 Goals 页顶部或 `Next` 卡片旁放一个轻量的状态胶囊（如 `Feeling: Normal | Tired | Busy | Stuck`），点一下就能直接用当前状态重新触发这一步的推荐或恢复建议。

---

### 3. Calendar 页：补齐“就地闭环”，别让用户来回跳页

对应文件：`goalcal/ui/calendar_page.py`、`goalcal/ui/calendar_view.py`、`goalcal/domain/calendar_service.py`
借鉴来源：`GoalCalendar_IOS_demo/GoalCalendarApp/Views/CalendarView.swift`（`EventDetailSheet`、`EventComposerSheet`、`goalsSection`）、`EventSeries.swift`、`SeriesScope.swift`

* **痛点 A：点开日历事件，不能直接完成或修改时间**
  * **现状**：`_event_details` 弹窗里，即使事件关联了 `Goal step`，也只有 `Delete event` 和 `Close` 两个按钮。想改具体时间只能靠拖拽（在 List 视图或跨天时很难拖），想标完成必须切到 Goals 或 Chat。
  * **优化方向**：借鉴 iOS `EventDetailSheet`：
    1. 如果是目标块（`step_id` 存在），弹窗顶部给主按钮 **`Mark done`**（直接推进目标进度并弹出反馈），次操作给 **`Skip / Missed`**（接入现有恢复流程）；
    2. 在弹窗内直接支持**精确修改日期与起止时间**，并在修改时实时提示是否与其它日程重叠（只提示不强行阻止）；
    3. 增加一个 **`View goal →`** 跳转，直达该步骤所属目标。

* **痛点 B：色彩编码缺乏图例与“目标全局感”**
  * **现状**：`calendar_service.py` 已经定义了语义色板（计划中深青 `#0f766e`、已完成鼠尾草绿 `#6ba589`、部分完成琥珀 `#d97706`、错过浅灰 `#cbd5e1`、外部日程 `#94a3b8`），但日历页面上没有任何图例说明。
  * **优化方向**：在日历顶部标题行或下方加上一行极简**色块图例**（`● Goal planned  ● Done  ● Partial  ● External`）；同时借鉴 iOS `goalsSection`，在日历下方放一个紧凑的“当前活跃目标进度条带”，让用户在排时间时心里始终挂着目标总进度。

* **痛点 C：手动新建不支持重复日程**
  * **现状**：`_new_event` 只能建单次日程，想添加“每周二、四晚去健身”这类固定约束，只能手动逐条创建。
  * **优化方向**：借鉴 iOS `EventComposerSheet` 与 `EventSeries`，增加轻量开关 `Repeat weekly` + 星期一到日圆点多选 + 持续周数（并实时预览 `"Will create N events"`），方便快速锁出固定日程。

---

### 4. Settings 页：去掉“极客语法”，按使用频率分层

对应文件：`goalcal/ui/settings_page.py`
借鉴来源：`GoalCalendar_IOS_demo/GoalCalendarApp/Views/SettingsView.swift`

* **把字符串输入改成可视化控件**：`Preferred windows` 目前要求用户按 `09-12,14-18` 手输，`Protected lunch window` 要求手输 `12-13`，输错就会报 `ValueError`。建议改成直观的时段勾选（如 `Morning 09–12`、`Afternoon 14–18`、`Evening 20–22` 快捷开关 + 自定义范围选择器），把配置出错率降到零。
* **让信息层级更符合用户心智**：
  * **高频个人面板**（7 天推进率 / 72h 恢复率 / 建议接受率 + 行为学习洞察 `History insight`）：这是最能给用户正反馈的成就感数据，放在 Settings 最底下容易被忽略，建议提升到 Settings 顶部（或做成清晰的顶部数据卡片），类似 iOS `SettingsView` 的数据区。
  * **低频系统配置**（时区、ICS 导入、重置偏好、Trial 反馈）：用选项卡或明确分组隔开，减少长表单压迫感。

---

### 5. 跨页导航与一致性（`app.py` 共享壳层）

对应文件：`app.py`、`goalcal/ui/theme.py`

* **增加“活跃状态微标”与“今日锚点”**：目前侧边栏只有 `New conversation` 和四个纯文字导航链接。可以在侧边栏下方展示当前置顶推进的目标名与进度条，以及“待签到过期块数量”提示（例如在 `Chat` 旁显示未处理气泡提示），让用户无论在哪一页都能感知当前焦点。
* **统一页面留白与宽度节奏**：Chat 页是居中的 `740px` 阅读宽度（体感最佳），而 Goals、Calendar、Settings 直接铺满 `layout="wide"` 全宽，在大屏显示器上 Goals 的表单和 Settings 的输入框会被拉得过长。建议除 Calendar 页保持较宽视野外，Goals 和 Settings 也约束到舒适的最大内容宽度（约 `860px–960px`），整体质感会更接近消费级效率产品。

---

## 三、建议的落地优先级（投入产出比排序）

| 优先级 | 改动项 | 涉及文件 | 预期体验收益 |
|---|---|---|---|
| **P0** | 在 Chat 候选时段卡片里渲染 `Candidate.reasons` | `goalcal/ui/chat.py` | 解释“为什么推荐这个时间”，显著提升点击 Book 的信任感 |
| **P0** | 在 Calendar `_event_details` 弹窗增加 `Mark done` 与改期表单 | `goalcal/ui/calendar_page.py` | 打通日历到目标进度的就地闭环，不再被迫来回切页 |
| **P0** | Calendar 页顶部加颜色图例；Goals / Settings 约束最大内容宽度 | `goalcal/ui/calendar_page.py`、`goalcal/ui/theme.py` | 消除色块语义盲区，提升大屏阅读质感 |
| **P1** | Goals 页引入 `StepStrip` 多色块微步骤条，精简 `All steps` 按钮密度 | `goalcal/ui/progress.py`、`goalcal/ui/goals_page.py` | 强化推进成就感，大幅降低长计划的视觉噪音 |
| **P1** | Chat 空首页利用 `current_status` 定制建议文案；提案保留 `resolved` 历史态 | `goalcal/ui/chat_page.py`、`goalcal/ui/controller.py` | 首屏更懂用户，对话记录可追溯 |
| **P1** | Settings 的 `Preferred windows` 与 `Protected lunch window` 控件化，指标面板上移 | `goalcal/ui/settings_page.py` | 消除手输字符串报错，突出正向成就反馈 |
| **P2** | 引入类似 `EventSeries` / `SeriesScope` 的重复事件创建与批量改期 | `goalcal/models.py`、`goalcal/domain/calendar_service.py`、`goalcal/ui/calendar_page.py` | 补齐“每周固定习惯/课程”的创建与批量维护能力 |
