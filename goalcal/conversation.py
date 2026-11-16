from __future__ import annotations

import json
import uuid
from dataclasses import fields, is_dataclass
from datetime import date, datetime

from sqlalchemy import delete, select

from .domain.planner import GoalBrief
from .domain.scheduler import Candidate
from .llm.action import Action
from .models import Conversation, Message
from .timeutil import utcnow_naive

STATE_VERSION = 1
PAGE_SIZE = 30


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
    if kind == "EventDraft":
        from .agent import EventDraft

        return EventDraft(**data)
    if kind == "Candidate":
        if isinstance(data["start"], str):
            data["start"] = datetime.fromisoformat(data["start"])
        if isinstance(data["end"], str):
            data["end"] = datetime.fromisoformat(data["end"])
        return Candidate(**data)
    if kind == "CheckinContext":
        from .agent import CheckinContext

        return CheckinContext(**data)
    if kind == "DialogContext":
        from .dialog import DialogContext

        return DialogContext(**data)
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


def load_task_state(session, user_id: int) -> dict:
    """只恢复当前任务状态（pending/goal_draft 等），与消息分页解耦。"""
    conversation = _conversation(session, user_id)
    if conversation is None:
        return {}
    try:
        return _decode(json.loads(conversation.state_json or "{}"))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return {}


def load_page(
    session, user_id: int, before_id: int | None = None, limit: int = PAGE_SIZE
) -> dict:
    """按消息 ID 游标分页：默认最新一页，before_id 取更早的一页。

    返回 {"messages", "has_more", "has_user_history"}；messages 为正序，
    每条带数据库 id（供下一次 before_id 游标使用）。has_user_history 表示
    整个对话里是否存在真实用户消息，与当前页无关。
    """
    conversation = _conversation(session, user_id)
    if conversation is None:
        return {"messages": [], "has_more": False, "has_user_history": False}
    query = select(Message).where(Message.conversation_id == conversation.id)
    if before_id is not None:
        query = query.where(Message.id < before_id)
    rows = list(session.scalars(query.order_by(Message.id.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    messages = [
        {"id": message.id, "role": message.role, "content": message.content, "client_id": message.client_id}
        for message in reversed(rows[:limit])
    ]
    has_user_history = session.scalar(
        select(Message.id).where(
            Message.conversation_id == conversation.id, Message.role == "user"
        ).limit(1)
    ) is not None
    return {"messages": messages, "has_more": has_more, "has_user_history": has_user_history}


def reset_state(session, user_id: int) -> None:
    """整页重进或手动新对话：删除当前 Conversation 的消息并清空任务状态。

    Conversation 行本身保留（外键与统计口径不变）；旧消息真实删除。
    """
    conversation = _conversation(session, user_id)
    if conversation is None:
        return
    session.execute(delete(Message).where(Message.conversation_id == conversation.id))
    conversation.state_json = "{}"
    conversation.updated_at = utcnow_naive()
    session.flush()


def load_recent(session, user_id: int, limit: int = 6) -> list[dict]:
    """普通 chat 的少量上下文：只读最近 limit 条，不加载完整历史。"""
    return load_page(session, user_id, limit=limit)["messages"]


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
    return messages, load_task_state(session, user_id)


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
    # 内存里可能是分页后的最后一页而非完整历史：无 client_id 的消息只有在
    # 位于任何已知 client_id 消息之前、且落在已存储范围内时，才视为旧消息跳过；
    # 跟在已知消息之后的无 client_id 消息是本轮新追加的，必须写入。
    first_known = next(
        (index for index, item in enumerate(messages) if item.get("client_id")),
        len(messages),
    )
    for index, item in enumerate(messages):
        if item.get("client_id") in existing_ids:
            continue
        if not item.get("client_id") and index < first_known and index < len(stored):
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
