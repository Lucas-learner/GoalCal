# GoalCal 对话层 LLM 重设计

日期：2026-10-27 · 状态：第一、二步已落地（2026-10-27），第三步待实施

## 第一步落地情况（实际，非计划）

- **ModelRouter**（`goalcal/llm/router.py`）：按层 turn/plan/memory 返回客户端；
  `LLM_{MODEL,BASE_URL,API_KEY}_{TURN,PLAN,MEMORY}` 逐字段回落到现有 `LLM_*`。
  配额、认证、写事务保护仍在 `LLMClient` 内部不变，遥测事件新增 `layer` 字段按层归因。
  L1 客户端超时 8s、不重试；当前输出上限见下方实测修正。plan 层已接入
  `plan.py`/`event_plan.py`；memory 层暂无调用方（L3 未实施），refine.py 留待第三步迁移。
- **L1 轮引擎**（`goalcal/llm/turn.py`）：每轮最多一次调用，输出严格校验的
  `TurnDecision`（relation/intent/reply/topic/offer 等白名单字段）。`to_action` 用
  natdate 重算时间、规则否决"删目标被读成建目标"，执行走 `agent.execute`（从
  `agent.handle` 拆出的白名单入口）；chat 回复用 L1 文本，写操作文案仍是确定性代码。
  流程退出（草稿/候选/预览/签到）暂保留规则二次确认，`looks_like_new_command` 尚未删除。
- **DialogContext**（`goalcal/dialog.py`）：`{kind, stage, subject, offers, return_to,
  clarify_count}`。kind/stage/subject 由 `derive()` 从持久化草稿键确定性解释（单一解释点，
  不产生第二份需手工同步的真相）；return_to/clarify_count/offers 是新增状态，随
  `conversation.state_json` 持久化，兼容旧存档（缺省时按 idle 推导）。UI 入口经
  `_persist()` 统一同步。offers 的完整生命周期见"第二步落地情况"。
  `subject.topic`（L1 写的话题摘要）仅在 kind 不变时继承，话题切换即清理。
- **降级**：L1 未配置/失败/JSON 非法/意图越界 → rules + 确定性话术（chat 回复不再
  机械复读同一句目标建议）；连续失败 3 次熔断 5 分钟（`turn.CircuitBreaker`，
  进程级全局，匹配单用户自托管部署；测试用 `reset_breaker` 隔离）。认证错误、
  写事务保护、上下文装配失败是确定性守卫，不计入熔断；JSON 非法与
  字段越界计熔断并上报 `llm_invalid` 遥测（按 `error_type` 区分）。无模型环境
  行为不变（原测试全绿）。
- **安全网**：L1 提议的危险动作（删目标/删事件/改事件）一律压置信度强制走
  pending_action 逐字确认；`to_action` 的 duration clamp 到 5–480 分钟。
  plan 层门禁改为 `settings.llm_enabled` + 客户端 ready：只配 `LLM_API_KEY_PLAN`
  也能启用 L2。AI 请求仍按用户和日期计数，暂不限制每日次数。
- **新增能力**：`resume_goal` 意图（恢复暂停目标）；目标澄清被新指令打断时压栈
  return_to，"我想恢复"弹栈续聊；"好/可以"经 L1 accept_offer 映射到当前流程的
  确定性接受（草稿确认/首选候选/恢复候选）；clarify 连问两轮换成确定性选项清单。
- **测试**：`tests/test_l1_turn.py`（router/引擎/熔断/上下文持久化/execute，fake 客户端）；
  `tests/test_l1_conversation.py`（AppTest 多轮：每轮调用上限、动作执行、提议接受、
  打断恢复、clarify 不循环、失败降级+熔断、危险动作确认、offers 生命周期）。
  第一阶段验收时全量 556 passed。

## 第二步落地情况（实际，2026-10-27）

