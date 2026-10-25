from __future__ import annotations

import json
import time
from .. import telemetry
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

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=30, max_retries=0)
        return self._client

    def complete_json(self, system: str, user: str, temperature: float = 0.0) -> dict[str, Any]:
        client = self._get()
        from ..tenancy import current_session
        from ..quota import consume
        session = current_session.get()
        if settings.production and session is None:
            raise LLMUnavailable("Authenticated user context required")
        if session is not None:
            # Never wait for an external request while holding a SQLite write
            # lock. Follow-up refinement after a write uses the existing template
            # fallback; read-only planning and parsing can use the provider.
            if session.info.get("wrote"):
                telemetry.emit('llm_skipped', user_id=session.info['principal'].user_id,
                               reason='write_transaction')
                raise LLMUnavailable("Template fallback during a write transaction")
            from ..auth import authenticate
            principal = authenticate(session.info['token'])
            try:
                consume(principal.user_id)
            except RuntimeError:
                telemetry.emit('llm_skipped', user_id=principal.user_id, reason='daily_quota')
                raise
        started = time.perf_counter()
        user_id = session.info['principal'].user_id if session is not None else None
        try:
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
            data = json.loads(content)
        except Exception as exc:
            telemetry.emit('llm_failed', user_id=user_id, model=self.model,
                           duration_ms=round((time.perf_counter()-started)*1000, 1),
                           error_type=type(exc).__name__)
            raise
        telemetry.emit('llm_completed', user_id=user_id, model=self.model,
                       duration_ms=round((time.perf_counter()-started)*1000, 1))
        return data
