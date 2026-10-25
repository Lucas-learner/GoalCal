import streamlit as st
from .. import auth


def clear_state():
    st.session_state.clear()


def require_login():
    try:
        principal = auth.authenticate(st.session_state.get('auth_token'))
    except auth.AuthError:
        if st.session_state.get('auth_token') or st.session_state.get('user_id'):
            clear_state()
        st.title('📆 GoalCal')
        login_tab, register_tab = st.tabs(['登录', '邀请码注册'])
        with login_tab, st.form('login'):
            username = st.text_input('用户名', key='login_name')
            password = st.text_input('密码', type='password', key='login_password')
            submitted = st.form_submit_button('登录')
        if submitted:
            try:
                token = auth.login(username, password)
                clear_state()
                st.session_state['auth_token'] = token
                st.rerun()
            except auth.AuthError as exc:
                st.error(str(exc))
        with register_tab, st.form('register'):
            username = st.text_input('用户名', key='register_name')
            password = st.text_input('密码（至少 12 个字符）', type='password')
            invitation = st.text_input('邀请码', type='password')
            tz = st.text_input('时区', value='Asia/Shanghai')
            registered = st.form_submit_button('注册')
        if registered:
            try:
                auth.create_user(username, password, tz, invitation)
                st.success('注册成功，请登录。')
            except auth.AuthError as exc:
                st.error(str(exc))
        st.caption("试用期间会记录操作类型、耗时和错误以改进产品；密码、聊天和日历正文不会自动写入运行日志。")
        st.stop()
    st.session_state['user_id'] = principal.user_id
    return principal