- **Offer Tracking 完整生命周期**（`goalcal/dialog.py`，不另建表）：offers 从"只存文本"
  升级为完整记录 `{id, text, kind, subject, created_ts, expires_ts, status}`，
  status ∈ open|accepted|declined|expired，随 `conversation.state_json` 持久化；
  旧存档 `{"text","ts"}` 由 `_normalize_offer` 补齐。API：`register_offer`
  （同 kind+subject 刷新而非堆叠）、`latest_open_offer`、`accept_offer`（接受一条、
  其余 open 失效）、`decline_offer`（只动一条）、`close_open_offers`（new_command/
  reject 时整轮失效）、`expire_offers`（TTL 600s）；总量上限 5 条，含少量已关闭历史。
  **模型只看到 open 提议的文本列表，永远不提供或选择 ID/动作**。
- **统一登记点**：`controller._record_reply` 里的 `_register_reply_offers` 覆盖所有
  确定性真实提议——goal_draft 预览、候选时段、批量排期、event_draft 预览、
  pending_action 确认、checkin recovery 候选与 goal_options 三选一；L1 文本提议
  在 `_apply_decision` 登记（kind=chat）。一致性守卫在 `derive()`：状态绑定提议
  （goal_draft/candidates/batch/event_draft/recovery/checkin_options/pending_action）
  对应会话状态消失即自动 expired——按钮、逐字短路、会话重置等任何清状态路径都被覆盖，
  无需散点调用。recovery 提议同时认 checkin(recovery) 与 pending(origin=recovery)
  两处状态（恢复候选两份状态都落）。
- **accept_offer**：`_accept_offer` 把最近 open 提议映射到当前确定性流程（草稿确认/
  首选候选/批量全排/日历预览确认/recovery 首选/pending_action 确认）；kind=chat 的
  L1 文本提议按当前话题翻译到具体流程。offer 与实际状态不匹配 → 标记 expired +
  诚实提示"That offer is no longer on the table"，绝不执行。goal_options 三选一
  无单一映射，按 in_flow 走 checkin 确定性解析（返回 `_UNHANDLED` 继续路由）。
- **reject_offer**：`TurnDecision.Relation` 新增取值，L1 prompt 教会识别
  "不用/先不/算了/no thanks" 为拒绝最近提议；controller 标记 declined + 关闭其余
  open 提议。拒绝状态绑定的提议会退出相应草稿/候选/预览；签到恢复会走确定性
  decline 路径推进队列；纯 chat 提议只关闭提议，不修改任务状态。零成本逐字短路
  （"取消"/"算了"等）保持优先，不进 L1。
- **clarify 三轮升级，不循环**：第 1 轮用 L1 短问；第 2 轮换确定性选项清单
  `agent.clarify_options`（2-3 个可执行选项，有进行中目标时带上具体下一步）；
  第 3 轮仍不明则 `CLARIFY_STOP` 明确"我先不猜"并清零计数回到 idle，不再追问。
- **回复三段式固化**：L1 SYSTEM（第二阶段 `PROMPT_VERSION` 为 l1.v2）rule 3 改为
  acknowledge → anchor（引用用户内容或当前上下文）→ ≤1 offer；`respond.py` 的
  chat fallback prompt 同步同一结构。确定性执行结果文案（Added/Booked/Goal set 等）
  保持原样，不强行模板化。
- **测试**：`test_l1_turn.py` 新增 offer 生命周期 API、状态绑定失效、旧存档兼容、
  "只喂 open 提议"共 4 条；`test_l1_conversation.py` 新增 reject 不执行/不误清、
  状态不匹配诚实拒绝、降级草稿提议被"好"接住、recovery 提议接受共 4 条，并扩展
  clarify 三轮升级与接受后的 offers 状态断言；每轮 L1 调用上限断言保留。
  第二阶段初次验收时全量 563 passed；后续交互修正及审查补丁完成后为 575 passed。

