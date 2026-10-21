from __future__ import annotations

import json
from typing import Any

from ..config import settings


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    """薄封装：以后换供应商只需要改 base_url/model。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key or settings.llm_api_key
        self.base_url = base_url or settings.llm_base_url
        self.model = model or settings.llm_model
        self._client = None

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _get(self):
        if self._client is None:
            if not self.ready:
                raise LLMUnavailable("LLM_API_KEY 未配置")
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=30)
        return self._client

    def complete_json(self, system: str, user: str, temperature: float = 0.0) -> dict[str, Any]:
        client = self._get()
        resp = client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)
