# Chat / Goals 任务流验收记录（2026-10-25）

本轮接续暂停时的中间工作区：初次检查为 393 passed / 11 failed，旧测试仍假设输入目标即创建。未提交、未打标签；此前会话及其已存在改动均保留。

## 已完成

- Chat 新目标先澄清并生成草稿；预览不写 Goal/Step，确认后才创建。支持仅保存、创建并给第一步候选、取消、继续修订。
- 草稿带持久化 draft_id；Goal 唯一约束防重复创建，旧标签页确认会复用目标；创建失败由调用方事务整体回滚。
- 普通候选支持编号文字选择（中英文）和确定性时间窗口重筛；空结果、无效输入保留候选；明确的新事件命令照常处理。
- Chat 居中消息列、单一主任务卡、默认三个编号候选；修正过窄列导致按钮截断的问题，缩短聊天容器以留出输入区。
- Goals 多目标目录 + 当前详情；默认显示进度和下一步，已排期时间可见；All steps 折叠，编辑/取消/删除进入 More，调整计划/归档进入 Manage goal。
- 修正 Tomorrow 窗口缺失、归档时 replan_draft 为 None 的异常，以及对话状态在 UI 测试间串扰的问题。
- 文档更新；数据库迁移 v3 增加草稿键，迁移自动备份旧库。

## 验证结果

- `.venv/bin/pytest -q`：**422 passed**。
- 规则解析器 baseline：**85/88 = 96.6%**；保留原有 hard-03 / hard-04 / hard-07 三项失败，未扩大本轮范围处理。
- `git diff --check`：通过。
- 新测试覆盖模板/LLM 草稿、修订、取消、刷新恢复、重复确认、事务回滚、v2 迁移保留目标；候选按钮/文字一致性、时间重筛、无效输入、批量恢复；Goals 切换与归档。
- 真实浏览器隔离验收：使用临时数据库和自动登录测试入口，走通草稿预览 → Create & schedule → 输入“第二个” → Goals 显示次日 10:00 已安排；截图检查 Chat 候选和 Goals 布局。
- 普通真实浏览器登录表单显示正常；填写后点击受密码管理器扩展弹层阻挡，重试仍受阻。因此不能声称真实浏览器登录表单提交已通过。登录及其后业务流程另有 AppTest 覆盖。
- 正式 launchd 应用已重启；8501 `/_stcore/health` 和现有 ngrok 地址均返回 200，正式浏览器登录页无报错；生产库 schema version=3，draft_id 字段存在。
- 临时 8511 / 8512 服务及本轮浏览器标签已清理；隧道未重启。

## 边界

- 草稿创建在 LLM 不可用时回退模板；自由文字修订需要 LLM，失败时保留原预览并提示。
- 本轮未加入长期自然语言记忆、行为学习或 Apple/Google 双向同步；未执行在线 LLM 提供商的付费请求验收。

## 后续修复：owner 已有数据报错

- 实际生产报错为 `no such column: steps.draft_id`：草稿键误加到了 Step ORM，旧库只迁移了 Goal；之前由模型自动建表的测试未发现差异。
- 移除 Step 的草稿键，Goal 的防重复创建机制保留；不改 owner 密码、不删除历史数据，不增加补丁列来掩盖模型错误。
- 登录前先注册统一导航，文件路由同时校验登录并初始化会话，防止旧链接/直接访问 Calendar 出现缺少 tz 的异常。
- 用真实 v2 steps schema 测试迁移后的租户读取和排期接受；补三个未登录子页面回归。全量 **427 passed**，diff check 通过；另覆盖旧 Calendar 链接登录后恢复统一导航和 Goals 排期。
- owner 的生产目标及已有步骤只读查询正常；用生产库的临时隔离副本验证当前 pending 候选接受成功，未在正式数据上执行排期。
- 应用已再次重启，健康检查和公网返回 200；真实浏览器直接访问 /calendar 正常显示登录页。

## 后续修复：登录页导航与重复登录

