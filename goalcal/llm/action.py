from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal[
    "add_event",
    "move_event",
    "delete_event",
    "query_events",
    "create_goal",
    "log_progress",
    "set_state",
    "chat",
]


class Action(BaseModel):
    """LLM/规则层唯一允许输出的结构。执行层只认这个结构。"""

    intent: Intent = "chat"
    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    duration_min: int | None = None
    target: str | None = Field(default=None, description="move/delete 时要找的原事件标题")
    deadline: date | None = None
    state: Literal["tired", "busy", "uninspired", "fresh"] | None = None
    progress_target: str | None = None
    confidence: float = 0.0
    raw: str = ""

    def normalized(self, now: datetime) -> "Action":
        """补齐 end / 时区，保证执行层拿到的永远是完整时间。"""
        if self.start is not None:
            if self.start.tzinfo is None:
                self.start = self.start.replace(tzinfo=now.tzinfo)
            if self.end is None:
                self.end = self.start + timedelta(minutes=self.duration_min or 60)
            if self.duration_min is None:
                self.duration_min = int((self.end - self.start).total_seconds() // 60)
        return self
