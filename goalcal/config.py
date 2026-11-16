from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(os.getenv("GOALCAL_ENV_FILE", ROOT / ".env"))


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    production: bool = True
    db_url: str = f"sqlite:///{ROOT / 'var' / 'goalcal-production.db'}"
    tz: str = "Asia/Shanghai"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V3"
    llm_enabled: bool = False

    @property
    def llm_ready(self) -> bool:
        return self.llm_enabled and bool(self.llm_api_key)


def get_settings() -> Settings:
    configured_url = os.getenv("GOALCAL_DB_URL", f"sqlite:///{ROOT / 'var' / 'goalcal-production.db'}")
    if _bool(os.getenv("GOALCAL_PRODUCTION"), True):
        if not configured_url.startswith("sqlite:////"):
            raise ValueError("Production requires an absolute SQLite database URL")
    return Settings(
        production=_bool(os.getenv("GOALCAL_PRODUCTION"), True),
        db_url=configured_url,
        tz=os.getenv("GOALCAL_TZ", "Asia/Shanghai"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1"),
        llm_model=os.getenv("LLM_MODEL", "deepseek-ai/DeepSeek-V3"),
        llm_enabled=_bool(os.getenv("LLM_ENABLED")),
    )


settings = get_settings()
