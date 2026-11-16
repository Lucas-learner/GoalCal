from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from .. import auth, telemetry
from ..tenancy import session_scope as tenant_session_scope
from ..timeutil import local_now


def user_id() -> int:
    return st.session_state["user_id"]


def timezone() -> str:
    return st.session_state["tz"]


def zone() -> ZoneInfo:
    return ZoneInfo(timezone())


def now() -> datetime:
    return local_now(timezone())


def flash(message: str) -> None:
    st.session_state["flash"] = message


@contextmanager
def session_scope(operation="dashboard.load"):
    operation_id = None
    try:
        with telemetry.operation(operation, user_id()) as operation_id:
            with tenant_session_scope(st.session_state.get("auth_token")) as session:
                yield session
    except auth.AuthError:
        from .auth import clear_state

        clear_state()
        st.rerun(scope="app")
    except Exception:
        st.session_state["last_error_id"] = operation_id
        messages = st.session_state.get("messages") or []
        if messages and messages[-1].get("role") == "user":
            messages.append({
                "role": "assistant",
                "content": "I couldn't complete that turn, so no changes were saved. Please try again.",
            })
        st.error(f"The action failed and its changes were rolled back. Reference: {operation_id}. You can send feedback in Settings.")
        st.stop()