## 后续交互修正（2026-10-27，落地后实测反馈）

- **建目标先确认期限再生成计划（2026-10-27 修订）**：`_create_goal` 缺期限时只保存
  `goal_brief`，提供 7 days / 1 month / 3 months / Cancel，也接受自行输入日期或时长；
  不再展示与 3 months 重复的 90-day 默认选项。在确定期限前不调用 L2、不出草稿、
  不建目标。确认期限后才生成计划草稿，确认草稿后才建目标及排期；按钮确认在对话中
  记录用户操作，避免两条助手状态消息紧邻。已建目标的期限修改真实写库，并清除旧候选。
  `revise_goal_draft` 对**纯期限/日期修改**（`_timeline_only_edit`）按新期限重建草稿
  计划并保留 draft_id；期限未变化则只更新 brief，不重复调用 L2。
  混合指令（"拆成两段，3个月"/"把第一步改成3个月"）绝不静默只改期限——有 LLM 时
  交 L2 整体修订，`use_llm=False` 时返回 None（UI 诚实提示未能修改）。
  判断失败一律保守落 L2/None。`continue_goal`/`planner.ready`/
  `clarification_text` 用于新目标期限确认，也兼容历史 goal_brief 状态。
- **整页重进/新浏览器会话清空会话**：`controller.ensure_state` 在 `conversation_loaded`
  缺失时不再加载旧消息/任务状态，而是 `conversation.reset_state` 真实删除该用户当前
  Conversation 的 Message 行、state_json 清为 "{}"（Conversation 行保留），session_state
  重置为欢迎语 + 指引首页（不滚动）。应用内切页（Goals/Calendar/Settings → Chat）
  共享同一 session_state，不受影响。重置后的首次渲染通过一次性
  `checkin_suppressed_once` 标记抑制自动签到，避免过期块提问压住指引；用户发出第一条
  消息后 run_turn 内的签到逻辑照常。logout 后重新登录等价于 fresh session。
- **Chat 手动“New conversation”**：入口固定在侧边栏 Chat 导航上方，不随消息滚动；
  单击即复用 `conversation.reset_state`，
  删除当前用户本次会话消息和未完成的草稿、候选、提议、打断栈，回到指引首页；
  目标和日历数据不变。
- **Log out 不再依赖 10 分钟就过期的 form csrf cookie**：`auth_proxy.logout` 改为只校验
  有效 session cookie（`_browser_token`），有效即撤销 token 并删 cookie（303）；失效
  session 也清除 cookie 并回登录页。退出不读取无用表单正文，拒绝跨站来源请求；
  login/register 的 form csrf 校验不变；账号与登出入口统一放在 Settings，直连兜底分支保留。
- **危险动作确认可用自然语言**：逐字确认仍走零成本短路；"好的，确认吧" 等非逐字
  回答保留 pending_action 交给 L1 判定，接受时调用原有 `confirm_action`，拒绝时清除
  待确认状态。L1 失败或回答含糊时只提示确认/取消，绝不默认执行。

## 晚间排期对话修正（2026-10-27）

- 真实 `deepseek-flash` 合成探测中，`max_tokens=300` 时 5 轮有 2 轮把输出预算全耗在
  reasoning tokens，未返回 JSON；轮引擎现用 1000 token 上限，检测 provider 的
  `finish_reason=length` 并将空/缺字段决策视为失败。L1 prompt 升 `l1.v3`，明确本应用
  可以写日历，模型只做决策，不能用闲聊文案宣称已写入或无写入能力。
- 可执行 chat 提议现在持久化 `payload`：对 `schedule_step` 绑定用户拥有的 goal_id、
  step_id 和原文时间窗口；接受时再次校验目标活跃、步骤待办，按绑定步骤产生候选。
  多目标且无法唯一识别时追问目标，不会选择第一个活跃目标。"好/可以"对 open 提议
  走零成本短路；"帮我安排进日历"在紧接泛指的排期提议时也可接受它。