- 将文件路由从 Streamlit 自动发现的 `pages/` 移至 `routes/`，所有页面统一经过 app.py 的登录和导航初始化；移除子页面重复登录/跳转入口。
- 未登录或会话失效时统一导航设为 hidden，登录成功后才显示 sidebar；不再提前出现功能导航和侧栏展开按钮。
- 加强三个子页面登录回归：只调用一次登录、随后不显示登录表单、rerun 保持同一 token、进入 Goals 可继续排期。全量 **429 passed**；diff check 通过。
- 隔离测试服务真实浏览器截图确认登录页没有侧栏/展开按钮；表单提交仍遇密码管理器扩展弹层，未声称浏览器登录提交全流程通过。

## 后续改进：日历跨页面持续挂载

- 经评估，等待主要在切页后的日历组件重建。已将日历放入登录后 app.py 的共享容器，其他页面将容器移到屏外但保持挂载；仅 Calendar 页面显示与处理可见交互。
- 真实浏览器检查：Chat 登录初始已有日历 iframe；Calendar → Goals → Chat → Calendar 始终为同一个 iframe，切到下一周后返回仍停留在下一周。
- 每次运行读取当前可见范围内事件；AppTest 验证在 Chat 新增日程后 Calendar 获取最新事件。
- 代价是首次进入应用时预先加载日历，登录首屏会多做一次日历查询和组件初始化。

## 后续调整：移除 Chat 常驻状态按钮

- 按用户确认，仅移除 Chat 输入框上方的 `Tired`、`Busy`、`Stuck` 三个常驻按钮；本次不新增替代入口，也不改状态文本解析或签到中的原因选项。
- 原因：常驻按钮缺少明确的目标/步骤语境，现有回复容易成为未落实到排期的套话。具体交互方案留待后续讨论。
- `.venv/bin/pytest -q tests/test_app_smoke.py`：**46 passed**；`git diff --check` 通过。

## 后续收尾：减少重复反馈并明确 Goals 操作

- Chat 预订单个或批量候选、恢复候选时，成功结果只保留一条聊天消息，不再同时弹同文案 toast；从 Goals 跳到 Chat 找时间时也不再重复提示。
- Goals 步骤高频按钮由单独的图标改成 `Find time` / `Reschedule` / `Complete` 文字标签；`More` 内的高级操作不变。
- AppTest 覆盖文字与按钮预订后仅出现一条成功消息、没有同文案 toast，以及已排期步骤显示 `Reschedule`。全量 `.venv/bin/pytest -q`：**431 passed**；`git diff --check` 通过。
- 临时数据库上的真实浏览器验收：密码管理器弹层最初阻挡输入；关闭旧测试标签后，普通登录表单成功提交，进入 Chat 且切到 Goals 无需再次登录。硬刷新会回到登录页；重新登录后聊天和已排期步骤恢复，此处仍有登录会话跨硬刷新的限制。
- 隔离自动登录入口走通草稿预览 → Create & schedule → 输入 `second` 预订次日 10:00 → 刷新恢复 → Goals 显示 `Reschedule` 与已排期时间 → Calendar 显示同一事件。Calendar 切到下一周、离开再返回后仍停在该周；预订成功只显示一条聊天消息，没有同文案 toast。
- 离线模式下尝试用文字修改草稿截止日会提示预览未变，原草稿得以保留；自由文字修订仍依赖 LLM，属于本轮未扩展的既有边界。

## 后续修复：硬刷新登录与离线截止日修改（2026-10-26）

- 截止日明确且仅修改截止日时，草稿直接更新 `brief.deadline` 与预览日期，沿用原草稿 ID、阶段和步骤；混合“改步骤”等要求而 LLM 不可用时保持原预览，不做误导性的部分修改。任意步骤内容的自由文字重写仍依赖 LLM。
- 公网入口 `127.0.0.1:8501` 改为同源登录网关，Streamlit 内部监听 `127.0.0.1:8502`；网关设置 12 小时可撤销的 `HttpOnly`、`SameSite=Lax` 会话 Cookie，公网 HTTPS 下加 `Secure`。Cookie 不写入 URL 或浏览器脚本存储；登录限流、邀请码注册、退出及账号停用继续使用现有账号服务。
- 隔离浏览器验收：登录一次后硬刷新仍在 Chat；输入 `Make the deadline 45 days` 后预览从 11 月 24 日改为 12 月 9 日，硬刷新后草稿及日期仍在；退出后返回登录页。另用隔离账号验证禁用后旧 Cookie 无法访问页面。
- 网关测试覆盖 Cookie 属性、同源表单检查、退出撤销、禁用失效和邀请码注册；完整 `.venv/bin/pytest -q`：**436 passed**。隔离启动器验证 8512 网关与 8513 Streamlit 同时启动、同时停止。
- 切换正式服务前已做数据库在线备份；正式 8501 网关和内部 8502 健康检查均为 200，公网 HTTPS 登录页返回 200，公网同源登录表单检查正常。没有使用 owner 密码或修改 owner 业务数据。切换前已打开的旧标签需要刷新并重新登录一次，之后硬刷新可恢复登录。

