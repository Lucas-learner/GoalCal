from __future__ import annotations

import logging

import streamlit as st

from goalcal.db import init_db
from goalcal.logging_setup import configure_logging
from goalcal.ui import calendar_page, chat_page, controller, theme
from goalcal.ui.auth import require_login

st.set_page_config(page_title="GoalCal", page_icon="📆", layout="wide")
configure_logging()
try:
    init_db()
except Exception as exc:
    logging.getLogger("goalcal").error("startup_failed error_type=%s", type(exc).__name__)
    st.error("数据库初始化失败，服务尚未启动。请查看本机管理日志并检查迁移备份。")
    st.stop()

chat = st.Page(chat_page.render, title="Chat", default=True)
goals = st.Page("routes/goals.py", title="Goals")
calendar = st.Page("routes/calendar.py", title="Calendar")
settings = st.Page("routes/settings.py", title="Settings")
st.session_state["chat_page"] = chat

page = st.navigation([chat, goals, calendar, settings], position="hidden")

principal = require_login()
st.session_state["tz"] = principal.tz
st.session_state["username"] = principal.username
controller.ensure_state()
controller.start_checkin_if_needed()


with st.sidebar:
    st.markdown("**GoalCal**")
    if st.button("New conversation", key="sidebar_new_conversation_action", use_container_width=True):
        controller.new_conversation(open_chat=True)
    st.page_link(chat, label="Chat", use_container_width=True)
    st.page_link(goals, label="Goals", use_container_width=True)
    st.page_link(calendar, label="Calendar", use_container_width=True)
    st.page_link(settings, label="Settings", use_container_width=True)

calendar_active = page.title == "Calendar"
calendar_shell_css = "" if calendar_active else """
.st-key-calendar_shell {
    position: absolute !important;
    left: -10000px;
    width: 100%;
    visibility: hidden;
    pointer-events: none;
}
"""
# 全局样式与日历壳样式合成同一个 st.html 节点，保证 Calendar iframe
# 前的 DOM 结构在所有页面完全一致。
theme.inject(calendar_shell_css)
with st.container(key="calendar_shell"):
    calendar_page.render(active=calendar_active)

page.run()