- 只有"今晚/明晚"等宽时段时不会默认写成 09:00；目标步骤给候选，普通事件追问钟点。
  中文数字钟点（如"今晚七点"）可解析。建议问题和明确否定不会写入；L1 把明确
  日历指令误报为 chat 时，确定性守卫仍把它交给执行器。降级规则保持 88/88 基线。
- 多轮验收覆盖多个活跃目标、接受/拒绝、泛指写日历、候选选定后真实写入、过期
  目标、L1 失败、宽时段/精确钟点和否定表达。上一轮的建议问题带有“今晚”时，
  简短回答“我想跑步”会继承这个时段；规则降级发起的排期提议也继承该时段。
  中文请求的候选和写入结果使用中文。`LLMClient` 在同时存在 HTTPS 与 SOCKS
  代理环境变量时显式采用 HTTPS 代理，避免可选 SOCKS 依赖缺失导致假性离线。
- 使用临时隔离数据库做过真实模型后台多轮验收：“晚上我可以做什么”→“我想跑步”
  得到半马下一步的今晚候选。验收还发现候选阶段的“可以”曾被当作“第一个”
  自动写入；现改为要求明确选择序号或点击候选后才写入。此验收没有触碰用户日历；
  最终候选确认与中文文案由 AppTest 回归覆盖。

## 网球/半马上下文串线修正（2026-10-27）

- 原会话中，“deadline is one month”只得到网球期限已改的文字，数据库仍保留 90 天；
  “yes”接受网球重新规划提议后，却细化了半马，因为提议没有目标绑定，执行器在
  `progress_target=None` 时会选第一个活跃目标。现在 `refine_steps` 提议和排期提议
  都绑定 goal_id/step_id，接受时重新校验目标仍活跃、步骤仍待办。
- 用户明确点名目标时，模型给出其他目标的步骤会被执行前的校验覆盖；在半马候选
  阶段说“I want to book tennis goal”会关闭旧提议并替换成网球候选。
- 没有期限的新目标先确认期限才生成草稿；草稿期限变化时重建计划。
  已建目标的期限修改写入数据库，旧候选清除。后台多轮回归覆盖原串线场景。
- 使用临时隔离数据库做真实 L1 后台复测：`I'd like to learn tennis` →
  `deadline is one month` → 创建并排期 → `yes` → `I want to book running goal` →
  `I want to book tennis goal`。草稿在期限回答后才出现；最后候选回到网球，
  数据库期限为 2026-11-26，跑步步骤未被细化，也没有意外日历写入。

## 目标优先级与首页现状（2026-10-27）

- Goal 新增 `pinned`，数据库版本升至 v4；旧库迁移前自动备份。活跃目标统一按
  **置顶优先 → 截止日升序（逾期自然靠前）→ 无截止日最后 → 创建时间** 排序，
  规则集中在 `goalcal/domain/goals.py::priority_key`。
- Goals 页目录、Chat 首页默认目标、无明确目标的下一步排期与 clarify 建议使用同一优先级；
  置顶开关位于 Goals 详情的 **Manage goal**。
- Chat 空首页已实现“左侧产品名 + 中间 slogan / 输入框 / 情境建议”三层结构，
  不再显示目标切换器、独立状态摘要或静态指引。Find 使用统一的 `Find time for my goal` 文案，
  多目标时先询问并等待用户选择，再进入对应目标的候选；Review 不再拼接下一事件摘要。
  首条消息或 Goals 页发起排期后进入现有消息流。契约见 `docs/DESIGN.md`。

## 首页多轮交互与排版修正（2026-10-27）

- 无明确步骤的排期通过未来目标块排除已排期步骤，优先寻找下一未排期 `todo`；
  预约后 New conversation 会推进到下一步，全部已排期则不显示 Find。全局 `next_step()` 语义不变。
