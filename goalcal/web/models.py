"""Additive persistence for web workflows; all datetimes are UTC naive."""
from __future__ import annotations

from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ..models import Base


class WebProposal(Base):
    __tablename__ = "web_proposals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id", ondelete="CASCADE"))
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="open")
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class WebOperation(Base):
    __tablename__ = "web_operations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)


class WebExecution(Base):
    __tablename__ = "web_executions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[int | None] = mapped_column(ForeignKey("steps.id", ondelete="CASCADE"), index=True, nullable=True)
    habit_occurrence_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("habit_occurrences.id", ondelete="CASCADE"), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime)
    expected_end_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class WebDraft(Base):
    __tablename__ = "web_drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id", ondelete="SET NULL"), nullable=True)


class Habit(Base):
    __tablename__ = "habits"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id", ondelete="SET NULL"))
    timezone: Mapped[str] = mapped_column(String(64))
    start_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="active")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    inactive_periods: Mapped[str] = mapped_column(Text, default="[]")
    confirmed_through: Mapped[date | None] = mapped_column(Date)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class HabitRule(Base):
    __tablename__ = "habit_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    habit_id: Mapped[int] = mapped_column(ForeignKey("habits.id", ondelete="CASCADE"), index=True)
    effective_date: Mapped[date] = mapped_column(Date)
    definition: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("habit_id", "effective_date"),)


class HabitOccurrence(Base):
    __tablename__ = "habit_occurrences"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    habit_id: Mapped[int] = mapped_column(ForeignKey("habits.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    prior_status: Mapped[str | None] = mapped_column(String(12))
    reason: Mapped[str | None] = mapped_column(String(32))
    custom: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("habit_id", "day"),)
