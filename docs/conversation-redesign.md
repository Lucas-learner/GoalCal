# 当前 Web 助手与历史对话层

新版入口为全局助手抽屉，客户端 `frontend/src/components/AssistantDrawer.tsx`，服务端 `goalcal/web/assistant.py` 与 `web/http.py`。使用 `WebThread` 保存线程和候选状态，刷新可恢复；显式清空及跨日且空闲至少 6 小时触发新线程。

理解继续复用 `llm/turn.py` 的 L1 和确定性规则，规划复用 L2。用户可在候选出现后继续输入、切换目标或修改时间；接受提议受资源版本与幂等约束，执行后可撤销。AI 只输出动作，数据变更由业务代码完成。

旧版 Chat/DialogContext 仍作为兼容与领域回归代码保留；其页面切换/刷新规则不能当作新版行为。