- 候选阶段的具体钟点（如 `3pm`、`明天下午3点`、`改到3点`）绑定当前 step_id，
  不再落成 Untitled 普通日程；过去时间保留候选且不写库。明确的新日程命令仍走独立流程。
  `this weekend` 与现有周末筛选共用窗口。规则解析同时修复 bare `find time` 的残留 target。
- Start a new goal 使用显式 `goal_intake` 状态，不再发送固定 video 示例；Goals 页 New goal 复用同一入口。
- Review 查询从当前时间开始，按日期分组并输出 Markdown 列表。`chat_ui.message_markdown`
  统一保留所有消息的可见换行，L1 与普通回复提示词同步要求段落和列表格式。
- 交互矩阵在隔离数据库中实跑 40 个场景；修复后 AppTest 覆盖精确钟点、周末重筛、
  预约后首页、New goal 引导和 Review 格式。浏览器探索子任务因额度中断，最终浏览器验收由主 agent 完成。

## 1. 出发点：为什么要重做

旧设计是"手写状态机主干 + LLM 只在闲聊时润色"，实测暴露了它的天花板：

- **意图闭环靠手写覆盖**：resume 丢了、"don't change" 接不住、"好" 接不住 LLM 自己发出的提议——每支持一种说法都要写正则，边际成本递增，永远追不上自然语言。
- **fallback 是上下文盲**：LLM 静默失败后，用户面对的是不读上下文的模板（复读、推无关目标），而用户以为自己还在和"智能"对话。
- **判断点全是脆弱正则**：`looks_like_new_command` 这类"流程内回答 vs 新指令"的模糊判断不适合手写规则。
- **五层模式隐式优先级**：pending_action / goal_draft / candidates / event_draft / checkin 在 controller 里硬编码级联，用户不知道系统在听什么。

## 2. 新架构原则

- **LLM 优先，优雅降级**：LLM 是对话主干。调用失败时诚实降级——能执行的指令照做，聊不了就简短说明，不用机械话术伪装在线。"离线可用"的原始约束（M1 demo）已过期，真正的需求是服务商故障时的韧性。
- **模型提议，代码执行**：LLM 只输出结构化 `Action`，校验、去重、事务、确认流全是确定性代码。防的是幻觉，与断网无关，永远不变。
- **日期计算归 natdate**：LLM 只抄录时间片段，不做日期算术。README 铁律保留。
- **状态显式化**：对话状态存数据库、结构清晰、给模型看、模型错了能回滚。
- **接住先于建议**：先回应用户说的内容，最多附一个提议。

## 3. 一轮对话的管线

```
用户输入
  ├─ 零成本短路：按钮点击 / 与快捷回复逐字相同 → 直接映射动作（不调模型）
  └─ 其余一律进 L1 轮引擎（普通对话/路由轮次最多一次 L1 调用；显式规划可追加 L2）：
       Perceive  理解：意图 + 指代 + 情绪 + 流程内回答还是新指令（看 DialogContext 判断）
       Orient    话题定向：继续当前话题 / 迁移 / 接受某个未兑现提议
       Decide    路由：输出结构化 Action（路由是理解的副产品，不设独立路由模型）
       Act       确定性执行：校验、去重、事务、metrics（模型不碰数据库）
       Respond   回复：acknowledge → anchor → ≤1 offer，语言跟随用户
```

两个关键结构：

- **DialogContext（统一对话状态）**：`{kind, stage, subject, offers[], return_to 栈, clarify 计数}`，替代现在五种分散状态。任何时刻只有一个当前话题，打断可压栈恢复。
- **Offer Tracking（提议追踪）**：助手发出的每个提议（含 LLM 的）登记为带超时的 PendingOffer，"好/可以" 优先解析为对最近提议的接受——消灭"提议石沉大海"。

## 4. 模型分层

