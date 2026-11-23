> 历史资料：保留当时记录，命令、路径、功能和待办不代表当前版本。当前资料见 [文档索引](../README.md)。

# GoalCal 系统架构图

基于 2026-11-03 工作区代码整理。当前是 Python + Streamlit 模块化单体，图中的分层代表代码职责，并非独立微服务。

## 1. 系统总览

```mermaid
flowchart TB
    U[用户浏览器] --> GW[认证网关 · Starlette / Uvicorn<br/>登录、注册、会话 Cookie · 默认 8501]
    GW --> UI[Streamlit 应用 · 默认 8502<br/>Chat / Goals / Calendar / Settings]
    UI --> C[交互控制器 · ui/controller.py<br/>收集输入、恢复上下文、呈现草稿和候选、处理确认]
    C <--> CTX[对话状态 · dialog.py / conversation.py<br/>当前流程、目标绑定、提议生命周期、打断恢复]

    subgraph Understanding[理解与判断层]
        SHORT[确定性交互判断<br/>按钮、明确确认、序号、时间窗口等短路]
        L1[L1 AI 轮引擎 · llm/turn.py<br/>意图、指代、话题关系、接受或拒绝提议、自然语言回复]
        RULE[规则解析 · llm/rules.py<br/>L1 未配置或失败时兜底]
        GUARD[结构与语义校验<br/>TurnDecision → Action<br/>白名单、日期重算、目标绑定、危险操作确认]
        DATE[确定性日期解析 · llm/natdate.py<br/>相对日期、中文时间、时间窗口]
    end
    C --> SHORT
    C --> L1
    L1 -. 未配置 / 超时 / 非法输出 / 熔断 .-> RULE
    L1 --> GUARD
    RULE --> GUARD
    DATE --> GUARD
    SHORT --> A
    GUARD --> A[业务执行与编排 · agent.py<br/>用户归属校验、确认流、去重、动作执行]

    subgraph Domain[领域业务层]
        GP[目标判断与管理<br/>planner.py / goals.py<br/>复杂度、期限澄清、草稿、进度]
        SCH[确定性排期判断器 · scheduler.py<br/>计算空档、约束过滤、状态匹配、评分与解释]
        REC[签到与恢复 · recovery.py<br/>识别过期块、记录结果、调整下一步]
        REF[滚动细化 · refine.py<br/>后续步骤不足时规划下一批]
        CAL[日历服务 · calendar_service.py<br/>事件查询、创建、改期、删除]
        MEM[偏好记忆 · memory.py<br/>显式偏好、校验与物化、可关闭的行为学习]
    end
    A --> GP
    A --> SCH
    A --> REC
    A --> REF
    A --> CAL
    MEM --> SCH
    GP --> L2[L2 AI 规划层<br/>plan.py / event_plan.py<br/>目标阶段、步骤拆解、计划修订、日历草稿]
    REF --> L2
    A --> L2
    L2 --> PV[Plan 结构校验<br/>生成可预览的计划；失败时使用规则或模板降级]
    PV --> A

    L1 --> ROUTER[ModelRouter + LLMClient<br/>按 turn / plan 配置模型、超时、用量与遥测]
    L2 --> ROUTER
    ROUTER --> API[外部 LLM API<br/>OpenAI 兼容接口，模型由配置决定]
    L3[L3 AI 记忆层<br/>仅预留 memory 路由，尚无调用方]:::planned
    L3 -. 预留 .-> ROUTER

    GP & REC & REF & CAL & MEM --> ORM[数据访问 · SQLAlchemy<br/>db.py / models.py / migrations.py<br/>事务提交与回滚、迁移；时间以 UTC 存储]
    CTX --> ORM
    A --> MET[指标 · metrics.py<br/>建议接受率、72h 恢复率、推进率]
    MET --> ORM
    ORM --> DB[(SQLite 数据库 · WAL<br/>默认 var/goalcal-production.db)]
    GW --> AUTH[auth.py / tenancy.py<br/>账号、会话与租户隔离]
    AUTH --> DB
    ICS[ICS 文件 / 订阅 URL] --> IMP[ics_import.py / safe_fetch.py<br/>单向导入、UID 去重、重复事件展开]
    IMP --> ORM
    classDef planned fill:#f1eee9,stroke:#888,stroke-dasharray:5 5,color:#555
```

AI 不直接读写数据库。控制器和业务层装配上下文，AI 输出结构化提议；校验、确认和数据库操作由应用代码负责。普通对话或路由轮次最多调用一次 L1，明确需要规划时可追加 L2；按钮及部分明确输入可直接走代码。

## 2. “判断器”分别判断什么

项目没有独立部署的通用 Judge 服务，也没有额外的大模型裁判；判断能力分布在以下模块。

