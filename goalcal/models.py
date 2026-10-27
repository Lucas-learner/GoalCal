from __future__ import annotations

from datetime import date, datetime

from .timeutil import utcnow_naive

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(80), default="demo")
    tz: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    goals: Mapped[list["Goal"]] = relationship(back_populates="user")


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    draft_id: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(Text, default="")
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|done|paused|archived
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    template_key: Mapped[str] = mapped_column(String(40), default="generic")
    outline: Mapped[str] = mapped_column(Text, default="")  # JSON 数组：阶段大纲（滚动拆解的总骨架）

    user: Mapped[User] = relationship(back_populates="goals")
    steps: Mapped[list["Step"]] = relationship(
        back_populates="goal", cascade="all, delete-orphan", order_by="Step.seq"
    )


class Step(Base):
    """目标下的最小执行单元（叶子任务）。

    energy: 0=低精力可做, 1=中等, 2=需要整块专注
    kind:   deep | admin | physical | social | research
    """

    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    milestone: Mapped[str] = mapped_column(String(120), default="")
    title: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(Text, default="")
    est_min: Mapped[int] = mapped_column(Integer, default=45)
    energy: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(20), default="deep")
    status: Mapped[str] = mapped_column(String(20), default="todo")  # todo|done|dropped
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    minutes_spent: Mapped[int] = mapped_column(Integer, default=0)

    goal: Mapped[Goal] = relationship(back_populates="steps")


class Event(Base):
    """kind: external(用户原有日程) | block(目标执行块) | buffer(缓冲)"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    start_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_utc: Mapped[datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(20), default="external")
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned|done|partial|missed|skipped|moved
    source: Mapped[str] = mapped_column(String(20), default="chat")  # chat|seed|import|proposal
    uid: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)  # ICS 导入去重键
    step_id: Mapped[int | None] = mapped_column(ForeignKey("steps.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class Checkin(Base):
    __tablename__ = "checkins"

    id: Mapped[int] = mapped_column(primary_key=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id"), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str] = mapped_column(String(20), default="done")  # done|partial|not_done
    reason: Mapped[str | None] = mapped_column(String(20), nullable=True)  # tired|no_time|stuck|unwilling
    minutes: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")


class StateLog(Base):
    __tablename__ = "state_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    energy: Mapped[str] = mapped_column(String(20), default="mid")  # low|mid|high
    label: Mapped[str] = mapped_column(String(20), default="")  # tired|busy|uninspired|fresh
    note: Mapped[str] = mapped_column(Text, default="")


class RescheduleLog(Base):
    __tablename__ = "reschedule_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    new_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    reason: Mapped[str] = mapped_column(String(40), default="unknown")
    moved_to_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto: Mapped[bool] = mapped_column(Boolean, default=True)


class UserPreference(Base):
    """长期记忆的用户偏好。value 为 JSON 文本；(user_id, key, scope) 唯一。

    scope:    global | scheduler | ...
    source:   explicit | derived | system（兼容早期 user/agent）
    """

    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "key", "scope", name="uq_user_preferences_user_key_scope"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text, default="null")  # JSON
    scope: Mapped[str] = mapped_column(String(40), default="global")
    source: Mapped[str] = mapped_column(String(20), default="explicit")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    evidence: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "client_id", name="uq_messages_conversation_client"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    client_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="text")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class Interaction(Base):
    """埋点：ts_sim 用注入的模拟时间，ts_wall 用真实墙钟。"""

    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts_sim: Mapped[datetime] = mapped_column(DateTime)
    ts_wall: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    kind: Mapped[str] = mapped_column(String(40))
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"), nullable=True)
    step_id: Mapped[int | None] = mapped_column(ForeignKey("steps.id"), nullable=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
