# Design

GoalCal 的视觉基调：暖、安静、克制。界面退到内容后面，对话是主角。

## Tokens

### 基础主题 Token

| Token | 值 | 用途 |
| --- | --- | --- |
| 背景 | `#FAF9F6` | 暖白，全站底色（含顶部 header） |
| 次级背景 | `#F1EEE9` | 暖灰，sidebar / 首页建议 hover 底 |
| 表面白 | `#FFFFFF` | `--goalcal-button-bg`，用户气泡、登出按钮、Calendar 导入入口底 |
| 文字 | `#292524` | `--goalcal-ink`，深墨正文与标题 |
| 主色 | `#0F766E` | `--goalcal-primary`，深青，主按钮 / 选中态 / 进度条 / 链接 |
| 分隔线 | `#E7E2DA` | `--goalcal-line`，细分隔线与容器/输入框描边 |
| 基础圆角 | `8px` | `--goalcal-radius` / `0.5rem`，按钮、输入框、边框容器统一 |
| 气泡圆角 | `16px` | 用户聊天消息胶囊圆角 |

字体只用系统栈（`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`），不引入外部字体与前端框架。

基础主题落在 `.streamlit/config.toml`（Streamlit 主题）与 `goalcal/ui/theme.py`（CSS 细节），两处保持一致。

### 日历语义色板（`goalcal/domain/calendar_service.py`）

| 语义 | 值 | 说明 |
| --- | --- | --- |
| 目标块 `planned` | `#0F766E` | 与主色一致 |
| 目标块 `done` | `#6BA589` | 鼠尾草绿，已完成记录 |
| 目标块 `partial` | `#D97706` | 琥珀橙，部分完成 |
| 目标块 `missed` | `#CBD5E1` | 浅灰，未完成/过期 |
| 外部日程 `external` | `#94A3B8` | 中性灰 |
| 缓冲块 `buffer` | `#E2E8F0` | 极浅灰 |

> 注意：日历事件色板当前独立于 `theme.py` 定义在 `calendar_service.py`；后续若新增事件类型（如习惯/重复任务），须先在本节约定不与 `partial` 等状态色冲突的二维视觉语法。

## 原则

- **单一主任务**：任何时刻 Chat 里只有一个待处理面板（`Confirm action` / `Goal draft` / `Suggested times` / `Batch plan` / `Calendar preview` / 快捷回答），统一用「小字标签 + 内容 + 主按钮」的克制样式，不叠卡片。
- **消息流视觉区分**：隐藏默认头像；助手回复不定高、无背景无边框，随内容生长；用户消息采用右对齐白底胶囊（`#FFFFFF`、`1px solid #E7E2DA`、`16px` 圆角、`max-width: 80%`）。输入框使用原生 `chat_input`，空首页与消息流共用同一输入入口。
- **层级靠留白和字重**：不用全大写标签做装饰，不用营销式欢迎区；空状态只给少量入口。
- **选择态可见**：Goals 目录里选中项用主色按钮（`primary`），其余为普通按钮。
- **跨页 DOM 稳定**：`app.py` 在登录后共享壳层中以固定节点顺序注入全局 CSS 与隐藏/激活的 `calendar_shell`，避免切页时日历组件节点错位重挂载。

## 页面布局与版宽现状

- **侧边栏（`app.py`）**：顶部放不可交互的粗体 **GoalCal**，下方依次是 `New conversation` 按钮与 `Chat / Goals / Calendar / Settings` 导航链接。未登录或网关会话失效时导航隐藏（`goalcal/ui/auth.py` 渲染登录/注册表单或跳回网关的超链接）。
- **Chat**：内容区居中，固定 `width=740`（`goalcal/ui/chat_page.py`）。
- **Goals / Calendar / Settings**：当前沿用 `layout="wide"` 铺开。
  - **Goals（`goals_page.py` / `progress.py`）**：多目标时为 `1:2` 目录与详情；主区突出进度与下一步，`All steps` 默认折叠，`Manage goal` 收纳置顶、AI 调整与归档。
  - **Calendar（`calendar_page.py` / `calendar_view.py`）**：`Week / Day / List` 视图，`firstDay=1`、`06:00–24:00`；标题旁带轻量 `Import calendar` 跳转链接；点击事件二次确认删除，点空白新建，支持拖拽改期。
  - **Settings（`settings_page.py`）**：包含 Account（账号、时区修改、Log out）、Scheduling preferences（含历史优势时段开关与洞察）、7 天指标、ICS 导入（文件 / 订阅 URL）与 Trial feedback。

