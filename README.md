> 邀请用户、自动诊断与反馈：[试用说明](docs/trial.md)。

> 当前试用入口及免费额度：[ngrok 部署说明](docs/ngrok.md)。

> 公网多用户版本：请先阅读 [本机部署与维护手册](docs/production.md)。应用现在要求登录，不再自动填充 demo 数据；下方原型说明中的时间机器和整库重置不再适用于服务界面。

# GoalCal

> A calendar that advances your goals, not just your meetings.
> IBA6316 / MDS5370 · L01 Group 12

用户用自然语言说出长期目标，GoalCal 把它拆成可执行的小步，结合日历空档和当下状态推荐时段，
在被打断后不堆积、不惩罚地把目标重新接上。

当前 L1 轮引擎、DialogContext、提议生命周期与降级策略见[对话层重设计](docs/conversation-redesign.md)。

---

## 现在能跑什么（M1）

| 能力 | 状态 | 说明 |
|---|---|---|
| 对话增 / 改 / 删 / 查日程 | ✅ | 规则解析器，无需 API key |
| 目标 → 叶子任务 | ✅ | Chat 先预览草稿，确认才创建；滚动拆解：LLM 生成 3–5 阶段大纲 + 首批 2–3 步，做完前自动补下一批；失败才回退模板；步骤可增删 |
| 目标澄清 | ✅ | 只在缺截止日时追问一轮；水平按默认值，第一次签到时顺带校正 |
| 进度追踪（按阶段） | ✅ | 已完成阶段数 + 当前阶段完成比例；自然语言回报完成 |
| 状态感知排期（tired / busy / uninspired） | ✅ | 确定性打分引擎，可解释 |
| 候选时段 → 一键写入日历 | ✅ | Chat 内联卡片：前 3 个候选 + 更多展开 + 窗口过滤（Any time / Today PM / Tonight / Tomorrow / This weekend） |
| 时间窗口 / 批量 / 定时点排期 | ✅ | “安排写脚本到今天下午”按窗口挖空档；“今天下午有空，把任务都排了”一次装箱所有目标；“…今天下午3点”直接定 |
| 过期块识别 + 对话签到 | ✅ | 打开页面后助手主动问“How did X go?”；快捷回复 Done / Partly / Didn't do it + 四种原因 |
| 四种恢复动作 | ✅ | 太累→换低精力步骤或减半；没时间→15 分钟启动块；卡住了→细化出 5 分钟启动动作；不想做了→延后 7/14 天 / 暂停 / 归档 |
| 对话与偏好记忆 | ✅ | 应用内切页保留消息和待处理上下文；整页重进开启新对话，目标与日历不受影响；明确排期偏好可查看、修改、重置并影响候选 |
| 核心指标 | ✅ | 建议接受率 / 72h 恢复率 / 推进率，Settings 展示 |
| 目标卡直接操作 | ✅ | 找时间 / 完成 / 取消安排 / 增删步骤 / 归档目标 |
| 日历可视化 / 拖拽改期 | ✅ | 点击事件可删除（二次确认）、点空白新建 |
| LLM 意图解析 | ✅ | LLM 优先，失败自动回落规则；`.env` 可关 |
| LLM 目标拆解 | ✅ | 按具体目标生成阶段大纲 + 首批步骤；结构校验失败才回退模板 |
| 计划可管理 | ✅ | 步骤可直接编辑；AI 修改先预览，确认后才替换，不是反复抽卡 |
| 主动追问过期块（对话内签到 + 恢复） | ✅ | 推送通知本身 ⏳（11 月） |
| ICS 导入 | ✅ | Settings 中文件上传或订阅 URL，按 UID 去重，DAILY/WEEKLY 重复事件展开 8 周；单向导入 |
| 多用户 / 登录 | ✅ | 用户名密码、邀请码注册、租户隔离 |

## 快速开始

```bash
cd /Users/lucas/projects/GoalCal

# 首次：创建环境（Python 3.12）
conda create -y -n goalcal python=3.12
conda activate goalcal
pip install -r requirements.txt

cp .env.example .env        # 不填 key 也能跑（规则解析）
python -m goalcal.service app
```

打开 http://localhost:8501，登录后进入 Chat；账号初始化见上方部署与维护手册。

### 试试这几句

```
I want to publish my first short video in 30 days
我要学会弹钢琴
Add a meeting tomorrow 10am
Move tomorrow's workout to Friday
I'm tired
I finished the script today
What do I have this week?
帮我安排下一步
delete "track my goal"
```

也可以不用打字：

