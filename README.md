# GoalCal

> A calendar that advances your goals, not just your meetings.
> IBA6316 / MDS5370 · L01 Group 12

用户用自然语言说出长期目标，GoalCal 把它拆成可执行的小步，结合日历空档和当下状态推荐时段，
在被打断后不堆积、不惩罚地把目标重新接上。

---

## 现在能跑什么（M1）

| 能力 | 状态 | 说明 |
|---|---|---|
| 对话增 / 改 / 删 / 查日程 | ✅ | 规则解析器，无需 API key |
| 目标 → 10 个叶子任务 | ✅ | 模板库 + 里程碑，可替换为 LLM + RAG |
| 进度追踪（按预估时长加权） | ✅ | 自然语言回报完成 |
| 状态感知排期（tired / busy / uninspired） | ✅ | 确定性打分引擎，可解释 |
| 候选时段 → 一键写入日历 | ✅ | 3 个候选卡片 + 匹配理由 |
| 日历可视化 / 拖拽改期 | ✅ | streamlit-calendar（FullCalendar） |
| 时间机器（demo 快进） | ✅ | 侧边栏 0–14 天 |
| LLM 意图解析 | ⏳ | 代码已接好，填 `.env` 即启用 |
| 主动推送 / 自动重排 | ⏳ | 下一步（F4） |
| 多用户 / 登录 / ICS 导入 | ⏳ | 11 月 |

## 快速开始

```bash
cd /Users/lucas/projects/GoalCal

# 首次：创建环境（Python 3.12）
conda create -y -n goalcal python=3.12
conda activate goalcal
pip install -r requirements.txt

cp .env.example .env        # 不填 key 也能跑（规则解析）
streamlit run app.py
```

打开 http://localhost:8501 。侧边栏可以重置 demo 数据、快进时间。

### 试试这几句

```
I want to publish my first short video in 30 days
Add a meeting tomorrow 10am
Move tomorrow's workout to Friday
I'm tired
I finished the script today
What do I have this week?
```

## 目录结构

```
GoalCal/
├── app.py                  # Streamlit 入口，只做编排
├── goalcal/
│   ├── config.py           # 环境变量
│   ├── models.py           # 表结构（目标/步骤/事件/打卡/状态）
│   ├── db.py               # 引擎与会话
│   ├── timeutil.py         # 时区约定：DB 存 UTC，计算用本地
│   ├── agent.py            # 解析 → 执行 → 回复（唯一写库入口）
│   ├── seed.py             # demo 数据
│   ├── llm/
│   │   ├── action.py       # 唯一允许的 LLM 输出结构
│   │   ├── natdate.py      # 确定性日期解析（关键）
│   │   ├── rules.py        # 规则解析器（baseline + 兜底）
│   │   ├── parser.py       # LLM 优先 → 规则兜底
│   │   └── prompts.py      # prompt 版本化
│   ├── domain/
│   │   ├── scheduler.py    # 排期打分引擎（纯函数，可单测）
│   │   ├── goals.py        # 拆解 / 进度 / 下一步
│   │   └── calendar_service.py
│   ├── data/goal_templates.py
│   └── ui/                 # 只负责渲染
├── bench/                  # 解析评测：46 条用例 + runner
├── tests/                  # 19 个单测
└── docs/
    ├── 技术落地方案.md
    └── 接口契约.md
```

## 测试与评测

```bash
pytest -q                                   # 19 passed
python bench/run_eval.py --parser rules     # 基线：41/46 = 89.1%
python bench/run_eval.py --parser both --out bench/results.csv
```

当前基线故意跑不满分：`hard-01`～`hard-04`、`hard-07` 是已知失败，
正是中期报告第 3 节要的“预期失败案例”。

## 三条铁律

1. **LLM 不直接改数据。** 它只输出 `Action`（JSON schema），执行由 `agent.py` 完成。
2. **日期不用 LLM 算。** `natdate.py` 负责相对日期、星期、中文说法；LLM 只判 intent。
3. **日程是本地时间，库是 UTC。** 只在 `timeutil.py` 的三个边界转换。