## Chat 空首页：三层对话入口

状态：**已实现（2026-10-27）**。

首页不是 dashboard，而是带一点当前情境的对话入口。参考 Claude / ChatGPT 的“一个问题、一个输入框、少量建议”，保留 GoalCal 自己的暖色与“为目标安排时间”的定位；不复制深色主题、模型选择器、图标墙或额外工具栏。

### 左侧产品名

- 侧栏顶部放不可交互的粗体 **GoalCal**，下方依次是 New conversation 与现有导航。
- 主内容区不再重复产品名，不显示巨大的欢迎标题。

### 中间三层

1. **一句 slogan**：首版固定为 **What do you want to make time for?**。不做随机轮播；以后若按状态变化，也必须确定、无布局跳动。
2. **唯一输入框**：保留原生 `st.chat_input`，placeholder 为 “Tell me a goal, schedule time, or share your progress…”。空首页不显示预置助手气泡、tagline 或另一组说明。
3. **2–3 条情境建议**：一行一条的可点击文字行，不做大卡片。用户状态直接藏进建议句，不再单独展示目标切换器、进度、Next step / Next event 摘要或静态 Keep going / Start something。

内容区保持约 740px 最大宽度，用适度顶部留白形成居中感。建议结构：

```text
What do you want to make time for?

[ Tell me a goal, schedule time, or share your progress… ]

Find time for my goal
Review my calendar
Start a new goal
```

### 建议的数据与行为契约

- Find 只在仍有未排期 `todo` 步骤时出现。多目标时先询问要安排哪条目标，并保存等待选择状态；回答目标名后进入该目标的下一未排期步骤。单目标可直接进入候选。
- 无明确步骤的排期优先选择没有未来目标块的 `todo` 步骤，避免重复推荐已经预约的步骤。
- Review 不拼接下一事件摘要；无日程时省略。点击后查询从当前时间开始的近期日程，按日期分组、每条事件单独一行。
- 无数据时仍保持三层结构，建议换成 Create a goal / Add an event / 产品能力，不回退到另一套首页。
- 候选显示中，序号用于选择、宽泛时间用于重筛、具体钟点用于安排当前步骤；明确的新日程命令不被当前候选捕获。
- Start a new goal 进入 `goal_intake`，先询问目标与可选期限，下一条用户输入才进入建目标流程，不注入示例目标。
- 所有聊天消息由同一渲染入口保留可见换行；多段落使用空行，多项内容使用 Markdown 列表。
- 用户发出第一条消息后进入现有消息流，三层首页消失；草稿、候选和待确认面板仍遵循单一主任务原则。

## 待办与演进方向（Backlog）

- **界面与体验优化（待实施）**：详见 [ui-ux-optimization-2026-11-16.md](ui-ux-optimization-2026-11-16.md)，包含 Chat 候选理由展示与提案历史态留痕、Goals `StepStrip` 进度块与版宽收紧（860–960px）、Calendar 弹窗就地完成/改期与图例、Settings 时段控件化、以及重复事件系列（`EventSeries` / `SeriesScope`）。
- **习惯（重复任务）与 Today 视角（暂留待办）**：
  - 定义上与 `Goal`（终局导向）区分，作为周期性节律实体独立建模；
  - **仅采纳需要消耗时间与精力的习惯**（具备时长 `est_min > 0` 与精力/类型属性，排入真实时间块，不引入喝水吃药类零时长纯打卡）；
  - **与目标共享每日时间上限**（排期引擎 `max_goal_minutes_per_day` 合并统计当日已排的目标块与习惯块）；
  - 在 `Today`（待定形态）与 `Calendar` 视角下，与目标步骤保持同等执行地位；
  - 后续启动设计或开发前，须先在本规范中补全“事务类型（External / Goal / Habit）× 执行状态（Planned / Done / Partial / Missed）”的二维日历视觉语法与 Today 页面信息架构。
