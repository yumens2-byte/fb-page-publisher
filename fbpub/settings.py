"""
fbpub/settings.py
=================
환경변수 → 상수. 모든 설정값은 이 파일에서만 읽는다.

접두어: FBDET_ (REQ-FBDET-001 D-11 예시값). 다른 페이지 키(FACE_ 등)와 공유하지 않는다.
"""
from __future__ import annotations

import os

VERSION = "1.1.0"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: str) -> bool:
    return _env(name, default).lower() == "true"


# ── 운영 모드 ─────────────────────────────────────────────
# "true"/빈 값 → DRY_RUN, "false" → 실게시, 그 외 값(오타·"1" 등) → 설정 오류로 실행 중단 (v1.1.0, 검토 M1)


def parse_dry_run(raw: str) -> bool | None:
    value = (raw or "").strip().lower()
    if value in ("", "true"):
        return True
    if value == "false":
        return False
    return None


_DRY_RUN_PARSED = parse_dry_run(os.getenv("DRY_RUN", ""))
DRY_RUN_INVALID = _DRY_RUN_PARSED is None
DRY_RUN = True if _DRY_RUN_PARSED is None else _DRY_RUN_PARSED
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

# ── Facebook Graph API ───────────────────────────────────
# v25.0: 만료 2028-07-29 (developers.facebook.com/docs/graph-api/changelog/versions)
GRAPH_VERSION = _env("FBDET_GRAPH_VERSION", "v25.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
PAGE_ID = _env("FBDET_PAGE_ID")
PAGE_TOKEN = _env("FBDET_PAGE_TOKEN")
# Preflight debug_token 용 (선택). 앱 액세스 토큰 = "{app_id}|{app_secret}"
APP_ID = _env("FBDET_APP_ID")
APP_SECRET = _env("FBDET_APP_SECRET")

# Page Photos 레퍼런스: "Files can not exceed 10MB"
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_ALLOWED_FORMATS = ("JPEG", "PNG")
PHOTO_ASPECT = (4, 5)            # 운영 규칙: 피드 4:5
PHOTO_ASPECT_TOLERANCE = 0.02

# Reels 게시 가이드: 9x16, 최소 540x960, 24~60fps, 3~90초
REEL_ASPECT = (9, 16)
REEL_ASPECT_TOLERANCE = 0.02
REEL_MIN_WIDTH = 540
REEL_MIN_HEIGHT = 960
REEL_MIN_SEC = 3.0
REEL_MAX_SEC = 90.0
REEL_MIN_FPS = 24.0
REEL_MAX_FPS = 60.0
# 운영 상한 (Meta 한도 "30 API-published posts within a 24-hour moving period" 보다 보수적)
REELS_DAILY_CAP = int(_env("FBDET_REELS_DAILY_CAP", "1"))
# video_reels 의 is_ai_generated (self-claim). 문서에 단계 미기재 → finish 에 포함, 플래그로 끌 수 있음
REEL_AI_FLAG = _env_bool("FBDET_REEL_AI_FLAG", "true")

HTTP_TIMEOUT_SEC = 60
UPLOAD_TIMEOUT_SEC = 300
REEL_POLL_INTERVAL_SEC = int(_env("FBDET_REEL_POLL_INTERVAL_SEC", "30"))
REEL_POLL_MAX_SEC = int(_env("FBDET_REEL_POLL_MAX_SEC", "600"))

# 피드 캡션 AI 표시 문구 (D-P08 미결정 → 기본 미사용, 값이 있으면 캡션 끝에 덧붙임)
PHOTO_AI_NOTICE = _env("FBDET_PHOTO_AI_NOTICE")

# ── Notion ───────────────────────────────────────────────
NOTION_TOKEN = _env("FBDET_NOTION_TOKEN")
NOTION_DB_ID = _env("FBDET_NOTION_DB_ID")
# 2022-06-28: DB 데이터 소스가 1개일 때만 동작 (2025-09-03 업그레이드 가이드). DB-05 에 소스 추가 금지.
NOTION_VERSION = _env("FBDET_NOTION_VERSION", "2022-06-28")
NOTION_BASE = "https://api.notion.com/v1"

# DB-05 속성명 — 노션 DB 와 1:1 일치해야 한다 (preflight 가 검사)
P_TITLE = "회차키"
P_NO = "일지번호"
P_STATE = "게시상태"
P_REVIEWED = "검수완료"
P_FEED_TEXT = "피드본문"
P_REEL_TEXT = "릴스캡션"
P_FEED_IMAGE = "피드이미지"
P_REEL_VIDEO = "릴스영상"
P_SCHEDULE = "예약일시"
P_FEED_ID = "FB피드ID"
P_REEL_ID = "FB릴스ID"
P_PUBLISHED_AT = "발행일시"
P_RESULT = "결과코드"

REQUIRED_PROPERTIES = {
    P_TITLE: "title",
    P_NO: "number",
    P_STATE: "select",
    P_REVIEWED: "checkbox",
    P_FEED_TEXT: "rich_text",
    P_REEL_TEXT: "rich_text",
    P_FEED_IMAGE: "files",
    P_REEL_VIDEO: "files",
    P_SCHEDULE: "date",
    P_FEED_ID: "rich_text",
    P_REEL_ID: "rich_text",
    P_PUBLISHED_AT: "date",
    P_RESULT: "rich_text",
}

# 게시상태 값
S_DRAFT = "초안"
S_REVIEW = "검수중"
S_APPROVED = "승인"
S_RUNNING = "발행중"
S_DONE = "발행완료"
S_PARTIAL = "부분완료"
S_FAILED = "실패"
S_CHECK = "확인필요"
STATE_OPTIONS = (S_DRAFT, S_REVIEW, S_APPROVED, S_RUNNING, S_DONE, S_PARTIAL, S_FAILED, S_CHECK)

# ── 알림 (선택) ───────────────────────────────────────────
# 전용 대상만. 투자 채널 등 다른 채널 ID 재사용 금지.
TELEGRAM_BOT_TOKEN = _env("FBDET_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("FBDET_TELEGRAM_CHAT_ID")
