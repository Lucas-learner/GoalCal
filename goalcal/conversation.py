from __future__ import annotations

import json
import uuid
from dataclasses import fields, is_dataclass
from datetime import date, datetime

from sqlalchemy import select

from .domain.planner import GoalBrief
from .domain.scheduler import Candidate
from .llm.action import Action
from .models import Conversation, Message
from .timeutil import utcnow_naive

STATE_VERSION = 1


def _encode(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Action):
        return {"__type__": "Action", "value": value.model_dump(mode="json")}
    if is_dataclass(value):
        return {
            "__type__": type(value).__name__,
            "value": {field.name: _encode(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    decoded = {key: _decode(item) for key, item in value.items()}
    kind = decoded.get("__type__")
    data = decoded.get("value")
    if kind == "Action":
        return Action(**data)
    if kind == "PendingAction":
        from .agent import PendingAction

        return PendingAction(**data)
    if kind == "GoalBrief":
        if data.get("deadline"):
            data["deadline"] = date.fromisoformat(data["deadline"])
        return GoalBrief(**data)
    if kind == "GoalDraft":
        from .agent import GoalDraft

        data["deadline"] = date.fromisoformat(data["deadline"])
        return GoalDraft(**data)
    if kind == "Candidate":
        if isinstance(data["start"], str):
            data["start"] = datetime.fromisoformat(data["start"])
        if isinstance(data["end"], str):
            data["end"] = datetime.fromisoformat(data["end"])
        return Candidate(**data)
    if kind == "CheckinContext":
        from .agent import CheckinContext

        return CheckinContext(**data)
    if kind is None:
        for key in ("start", "end"):
            if isinstance(decoded.get(key), str):
                try:
                    decoded[key] = datetime.fromisoformat(decoded[key])
                except ValueError:
                    pass
    return decoded


def _conversation(session, user_id: int) -> Conversation | None:
    return session.scalar(
        select(Conversation).where(Conversation.user_id == user_id).order_by(Conversation.id.desc())
    )


def load_state(session, user_id: int) -> tuple[list[dict], dict]:
    conversation = _conversation(session, user_id)
    if conversation is None:
        return [], {}
    messages = [
        {"role": message.role, "content": message.content, "client_id": message.client_id}
        for message in session.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
        )
    ]
    try:
        state = _decode(json.loads(conversation.state_json or "{}"))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        state = {}
    return messages, state


def save_state(session, user_id: int, messages: list[dict], state: dict) -> Conversation:
    conversation = _conversation(session, user_id)
    if conversation is None:
        conversation = Conversation(user_id=user_id)
        session.add(conversation)
        session.flush()
    stored = list(
        session.execute(
            select(Message.id, Message.client_id).where(Message.conversation_id == conversation.id)
        )
    )
    existing_ids = {row.client_id for row in stored if row.client_id is not None}
    for index, item in enumerate(messages):
        if item.get("client_id") in existing_ids:
            continue
        if index < len(stored) and not item.get("client_id"):
            continue
        item["client_id"] = item.get("client_id") or uuid.uuid4().hex
        session.add(
            Message(
                conversation_id=conversation.id,
                client_id=item["client_id"],
                role=item["role"],
                content=item["content"],
            )
        )
    conversation.state_json = json.dumps(_encode(state), ensure_ascii=False)
    conversation.updated_at = utcnow_naive()
    session.flush()
    return conversation
