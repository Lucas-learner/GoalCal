#!/usr/bin/env bash
# Writes LLM credentials to Config/env.local (gitignored, chmod 600).
#
# Interactive on purpose: the key never appears in a shell history entry or in
# chat. Everything that needs it sources this file.
#
# NOTE: every variable interpolation below uses ${VAR} braces. A bare $VAR that
# is immediately followed by a full-width CJK punctuation mark breaks: bash folds
# the multi-byte character into the variable name and, under `set -u`, aborts
# with "unbound variable". Keep the braces.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p Config
CONFIG="Config/env.local"

if [ -f "${CONFIG}" ]; then
    echo "注意:${CONFIG} 已存在,继续会覆盖。"
fi

echo "填入你的 OpenAI 兼容服务商信息。例子:"
echo "  DeepSeek   -> https://api.deepseek.com                          / deepseek-flash"
echo "  阿里云百炼 -> https://dashscope.aliyuncs.com/compatible-mode/v1 / qwen-plus"
echo ""

read -r -p "LLM_BASE_URL: " base_url
read -r -p "LLM_MODEL   : " model
read -r -s -p "LLM_API_KEY : " api_key
echo ""

if [ -z "${base_url}" ] || [ -z "${model}" ] || [ -z "${api_key}" ]; then
    echo "三项都不能为空,已中止。" >&2
    exit 1
fi

umask 077
{
    printf 'LLM_BASE_URL=%q\n' "${base_url}"
    printf 'LLM_MODEL=%q\n' "${model}"
    printf 'LLM_API_KEY=%q\n' "${api_key}"
} > "${CONFIG}"
chmod 600 "${CONFIG}"

# Verify the file actually parses before declaring success. Writing it and then
# dying on a message would leave a valid file but a confusing failure.
if ! grep -q '^LLM_BASE_URL=' "${CONFIG}" \
   || ! grep -q '^LLM_MODEL=' "${CONFIG}" \
   || ! grep -q '^LLM_API_KEY=' "${CONFIG}"; then
    echo "写入校验失败:${CONFIG} 内容不完整。" >&2
    exit 1
fi

echo ""
echo "已写入:${CONFIG}"
echo "权限  :$(stat -f '%Sp' "${CONFIG}" 2>/dev/null || echo 600)"
echo "已在 .gitignore 中,不会提交。"
echo ""
echo "下一步:./scripts/demo.sh --llm"
