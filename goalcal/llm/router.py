"""按层（turn/plan/memory）分发 LLM 客户端。

每层可用 LLM_MODEL_TURN / LLM_BASE_URL_TURN / LLM_API_KEY_TURN（同理 _PLAN、
_MEMORY）单独配置；未配置的字段逐项回落到现有 LLM_MODEL / LLM_BASE_URL /
LLM_API_KEY，换供应商只改环境变量。配额、认证与写事务保护都在 LLMClient
内部，按层共享同一套语义，遥测按 layer 归因。

L1 轮引擎（turn）是每轮同步调用：超时 8s、不重试、max_tokens 1000；
其它层保持原有默认（30s），避免拖慢全局。
"""

from __future__ import annotations

import os

from ..config import settings
from .client import LLMClient

LAYERS = ("turn", "plan", "memory")

# 只收紧 L1；plan/memory 走 LLMClient 默认值
_LAYER_OPTIONS = {
    # Some providers count reasoning tokens against max_tokens. 300 can be spent
    # entirely on reasoning, leaving no JSON decision for the turn engine.
    "turn": {"timeout": 8, "max_tokens": 1000},
}


def _layer_value(layer: str, field: str) -> str:
    return os.getenv(f"LLM_{field.upper()}_{layer.upper()}") or getattr(settings, f"llm_{field}")


class ModelRouter:
    def __init__(self):
        self._clients: dict[str, LLMClient] = {}

    def client(self, layer: str) -> LLMClient:
        if layer not in LAYERS:
            raise ValueError(f"unknown LLM layer: {layer}")
        if layer not in self._clients:
            self._clients[layer] = LLMClient(
                api_key=_layer_value(layer, "api_key") or None,
                base_url=_layer_value(layer, "base_url") or None,
                model=_layer_value(layer, "model") or None,
                layer=layer,
                **_LAYER_OPTIONS.get(layer, {}),
            )
        return self._clients[layer]

    def ready(self, layer: str) -> bool:
        return settings.llm_enabled and self.client(layer).ready


router = ModelRouter()


def plan_client() -> LLMClient:
    """L2 规划调用（拆目标/改计划/提取日历块）的统一入口。"""
    return router.client("plan")


def turn_client() -> LLMClient | None:
    """L1 轮引擎客户端；未启用或未配置 key 时返回 None，由调用方走降级路径。"""
    return router.client("turn") if router.ready("turn") else None