## 后续修复：英文登录页与公网表单提交（2026-10-26）

- 登录网关原先将浏览器 `Origin` 协议与应用在反向代理后看到的协议直接比较，公网 HTTPS 请求可能被误判为 `Invalid form submission`；此前的公网 curl 检查不能代表真实浏览器提交。
- 登录、邀请码注册和退出表单改用页面生成的随机表单令牌与 HttpOnly、SameSite=Strict Cookie 校验；仍限制表单大小。页面、字段、成功与认证错误提示改为英文，Streamlit 直接入口也同步改为英文。
- 全量 `.venv/bin/pytest -q`：**436 passed**；`git diff --check` 通过。测试覆盖缺少或错误令牌被拒、无 `Origin` 的合法登录、注册与退出。
- 正式服务重启后，8501 网关、8502 内部 Streamlit 和公网 HTTPS 均返回 200；公网真实浏览器填写无效测试凭据并提交，显示英文 `Incorrect username or password.`，不再显示 `Invalid form submission`。未使用 owner 密码，也未改动 owner 业务数据。

## 后续修复：登录后页面持续加载（2026-10-26）

- 正式日志显示登录后 WebSocket 被内部 Streamlit 连续拒绝：公网 `Origin` 与内部 `Host=127.0.0.1:8502` 不匹配；HTTP 健康检查与登录提交成功均未覆盖这个阶段。
- 网关先校验浏览器 WebSocket 来源与公网 Host 一致，再将连接以内部服务自己的 `Origin` 发给 Streamlit；未放宽内部 Streamlit 的来源检查。
- 新增公网来源校验及内部转发回归测试；全量 `.venv/bin/pytest -q`：**437 passed**，`git diff --check` 通过。正式 8501/8502 已重启并健康；隔离测试账号的真实浏览器从登录页进入 Chat，看到导航和聊天输入框。测试账号及隔离服务已清理，未使用 owner 密码。

## 交接状态（2026-10-26）

- 当前工作区基于 `90282c9 chore: snapshot v0 baseline`，本阶段改动仍未提交或打新 tag；`git status` 有大量已修改和未跟踪文件，包括本验证文档。接手时先查看工作区，勿以 v0 快照覆盖这些改动。
- 自检：全量 `.venv/bin/pytest -q` **437 passed**；规则解析基线 **85/88**，仅 hard-03 / hard-04 / hard-07 三项已知失败；`git diff --check` 通过。8501 网关、8502 Streamlit 健康接口、公网 ngrok 首页均返回 200。8512/8513 隔离服务与测试浏览器标签已清理。
- 生产入口为 `https://disciplelike-preoccupiedly-kay.ngrok-free.dev/`；8501 是认证网关，8502 是仅本机监听的 Streamlit。应用由 `local.goalcal.app` launchd 维持，隧道由 `local.goalcal.tunnel` 维持；运行日志在 `var/logs/app.log` 和 `var/logs/application.log`。重启应用可用 `launchctl kickstart -k gui/$(id -u)/local.goalcal.app`，不需重启隧道。
- 最近故障链：公网登录表单误报 `Invalid form submission` 已改为表单令牌校验；登录后持续加载是网关转发 WebSocket 时公网 Origin 与内部 Host 不符，现已校验外部来源并重写内部 Origin。隔离真实浏览器完成登录到 Chat；**owner 在公网的最终登录体验仍需由 owner 本人复试**，此前没有获取或使用其密码。HTTP 200 只能证明入口在线，不能单独证明登录后的 WebSocket 和页面可用。
- 若 owner 仍反馈加载问题，先查看最新 `var/logs/app.log` 中的 WebSocket 拒绝或应用异常，再用隔离账号走登录 → Chat，不要修改 owner 数据或在日志中写入凭据。当前服务已运行修复后的代码。
- 本阶段产品功能已完成 Chat 草稿确认、候选文字操作及 Chat/Goals 页面精简；离线只支持明确的截止日草稿修订，自由文字重写步骤仍依赖 LLM。自然语言长期记忆、行为学习及 Apple/Google 双向同步未纳入本阶段。下一步优先由 owner 验收公网登录与核心路径，之后再决定是否整理提交及版本标签；不得擅自发布 v0.1。

