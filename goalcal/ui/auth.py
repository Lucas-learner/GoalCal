import streamlit as st
from .. import auth
from ..auth_proxy import COOKIE_NAME, english_auth_error


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


def require_login():
    try:
        principal = auth.authenticate(current_token())
    except auth.AuthError:
        if st.session_state.get('auth_token') or st.session_state.get('user_id'):
            clear_state()
        st.title('📆 GoalCal')
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
            tz = st.text_input('Time zone', value='Asia/Shanghai')
            registered = st.form_submit_button('Register')
        if registered:
            try:
                auth.create_user(username, password, tz, invitation)
                st.success('Account created. Please log in.')
            except auth.AuthError as exc:
                st.error(english_auth_error(str(exc)))
        st.caption("During the trial, we log action types, timing, and errors to improve the app. Passwords, chat messages, and calendar details are not automatically added to runtime logs.")
        st.stop()
    st.session_state['user_id'] = principal.user_id
    return principal
