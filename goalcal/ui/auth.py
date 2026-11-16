import zoneinfo

import streamlit as st
from .. import auth
from ..auth_proxy import COOKIE_NAME, english_auth_error

_TIMEZONES = sorted(zoneinfo.available_timezones())
_DEFAULT_TZ_INDEX = _TIMEZONES.index('Asia/Shanghai') if 'Asia/Shanghai' in _TIMEZONES else 0


def clear_state():
    st.session_state.clear()


def current_token():
    token = st.session_state.get('auth_token')
    if token:
        return token
    token = st.context.cookies.get(COOKIE_NAME)
    if isinstance(token, str) and token:
        st.session_state['auth_token'] = token
        return token
    return None


def behind_proxy():
    """登录网关持有 HttpOnly 会话 cookie；浏览器带着它说明运行在网关之后。"""
    token = st.context.cookies.get(COOKIE_NAME)
    return isinstance(token, str) and bool(token)


def _gateway_expired_page():
    # 网关之后会话失效：应用内表单无法补种 HttpOnly cookie，且 websocket
    # 断开后表单按钮是死的。纯链接整页跳回网关登录页，断连残影里也能点。
    st.title('📅 GoalCal')
    st.markdown(
        'Your session has expired. <a href="/" target="_top">Log in again</a>',
        unsafe_allow_html=True,
    )


def _local_login_page():
    st.title('📅 GoalCal')
    login_tab, register_tab = st.tabs(['Log in', 'Create account'])
    with login_tab, st.form('login'):
        username = st.text_input('Username', key='login_name')
        password = st.text_input('Password', type='password', key='login_password')
        submitted = st.form_submit_button('Log in')
    if submitted:
        try:
            token = auth.login(username, password)
            clear_state()
            st.session_state['auth_token'] = token
            st.rerun()
        except auth.AuthError as exc:
            st.error(english_auth_error(str(exc)))
    with register_tab, st.form('register'):
        username = st.text_input('Username', key='register_name')
        password = st.text_input('Password (at least 12 characters)', type='password')
        invitation = st.text_input('Invitation code', type='password')
        tz = st.selectbox('Time zone', options=_TIMEZONES, index=_DEFAULT_TZ_INDEX)
        registered = st.form_submit_button('Register')
    if registered:
        try:
            auth.create_user(username, password, tz, invitation)
            st.success('Account created. Please log in.')
        except auth.AuthError as exc:
            st.error(english_auth_error(str(exc)))
    st.caption("During the trial, we log action types, timing, and errors to improve the app. Passwords, chat messages, and calendar details are not automatically added to runtime logs.")


def require_login():
    try:
        principal = auth.authenticate(current_token())
    except auth.AuthError:
        if st.session_state.get('auth_token') or st.session_state.get('user_id'):
            clear_state()
        if behind_proxy():
            _gateway_expired_page()
        else:
            _local_login_page()
        st.stop()
    st.session_state['user_id'] = principal.user_id
    return principal
