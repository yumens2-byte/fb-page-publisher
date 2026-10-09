"""
fbpub/notify.py
===============
결과 알림 (선택). 전용 Telegram 대화방만 사용한다 — 다른 채널 ID 재사용 금지.
미설정이면 로그만 남긴다. 알림 실패는 게시 결과에 영향을 주지 않는다.

사용: python -m fbpub.notify --message "내용"
"""
from __future__ import annotations

import argparse
import logging

import requests

from fbpub import settings
from fbpub.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def send(message: str) -> bool:
    text = redact(message)[:1000]
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.info(f"[Notify] 알림 대상 미설정 — 로그만 기록: {text}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"[Notify] 전송 실패 http_{resp.status_code}")
            return False
        return True
    except requests.exceptions.RequestException as e:
        logger.warning(f"[Notify] 전송 실패: {type(e).__name__}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", required=True)
    send(parser.parse_args().message)
