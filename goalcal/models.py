from __future__ import annotations

from datetime import date, datetime

from .timeutil import utcnow_naive

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), default="demo")
    tz: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    goals: Mapped[list["Goal"]] = relationship(back_populates="user")


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(Text, default="")
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|done|archived
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    template_key: Mapped[str] = mapped_column(String(40), default="generic")

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
    status: Mapped[str] = mapped_column(String(20), default="todo")  # todo|scheduled|done|skipped
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
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned|done|missed|moved
    source: Mapped[str] = mapped_column(String(20), default="chat")  # chat|seed|import|proposal
    step_id: Mapped[int | None] = mapped_column(ForeignKey("steps.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class Checkin(Base):
    __tablename__ = "checkins"

    id: Mapped[int] = mapped_column(primary_key=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str] = mapped_column(String(20), default="done")  # done|partial|not_done
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
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    reason: Mapped[str] = mapped_column(String(40), default="unknown")
    moved_to_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto: Mapped[bool] = mapped_column(Boolean, default=True)
