#!/usr/bin/env bash
# Replaces ONLY the API key in Config/env.local, then verifies it immediately.
#
# Use this instead of setup-llm.sh when base_url and model are already correct
# but the key is wrong: one prompt, one field, no chance of misalignment.
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="Config/env.local"

if [ ! -f "${CONFIG}" ]; then
    echo "${CONFIG} 不存在，请先运行 ./scripts/setup-llm.sh" >&2
    exit 1
fi

# Pull the existing base_url / model so we can show and reuse them.
LLM_BASE_URL=""
LLM_MODEL=""
while IFS='=' read -r key value; do
    case "${key}" in
        LLM_BASE_URL) LLM_BASE_URL="$(printf '%s' "${value}" | sed -e 's/^"//' -e 's/"$//')" ;;
        LLM_MODEL)    LLM_MODEL="$(printf '%s' "${value}" | sed -e 's/^"//' -e 's/"$//')" ;;
    esac
done < "${CONFIG}"

if [ -z "${LLM_BASE_URL}" ] || [ -z "${LLM_MODEL}" ]; then
    echo "配置里缺少 LLM_BASE_URL 或 LLM_MODEL，请先运行 ./scripts/setup-llm.sh" >&2
    exit 1
fi

echo "地址:${LLM_BASE_URL}"
echo "模型:${LLM_MODEL}"
echo ""
read -r -s -p "粘贴新的 API key（输入不显示）: " api_key
echo ""

if [ -z "${api_key}" ]; then
    echo "key 为空，已中止。" >&2
    exit 1
fi

# Rewrite only the key line so base_url/model cannot be clobbered.
TMP="${CONFIG}.tmp"
{
    grep -v '^LLM_API_KEY=' "${CONFIG}" || true
    printf 'LLM_API_KEY=%q\n' "${api_key}"
} > "${TMP}"
mv "${TMP}" "${CONFIG}"
chmod 600 "${CONFIG}"
echo "已写入 ${CONFIG}"
echo ""
echo "正在验证 ..."

probe_body="$(printf '{"model":"%s","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' "${LLM_MODEL}")"
probe_out="Config/.probe.out"
http_status="$(curl -sS --max-time 30 -o "${probe_out}" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer ${api_key}" \
    -d "${probe_body}" \
    "${LLM_BASE_URL%/}/chat/completions" || echo "000")"

if [ "${http_status}" = "200" ]; then
    echo "✅ key 有效，模型已连通。"
    rm -f "${probe_out}"
    echo ""
    echo "下一步:./scripts/demo.sh --llm"
else
    echo "❌ HTTP ${http_status}"
    sed -e 's/[A-Za-z0-9_-]\{20,\}/<已脱敏>/g' "${probe_out}" 2>/dev/null | head -c 400
    echo ""
    rm -f "${probe_out}"
    exit 1
fi
