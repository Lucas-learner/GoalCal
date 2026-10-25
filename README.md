> 邀请用户、自动诊断与反馈：[试用说明](docs/trial.md)。

> 当前试用入口及免费额度：[ngrok 部署说明](docs/ngrok.md)。

> 公网多用户版本：请先阅读 [本机部署与维护手册](docs/production.md)。应用现在要求登录，不再自动填充 demo 数据；下方原型说明中的时间机器和整库重置不再适用于服务界面。

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
| 目标 → 叶子任务 | ✅ | 滚动拆解：LLM 生成 3–5 阶段大纲 + 首批 2–3 步，做完前自动补下一批；失败才回退模板；步骤可增删 |
| 目标澄清 | ✅ | 只在缺截止日时追问一轮；水平按默认值，第一次签到时顺带校正 |
| 进度追踪（按阶段） | ✅ | 已完成阶段数 + 当前阶段完成比例；自然语言回报完成 |
| 状态感知排期（tired / busy / uninspired） | ✅ | 确定性打分引擎，可解释 |
| 候选时段 → 一键写入日历 | ✅ | 悬浮对话框（Suggested slots）：6 个候选按天分组 + 匹配理由，窗口过滤下拉（Any time / Today PM / Tonight / Tomorrow / This weekend） |
| 时间窗口 / 批量 / 定时点排期 | ✅ | “安排写脚本到今天下午”按窗口挖空档；“今天下午有空，把任务都排了”一次装箱所有目标；“…今天下午3点”直接定 |
| 过期块识别 + 对话签到 | ✅ | 打开页面 / 时间机器快进后助手主动问“How did X go?”；快捷回复 Done / Partly / Didn't do it + 四种原因 |
| 四种恢复动作 | ✅ | 太累→换低精力步骤或减半；没时间→15 分钟启动块；卡住了→细化出 5 分钟启动动作；不想做了→延后 7/14 天 / 暂停 / 归档 |
| 核心指标 | ✅ | 建议接受率 / 72h 恢复率 / 推进率，侧边栏展示（时间机器口径） |
| 目标卡直接操作 | ✅ | 找时间 / 完成 / 取消安排 / 增删步骤 / 归档目标 |
| 日历可视化 / 拖拽改期 | ✅ | 点击事件可删除（二次确认）、点空白新建 |
| 时间机器（demo 快进） | ✅ | 侧边栏 0–14 天 |
| LLM 意图解析 | ✅ | LLM 优先，失败自动回落规则；`.env` 可关 |
| LLM 目标拆解 | ✅ | 按具体目标生成阶段大纲 + 首批步骤；结构校验失败才回退模板 |
| 计划可管理 | ✅ | 步骤可直接编辑；AI 修改先预览，确认后才替换，不是反复抽卡 |
| 主动追问过期块（对话内签到 + 恢复） | ✅ | 推送通知本身 ⏳（11 月） |
| ICS 导入 | ✅ | 侧边栏折叠面板 “Import my calendar (.ics)”：文件上传或订阅 URL，按 UID 去重，DAILY/WEEKLY 重复事件展开 8 周 |
| 多用户 / 登录 | ⏳ | 11 月 |

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
- 主区是三栏：左栏对话（消息 + 快捷回复），中栏日历，右栏 **Goals** 卡片；候选时段统一走悬浮对话框（Suggested slots），不占栏内空间
- 签到/目标澄清时，对话下方会出现快捷回复按钮（Done / Partly / Too tired / Reschedule all 等），点一下等于输入
- 说“我要学会弹钢琴”这类复杂目标，会先确认周期，再生成专属计划
- 已有目标如果拆得不合适，点 **Adjust plan with AI**，说明要改什么 → 预览 → 确认应用；也可以直接点步骤旁的 **✏️** 手动改标题/时长/阶段
- 步骤行上的按钮是纯图标，悬浮显示说明：**📅** 找时间（已排则变 **🔄** 改期）/ **✅** 完成 / **✕** 取消块 / **✏️** 编辑 / **🗑** 删除
- 多于一个目标时，Goals 列顶部会出现目录下拉，可跳转/过滤到单个目标
- 完成步骤时：已到时间的块标 done 保留在日历上作完成记录，只有未来块才取消；块按状态着色（done 灰绿 / partial 琥珀 / missed 浅灰）
- 展开 **All steps** 可添加自定义步骤，或删除某一步
- 新建目标时可选择“AI 自动拆解”或“空白目标”，不强制 10 步；计划不是一次性结果，之后可逐条编辑
- **Archive goal** 归档整个目标（会撤掉它的未来日历块）
- 日历上点事件 → 弹出详情对话框 → 二次确认删除
- 日历上点空白 → 弹出新建事件对话框；拖拽 → 改期

## 目录结构

```
GoalCal/
├── app.py                  # Streamlit 入口，只做编排
├── goalcal/
│   ├── config.py           # 环境变量
│   ├── models.py           # 表结构（目标/步骤/事件/打卡/状态）
│   ├── db.py               # 引擎与会话
│   ├── timeutil.py         # 时区约定：DB 存 UTC，计算用本地
│   ├── metrics.py          # 埋点 + 三个核心指标（接受率/72h 恢复率/推进率）
│   ├── agent.py            # 解析 → 执行 → 回复（唯一写库入口）
│   ├── seed.py             # demo 数据
│   ├── llm/
│   │   ├── action.py       # 唯一允许的 LLM 输出结构
│   │   ├── natdate.py      # 确定性日期/时间窗口解析（关键）
│   │   ├── rules.py        # 规则解析器（baseline + 兜底）
│   │   ├── parser.py       # LLM 优先 → 规则兜底
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
│   └── ui/                 # 只负责渲染
├── bench/                  # 解析评测：88 条用例 + runner
├── tests/                  # 297 个单测（规则/排期/执行层/签到恢复/ICS/时区/DB/UI smoke）
└── docs/
    ├── 技术落地方案.md
    └── 接口契约.md
```

## 测试与评测

```bash
pytest -q                                   # 297 passed
python bench/run_eval.py --parser rules     # 基线：85/88 = 96.6%
python bench/run_eval.py --parser both --out bench/results.csv
```

测试使用独立的临时 SQLite（`tests/conftest.py`），不会写进开发用的 `goalcal.db`。

移交注意：`.env` 和 `goalcal.db` 都在 `.gitignore` 里、不会进仓库——接手后按 `cp .env.example .env` 重建密钥文件，首次运行会自动建库。

bench 仍保留 3 条已知失败（`hard-03`、`hard-04`、`hard-07`：口语改期/查询、"next Friday" 的歧义），
正是中期报告第 3 节要的“预期失败案例”。

## 三条铁律

1. **LLM 不直接改数据。** 它只输出 `Action` 或 `Plan`（JSON schema），校验和执行由 `agent.py` 完成。
2. **日期不用 LLM 算。** `natdate.py` 负责相对日期、星期、中文说法；LLM 只判 intent、拆步骤，
   并把原文里的时间片段抄进 `when_text`；`natdate` 解析失败才采用 LLM 给的时间，且置信度降一档。
3. **日程是本地时间，库是 UTC。** 只在 `timeutil.py` 的三个边界转换。