## 独立安全回归（2026-10-26）

- 独立审查发现并用真实规则解析复现两类误操作：签到追问中输入 `I finished New step` 会误完成旧步骤；`delete goal "Learn piano"` / `delete "Team lunch"` 可能模糊匹配并删除名称不同的目标/日程。
- 带明确步骤名的进度汇报现在退出旧候选或签到上下文并交由正常进度处理；`Done` 等无目标签到回复保持原行为。目标删除不再使用相似度兜底，日程删除要求标题精确匹配；移动日程仍支持模糊查找。
- 新增三项安全回归，相关测试 120 项通过，完整 `.venv/bin/pytest -q`：**440 passed**；规则解析基线仍为 **85/88**，仅原有 hard-03 / hard-04 / hard-07；`git diff --check` 通过。未提交或新增版本标签。

## Calendar 跨页视图保持修复（2026-10-26）

- owner 复试确认核心链路通过，但 Calendar 切到其他页面再返回会重置周视图。原因是标题容器和隐藏 CSS 随页面增删，改变组件在 Streamlit 中的位置；稳定 key 本身不足以阻止 iframe 重建。
- 样式节点现在始终渲染，标题放入固定容器，仅切换内容和可见性；结构回归覆盖跨 Chat / Goals / Calendar 的稳定 key、标题容器和样式节点。
- 全量 `.venv/bin/pytest -q`：**440 passed**。隔离账号真实浏览器从 `Oct 26–Nov 1` 切到 `Nov 2–8`，再执行 Calendar → Chat → Calendar，iframe 标记与 `Nov 2–8` 标题均保留，确认未重建组件。未使用 owner 凭据或业务数据。

## 并行推进：解析、行为记忆与外部日历准备（2026-10-26）

- 修复口语改期 `Can we do ... instead`、查询 `what's my day look like` 和英文 `next <weekday>` 的下一日历周语义；规则基线从 **85/88** 提升到 **88/88 = 100%**，原有用例未回归。
- `memory.py` 增加只读行为学习：至少 5 个已解决目标块，最佳时段得分至少 3 且占总得分 2/3，才补充 morning/afternoon/evening 中的一个优选时段；done=1、partial=0.5、missed=0。显式偏好始终覆盖，样本不足或分散不学习，不在读取时写库。
- Settings 展示历史样本数和优势时段，提供 `Learn preferred windows from goal history` 开关；关闭后恢复默认，重新开启可再次使用历史，重置偏好会同时重置开关。覆盖学习阈值、时区、租户隔离、显式覆盖、开关和 UI 保存。
- 外部日历完成技术准备但未声称已接通：Google 首切片应采用单主日历、OAuth `calendar.events`、events.list 增量 sync token、patch/ETag 冲突处理、5–15 分钟轮询和事务 outbox；先补 connection/event_link/outbox 与 Event 修改时间、软删除、busy 语义，再接真实 API。Apple 公网路径是 iCloud CalDAV + 用户专用密码，macOS EventKit 只适用于本机原生 helper，两者不与 Google 首切片捆绑。
- 官方依据：[Google 增量同步](https://developers.google.com/workspace/calendar/api/guides/sync)、[Google OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)、[Apple EventKit](https://developer.apple.com/documentation/eventkit)、[Apple 专用密码](https://support.apple.com/en-us/102654)。未新增 SDK、未配置凭据、未访问真实外部日历。
- 完整 `.venv/bin/pytest -q`：**462 passed**；`git diff --check` 通过。README、技术方案和接口契约已同步。仍未提交或新增版本标签。
