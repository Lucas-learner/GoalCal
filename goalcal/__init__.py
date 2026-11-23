"""GoalCal — goal-advancing AI calendar.

分层约定（很重要，改动前先读 docs/接口契约.md）：
    llm/      只负责“理解”，输出结构化 Action，不直接碰数据库
    domain/   只负责“决策与执行”，纯 Python，可单测，可离线跑
    web/      Web API、事务与助手编排；frontend/ 负责当前界面
    ui/       旧 Streamlit 兼容回归，不是生产入口
"""

__version__ = "0.1.0"
