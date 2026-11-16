#!/bin/zsh
set -e
cd "$(dirname "$0")/.."
export GOALCAL_ENV_FILE="$PWD/.env.production"
printf '\nGoalCal：创建你的第一个账号\n用户名为 owner，密码至少 12 个字符。输入时不会显示字符。\n密码只在这台 Mac 中处理，不要把密码发送到聊天。\n\n'
.venv/bin/python -m goalcal.admin create-user owner
printf '\n账号已创建。请在浏览器中使用 owner 和刚才的密码登录。\n'
open http://127.0.0.1:8501
printf '\n按回车关闭此窗口。'
read -r
