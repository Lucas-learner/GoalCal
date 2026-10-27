from __future__ import annotations

import logging
import html

import streamlit as st

from goalcal import auth
from goalcal.db import init_db
from goalcal.logging_setup import configure_logging
from goalcal.ui import calendar_page, chat_page, controller, theme
from goalcal.ui.auth import clear_state, current_token, require_login
from goalcal.auth_proxy import COOKIE_NAME, FORM_COOKIE_NAME

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

try:
    auth.authenticate(current_token())
    navigation_position = "sidebar"
except auth.AuthError:
    navigation_position = "hidden"

page = st.navigation([chat, goals, calendar, settings], position=navigation_position)

principal = require_login()
st.session_state["tz"] = principal.tz
controller.ensure_state()
controller.start_checkin_if_needed()


with st.sidebar:
    st.caption(f"{principal.username} · {principal.tz}")
    if isinstance(st.context.cookies.get(COOKIE_NAME), str) and st.context.cookies.get(COOKIE_NAME):
        form_token = html.escape(st.context.cookies.get(FORM_COOKIE_NAME, ""), quote=True)
        st.html(f'<form action="/_goalcal/logout" method="post"><input type="hidden" name="csrf" value="{form_token}"><button type="submit">Log out</button></form>')
    elif st.button("Log out"):
        auth.logout(st.session_state.get("auth_token"))
        clear_state()
        st.rerun(scope="app")

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