- 界面为英文，但解析器是中英双语的——用中文输入照常工作（如“我要学会弹钢琴”“帮我安排下一步”）
- 默认首页是居中的 **Chat**；空首页只有固定问题、输入框和 2–3 条情境建议，不重复展示 dashboard；**Goals / Calendar / Settings** 是独立子页面
- **Find time for my goal** 在多目标时先询问目标，回答后进入该目标的下一未排期步骤；已排期步骤不重复推荐。日程建议执行真实近期查询并按日期分组；首条消息后进入消息流
- 候选时段以卡片内联在对话中，默认显示前 3 个，可展开更多；从 Goals 找时间也会回到 Chat 选择
- 签到/目标澄清时，对话下方会出现快捷回复按钮（Done / Partly / Too tired / Reschedule all 等），点一下等于输入
- 应用内切页会保留对话、目标澄清、目标草稿、待选候选和签到上下文；整页重进、新浏览器会话或 **New conversation** 会开启新对话并清空这些临时状态，目标与日历业务数据不受影响
- 新目标缺少期限时先确认期限，再生成草稿；草稿支持继续输入调整。**Create & schedule** 创建并给第一步候选，**Save goal** 仅保存，**Cancel** 不创建；重复确认同一草稿不会重复建目标
- 可输入“第二个 / 2 / second”选择候选，或“改到明晚 / 周四晚上 / this weekend”重筛当前步骤；直接回复“明天下午3点”会安排当前步骤，不会新建 Untitled 日程；无效输入或无空档会保留原候选
- **Goals** 按“置顶 → 截止日 → 创建时间”排序，多目标时显示紧凑目录和当前详情；默认突出进度与下一步，**All steps** 默认折叠；**Manage goal** 内可置顶、调整计划或归档
- 步骤常用操作为 **Find time**（已排则变 **Reschedule**）和 **Complete**；**More** 内可取消块、编辑和确认删除
- **Start a new goal** 与 Goals 页的 **New goal** 都会先在 Chat 询问目标；只有用户随后输入的内容会进入建目标流程，不会自动填写示例
- **Settings** 顶部显示当前账号、时区和 **Log out**；也可设置工作时间、每日目标上限、缓冲和优选时段。有足够完成历史时会保守学习优势时段，可查看依据或关闭学习，显式偏好始终优先
- ICS 导入、反馈和 7 天指标也已移到 Settings；ICS 仍是单向导入，不是 Apple/Google 双向同步
- **Archive goal** 归档整个目标（会撤掉它的未来日历块）
- 登录后日历组件持续挂载，切页保留当前日期和视图；每次运行读取可见范围内的最新事件。Calendar 上点事件 → 弹出详情对话框 → 二次确认删除；点空白新建，拖拽改期

## 目录结构

```
GoalCal/
├── app.py                  # 启动、登录与四个页面的导航
├── routes/                  # Goals / Calendar / Settings 文件路由；Chat 为默认页
├── goalcal/
│   ├── config.py           # 环境变量
│   ├── models.py           # 目标/事件/对话/偏好等表结构
│   ├── db.py               # 引擎与会话
│   ├── timeutil.py         # 时区约定：DB 存 UTC，计算用本地
│   ├── metrics.py          # 埋点 + 三个核心指标（接受率/72h 恢复率/推进率）
│   ├── agent.py            # 白名单动作执行与确定性业务编排
│   ├── dialog.py           # DialogContext、提议生命周期与打断恢复
│   ├── conversation.py     # 对话与待处理上下文的持久化/重置
│   ├── memory.py           # 明确偏好、可关闭的行为学习与排期物化
│   ├── seed.py             # demo 数据
│   ├── llm/
│   │   ├── action.py       # 理解层与执行层之间的 Action 结构
│   │   ├── turn.py         # L1 轮引擎：TurnDecision → Action
│   │   ├── router.py       # turn / plan / memory 分层模型客户端
│   │   ├── natdate.py      # 确定性日期/时间窗口解析（关键）
│   │   ├── rules.py        # 规则解析器（baseline + 兜底）
│   │   ├── parser.py       # 兼容解析入口与规则兜底
│   │   ├── plan.py         # LLM 目标拆解 + 结构校验
│   │   └── prompts.py      # prompt 版本化
│   ├── domain/
│   │   ├── scheduler.py    # 排期打分引擎（纯函数，可单测）
│   │   ├── planner.py      # 目标复杂度判断 / 澄清 / 默认假设
│   │   ├── goals.py        # 拆解 / 进度 / 下一步
│   │   ├── recovery.py     # 过期块识别 / 签到落库
│   │   ├── refine.py       # 滚动细化（剩余步骤 <2 自动续批）
│   │   ├── ics_import.py   # ICS 导入（UID 去重，重复事件展开 8 周）
│   │   └── calendar_service.py
│   ├── data/goal_templates.py
│   └── ui/                 # 页面渲染、共享控制器与运行时
├── bench/                  # 解析评测：88 条用例 + runner
├── tests/                  # 规则/排期/执行/恢复/安全/记忆/迁移/UI smoke
└── docs/
    ├── 技术落地方案.md
    └── 接口契约.md
```

## 测试与评测

```bash
pytest -q
GOALCAL_PRODUCTION=false python bench/run_eval.py --parser rules  # 基线：88/88 = 100%
GOALCAL_PRODUCTION=false python bench/run_eval.py --parser both --out bench/results.csv
```

测试使用独立的临时 SQLite（`tests/conftest.py`），不会写进开发用的 `goalcal.db`。

移交注意：`.env` 和 `goalcal.db` 都在 `.gitignore` 里、不会进仓库——接手后按 `cp .env.example .env` 重建密钥文件，首次运行会自动建库。

规则基线覆盖口语改期、日程查询和英文 `next <weekday>` 的下一日历周语义；新增表达应连同回归用例一起加入。

## 三条铁律

1. **LLM 不直接改数据。** 它只输出 `Action` 或 `Plan`（JSON schema），校验和执行由 `agent.py` 完成。
2. **日期不用 LLM 算。** `natdate.py` 负责相对日期、星期、中文说法；LLM 只判 intent、拆步骤，
   并把原文里的时间片段抄进 `when_text`；`natdate` 解析失败才采用 LLM 给的时间，且置信度降一档。
3. **日程是本地时间，库是 UTC。** 只在 `timeutil.py` 的三个边界转换。