| 层 | 职责 | 时机 | 延迟预算 | 模型 |
|---|---|---|---|---|
| **L1 轮引擎** | 理解+路由+生成回复，单次调用出结构化 JSON | 每轮同步 | ≤2s | DeepSeek 官方 chat 模型 |
| **L2 规划器** | 建目标计划、拆步骤、replan | 按需，允许 loading | ≤10s | 前期同 L1；后期可升 reasoner 类 |
| **L3 记忆批处理** | 滚动压缩用户画像（常 miss 时段、精力模式、偏好） | 异步，每天或积累 N 条 | 不在用户路径 | chat 类即可 |

- **普通对话/路由轮次的 L1 调用固定为 0 或 1 次**；显式规划动作可同轮追加一次 L2，延迟和成本边界可预测。
- **判断层由 L1 兼任**："流程内回答 vs 新指令"交给 L1 结合 DialogContext 判断，删掉 `looks_like_new_command` 等正则。独立的 Jev 式决策模型暂不上——前置路由是纯增延迟；等成本或质量信号出现时，再以"前门级联"或"L1 输出事后校验"的形式引入，接口按层抽象进 ModelRouter，Jev / d1 / 自训小模型可互换。

## 5. 流畅度保障（前期硬指标）

1. 普通对话/路由轮次最多一次 L1；显式规划动作（建目标/改计划/replan）可同轮追加一次
   L2 并给 loading；L1 失败后的降级路径整轮不再调用任何 LLM（解析走规则，
   建目标/改草稿走确定性模板，`use_llm=False` 沿 handle→execute 传入所有含
   LLM 入口的 handler：plan 拆解、event_plan 上下文提取、refine 细化/续批）；
2. 当前 L1 `max_tokens=1000`（含模型推理 token），回复限 1–3 句；
3. 上下文装配包 ≤2k tokens（DialogContext + 最近 10 条消息 + 画像摘要 + 日程快照）；
4. system prompt 稳定前缀吃 context caching；
5. L1 超时 8s 单次不重试；
6. L2 慢操作给明确 loading 文案。

成本量级：每轮约人民币几厘，重度用户一天一毛多，前期不为省钱拆层。

## 6. 降级行为

- L1 失败 → `rules.py` 解析 + 简短诚实话术（规则从"并行主干"改身份为"降级路径 + 评测 baseline"）；
- L2 失败 → 模板计划兜底（已存在）；
- L3 失败 → 静默跳过，下批重试；
- L1 连续失败 N 次 → 熔断 5 分钟直接降级，避免每轮白等 8s。

## 7. 供应商灵活性

按层配置、全局兜底：`LLM_MODEL_TURN / _PLAN / _MEMORY`（含各自的 base_url/key），未配置回落到现有 `LLM_MODEL`。代码侧加 `ModelRouter`，现有配额、遥测、写事务保护原样生效并按层归因。换供应商只改环境变量。

## 8. 评测

- `rules.py` 保留为回归 baseline（新 prompt 版本至少要打得过它）；
- 新增**多轮对话评测集**（现在的 bench 全是单轮），覆盖：resume 闭环、提议接受、话题迁移、打断恢复、clarify 升级；
- 遥测按层出延迟/成本报表，流畅度持续可测。

## 9. 落地路线

1. **第一步（收益最大，已完成 2026-10-27）**：`ModelRouter` + L1 轮引擎 + DialogContext 统一——直接消灭 fallback 复读、resume 丢失、提议石沉大海；落地细节见文首"第一步落地情况"；
2. **第二步（已完成 2026-10-27）**：Offer Tracking 完整生命周期、clarify 升级策略、回复三段式 prompt 固化——落地细节见文首"第二步落地情况"；
3. **第三步（待实施）**：L2 规划器接新管线（含 refine.py 迁移 plan 层）、L3 画像回灌 `_propose` 排序；
4. **每步都有可验证的出口**：多轮评测集通过 + 全量 pytest + 浏览器实测。
