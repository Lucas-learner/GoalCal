"""Durable HTTP request and assistant state, separate from the legacy UI state."""
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ..models import Base


class WebRequest(Base):
    __tablename__ = 'web_requests'
    __table_args__ = (UniqueConstraint('user_id', 'key', name='uq_web_request_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[str] = mapped_column(Text)


class WebThread(Base):
    __tablename__ = 'web_threads'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    messages_json: Mapped[str] = mapped_column(Text, default='[]')
    focus_goal_id: Mapped[int | None] = mapped_column(ForeignKey('goals.id', ondelete='SET NULL'), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    choices_json: Mapped[str] = mapped_column(Text, default='[]')
