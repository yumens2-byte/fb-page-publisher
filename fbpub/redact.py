"""
fbpub/redact.py
===============
로그·결과 문자열에서 토큰·식별자를 가린다. (access_token·EAA 토큰·노션 토큰 패턴 + 페이지/노션 ID)
"""
from __future__ import annotations

import re
from typing import Any

from fbpub import settings

VERSION = "1.1.0"

MASK = "***"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(access_token|input_token)=[^&\s'\"<>]+"), rf"\1={MASK}"),
    (re.compile(r"(?i)(\"?access_token\"?\s*:\s*\")[^\"]+"), rf"\1{MASK}"),
    (re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?(?:oauth|bearer)\s+)[^\s\"']+"), rf"\1{MASK}"),
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), f"EAA{MASK}"),
    (re.compile(r"\b(secret|ntn)_[A-Za-z0-9]{20,}"), rf"\1_{MASK}"),
)


def _secrets(include_ids: bool) -> list[str]:
    values = [
        settings.PAGE_TOKEN,
        settings.APP_SECRET,
        settings.NOTION_TOKEN,
        settings.TELEGRAM_BOT_TOKEN,
    ]
    if include_ids:
        values += [settings.PAGE_ID, settings.NOTION_DB_ID, settings.TELEGRAM_CHAT_ID]
    return [v for v in values if v and len(v) >= 6]


def redact(text: Any, include_ids: bool = True) -> str:
    """include_ids=False: 자격증명만 가린다 (비공개 노션 원장 기록용 — 게시물 ID 를 온전히 남기기 위해)."""
    s = str(text)
    for value in _secrets(include_ids):
        s = s.replace(value, MASK)
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s
