"""Lightweight bilingual helpers (Simplified Chinese / English) for web & planning."""

from __future__ import annotations

from datetime import datetime

SUPPORTED_LANGS = ("zh", "en")


def normalize_lang(value: str | None) -> str | None:
    """Return 'zh' or 'en' when explicitly supported; otherwise None."""
    if not value:
        return None
    cleaned = value.strip().lower().replace("_", "-")
    if cleaned.startswith("zh"):
        return "zh"
    if cleaned.startswith("en"):
        return "en"
    return None


def parse_accept_language(header: str | None, *, default: str = "en") -> str:
    """Pick 'zh' or 'en' from an HTTP Accept-Language header, respecting q weights."""
    if not header:
        return default
    ranked: list[tuple[float, int, str]] = []
    for idx, raw_part in enumerate(header.split(",")):
        part = raw_part.strip()
        if not part:
            continue
        tag, *params = [seg.strip() for seg in part.split(";")]
        lang = normalize_lang(tag)
        if not lang:
            continue
        q = 1.0
        for param in params:
            if param.lower().startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
        if q > 0:
            ranked.append((q, -idx, lang))
    if not ranked:
        return default
    ranked.sort(reverse=True)
    return ranked[0][2]


def pick(lang: str | None, zh: str, en: str) -> str:
    """Select Chinese or English text based on resolved language."""
    return en if normalize_lang(lang) == "en" else zh


def format_when(start_local: datetime, end_local: datetime, lang: str | None = "zh") -> str:
    """Format a localized local time interval string."""
    if normalize_lang(lang) == "en":
        if start_local.date() == end_local.date():
            return f"{start_local:%b} {start_local.day}, {start_local:%H:%M}–{end_local:%H:%M}"
        return (
            f"{start_local:%b} {start_local.day} {start_local:%H:%M}"
            f"–{end_local:%b} {end_local.day} {end_local:%H:%M}"
        )
    return f"{start_local:%m月%d日 %H:%M}–{end_local:%H:%M}"
