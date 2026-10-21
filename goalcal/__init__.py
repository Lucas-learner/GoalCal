"""GoalCal — goal-advancing AI calendar.

分层约定（很重要，改动前先读 docs/接口契约.md）：
    llm/      只负责“理解”，输出结构化 Action，不直接碰数据库
    domain/   只负责“决策与执行”，纯 Python，可单测，可离线跑
    ui/       只负责渲染，业务判断一律下沉到 domain/
"""

__version__ = "0.1.0"
