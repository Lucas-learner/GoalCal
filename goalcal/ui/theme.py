"""全局视觉：暖白底、深墨字、深青主色、系统字体、细分隔线、8px 圆角。

app.py 在登录后的共享 shell 中以固定节点顺序调用 inject()，
跨页 DOM 结构（尤其是 Calendar iframe 之前的节点）必须保持稳定。
"""

from __future__ import annotations

import streamlit as st

CSS = """
:root {
    --goalcal-ink: #292524;
    --goalcal-primary: #0f766e;
    --goalcal-line: #e7e2da;
    --goalcal-radius: 8px;
}

.stApp,
.stApp header[data-testid="stHeader"] {
    background: #faf9f6;
}

section[data-testid="stSidebar"] {
    background: #f1eee9;
    border-right: 1px solid var(--goalcal-line);
}

.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea,
.stApp button, .stApp [data-testid="stMarkdownContainer"] {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", "PingFang SC", "Hiragino Sans GB",
        "Microsoft YaHei", sans-serif;
    color: var(--goalcal-ink);
}

.stApp h1, .stApp h2, .stApp h3 {
    color: var(--goalcal-ink);
    letter-spacing: -0.01em;
}

div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-calendar_shell) {
    border: none;
}

div[data-testid="stVerticalBlockBorderWrapper"] {
    border: 1px solid var(--goalcal-line);
    border-radius: var(--goalcal-radius);
    box-shadow: none;
}

.stButton > button, .stPopover > button, a[data-testid="stLinkButton"] {
    border-radius: var(--goalcal-radius);
    border-color: var(--goalcal-line);
}
form[action="/_goalcal/logout"] {
    margin: 0;
}
form[action="/_goalcal/logout"] button {
    width: 100%;
    min-height: 2.5rem;
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--goalcal-line);
    border-radius: var(--goalcal-radius);
    background: transparent;
    cursor: pointer;
}
.stButton > button[kind="primary"] {
    background: var(--goalcal-primary);
    border-color: var(--goalcal-primary);
}
.stApp .stButton > button[kind="primary"],
.stApp .stButton > button[kind="primary"] * {
    color: #ffffff;
}

div[data-testid="stTextInput"] input,
div[data-testid="stTextArea"] textarea,
div[data-testid="stNumberInput"] input,
div[data-baseweb="select"] > div,
div[data-testid="stChatInput"] textarea {
    border-radius: var(--goalcal-radius) !important;
    border-color: var(--goalcal-line);
}

div[data-testid="stChatMessage"] {
    background: transparent;
    border: none;
    padding-top: 0.25rem;
    padding-bottom: 0.25rem;
}

hr, div[data-testid="stDivider"] {
    border-color: var(--goalcal-line);
}

div[data-testid="stProgressBarTrack"] > div {
    background-color: var(--goalcal-primary);
}

/* Chat 空首页（.st-key-home_* 元素仅首页存在，其他页面不匹配） */
.stApp .st-key-home_slogan {
    margin-top: 12vh;
}
.stApp .st-key-home_slogan p {
    font-size: 1.5rem;
    font-weight: 650;
    letter-spacing: -0.01em;
}
.stApp .st-key-home_suggestions {
    margin-top: 1.5rem;
}
.stApp div[class*="st-key-home_suggestion_"] .stButton {
    width: 100%;
}
.stApp div[class*="st-key-home_suggestion_"] .stButton > button {
    display: block;
    width: 100%;
    max-width: 100%;
    text-align: left;
    background: transparent;
    border: none;
    box-shadow: none;
    padding-left: 0.25rem;
    color: var(--goalcal-primary);
    font-size: 0.95rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.stApp div[class*="st-key-home_suggestion_"] .stButton > button p {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.stApp div[class*="st-key-home_suggestion_"] .stButton > button:hover {
    background: #f1eee9;
    color: var(--goalcal-primary);
}
"""


def inject(extra: str = "") -> None:
    """注入全局 CSS；extra 是页面级的附加样式（如跨页隐藏日历壳）。

    固定为一次 st.html 调用：Calendar iframe 前的节点数在所有页面保持一致。
    """
    st.html(f"<style>{CSS}{extra}</style>")
