"""
fbpub/content_guard.py
======================
게시 직전 텍스트 최종 게이트 (마스터 수동 검수의 보조 — 대체가 아님).

규칙 출처: 수사일지 작성 규칙(예약 작업 STEP 2) / REQ-FBDET-001 §17.4
  - 필수 문구: 픽션 고지, 실명·회사명 안내, 해시태그 3종
  - 3요건 줄: "⚖️ 체크: ①우위 X ②적정범위 X ③고통 X" (X ∈ ✔ ? ✖)
  - 날짜: 가상 상대일 D+n 사용, 실제 날짜 표기 금지
  - 반응 강요 문구 금지
금칙어 사전(DB-06)은 외부 반출 금지 원칙에 따라 여기서 다루지 않는다.
"""
from __future__ import annotations

import re

VERSION = "1.0.0"

FICTION_NOTICE = "※ 여러 사례를 재구성한 픽션입니다."
LEGAL_NOTICE = "법률 자문이 아닙니다."
COMMENT_NOTICE = "(실명·회사명은 적지 말아 주세요. 확인 시 숨김 처리됩니다.)"
HASHTAGS = ("#사건파일76", "#수사일지", "#직장내괴롭힘")

CHECK_LINE = re.compile(r"⚖️?\s*체크:\s*①우위\s*[✔?✖]\s*②적정범위\s*[✔?✖]\s*③고통\s*[✔?✖]")
RELATIVE_DAY = re.compile(r"D\+\d+")
REAL_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(19|20)\d{2}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}"),
    re.compile(r"(?<!\d)\d{1,2}\s*월\s*\d{1,2}\s*일"),
)
PUSHY_PHRASES = ("댓글 달아", "댓글로 남겨", "좋아요 눌러", "공유해 주세요", "팔로우 해 주세요")


def check_feed_text(text: str) -> list[str]:
    """위반 코드 목록 (빈 리스트 = 통과)."""
    errors: list[str] = []
    body = text or ""
    if not body.strip():
        return ["feed:empty"]
    if FICTION_NOTICE not in body or LEGAL_NOTICE not in body:
        errors.append("feed:fiction_notice_missing")
    if COMMENT_NOTICE not in body:
        errors.append("feed:comment_notice_missing")
    for tag in HASHTAGS:
        if tag not in body:
            errors.append(f"feed:hashtag_missing:{tag}")
    if not CHECK_LINE.search(body):
        errors.append("feed:check_line_invalid")
    if not RELATIVE_DAY.search(body):
        errors.append("feed:relative_day_missing")
    errors.extend(_common(body, "feed"))
    return errors


def check_reel_text(text: str) -> list[str]:
    errors: list[str] = []
    body = text or ""
    if not body.strip():
        return ["reel:empty"]
    if FICTION_NOTICE not in body:
        errors.append("reel:fiction_notice_missing")
    errors.extend(_common(body, "reel"))
    return errors


def _common(body: str, scope: str) -> list[str]:
    errors: list[str] = []
    for pattern in REAL_DATE_PATTERNS:
        if pattern.search(body):
            errors.append(f"{scope}:real_date_found")
            break
    for phrase in PUSHY_PHRASES:
        if phrase in body:
            errors.append(f"{scope}:pushy_phrase")
            break
    return errors