| 判断环节 | 实现位置 | 作用 | 使用 AI |
|---|---|---|---|
| 对话关系与意图 | `llm/turn.py` | 判断新指令、流程内回答、接受或拒绝提议、澄清等，输出 `TurnDecision` | L1 |
| 明确交互与降级解析 | `ui/controller.py`、`llm/rules.py` | 确认、取消、序号选择和时间重筛；模型失败时解析可识别指令 | 否 |
| 执行前校验 | `llm/action.py`、`llm/turn.py`、`agent.py`、`ui/controller.py` | 校验字段、动作范围、所属用户及目标绑定；高风险动作进入确认流 | 否 |
| 目标复杂度与澄清 | `domain/planner.py` | 判断目标类型和复杂度；缺期限时先澄清，再生成计划 | 否 |
| 日期与时间窗口 | `llm/natdate.py`、`timeutil.py` | 重算相对日期和时段，处理本地时间与 UTC 边界 | 否 |
| 计划有效性 | `llm/plan.py` | 校验 AI 返回的阶段、步骤、时长等结构，清理或拒绝无效输出 | 否 |
| 候选时段判断与评分 | `domain/scheduler.py` | 根据忙碌日程、缓冲、每日上限、精力、任务类型和偏好筛选并排序 | 否 |
| 过期与恢复判断 | `domain/recovery.py`、`agent.py` | 判断过期执行块；根据累、没时间、卡住、不想做等原因生成恢复动作 | 主要为确定性代码；细化可按需调用 L2 |

排期引擎是纯函数：输入日程、任务要求、状态与偏好，输出带 `score` 和 `reasons` 的候选时段。推荐不是模型随意给出的时间；用户选定候选后，业务层才执行预约。明确指定钟点的指令可以走直接排期路径，并经过相应校验。

## 3. 数据库与核心关系

```mermaid
erDiagram
    users ||--o{ goals : owns
    goals ||--o{ steps : contains
    users ||--o{ events : owns
    steps o|--o{ events : scheduled_as
    steps ||--o{ checkins : receives
    events o|--o{ checkins : records
    users ||--o| conversations : has
    conversations ||--o{ messages : contains
    users ||--o{ user_preferences : configures
    users ||--o{ state_logs : reports
    events ||--o{ reschedule_logs : tracks
    users ||--o{ interactions : generates
```

| 数据组 | 表 | 保存内容 |
|---|---|---|
| 用户 | `users` | 账号、密码哈希、时区和启用状态 |
| 目标与计划 | `goals`、`steps` | 期限、状态、置顶、阶段大纲、步骤时长、精力要求、完成进度 |
| 日历与执行 | `events`、`checkins`、`reschedule_logs` | 外部事件、目标执行块、签到结果、未完成原因、改期记录 |
| 状态与偏好 | `state_logs`、`user_preferences` | 精力状态、明确排期偏好、来源、证据与有效期 |
| 对话 | `conversations`、`messages` | 消息、草稿、候选、待确认动作、提议和打断恢复状态 |
| 产品指标 | `interactions` | 建议、接受、执行、恢复等行为埋点 |

上图覆盖 `models.py` 中的核心业务实体；认证辅助存储由 `auth.py` 管理。对话流程主要存放在 `conversations.state_json`，没有另建提议表。应用内切页保留对话；整页重进或新会话会重置本次消息和临时流程，目标与日历业务数据保留。当前记忆使用结构化偏好与历史事件，没有向量数据库；行为学习结果在排期偏好物化时计算，不自动写回偏好表。

## 4. 一次“创建目标并安排时间”的链路

```mermaid
sequenceDiagram
    actor U as 用户
    participant C as 控制器与对话状态
    participant L1 as L1 理解层
    participant A as 业务执行器
    participant L2 as L2 规划层
    participant S as 确定性排期引擎
    participant D as SQLite
    U->>C: 说出目标
    C->>L1: 用户输入 + 对话与业务上下文
    L1-->>C: 结构化意图与回复
    C->>A: 校验并转换后的 Action
    opt 缺少期限
        A-->>U: 询问期限
        U->>C: 回答期限
        C->>A: 更新 GoalBrief
    end
    A->>L2: 目标、期限与约束
    L2-->>A: 阶段大纲与首批步骤
    A-->>C: 校验后的目标草稿
    C-->>U: 展示草稿
    U->>C: Create & schedule
    C->>A: 确认草稿
    A->>D: 去重并创建目标与步骤
    A->>D: 读取日程、状态与偏好
    A->>S: 任务要求、忙碌块、状态、偏好
    S-->>A: 候选时段 + 分数 + 原因
    A-->>U: 展示候选
    U->>C: 选择候选
    C->>A: 接受排期
    A->>D: 校验并写入目标执行块与指标
    A-->>U: 返回预约结果
```

## 5. 代码依据

- 入口与部署：[`app.py`](../../app.py)、[`service.py`](../../goalcal/service.py)、[`auth_proxy.py`](../../goalcal/auth_proxy.py)。
- 控制与执行：[`controller.py`](../../goalcal/ui/controller.py)、[`agent.py`](../../goalcal/agent.py)、[`dialog.py`](../../goalcal/dialog.py)。
- AI 分层：[`turn.py`](../../goalcal/llm/turn.py)、[`plan.py`](../../goalcal/llm/plan.py)、[`router.py`](../../goalcal/llm/router.py)。
- 领域判断：[`planner.py`](../../goalcal/domain/planner.py)、[`scheduler.py`](../../goalcal/domain/scheduler.py)、[`recovery.py`](../../goalcal/domain/recovery.py)。
- 数据与记忆：[`models.py`](../../goalcal/models.py)、[`db.py`](../../goalcal/db.py)、[`memory.py`](../../goalcal/memory.py)。
