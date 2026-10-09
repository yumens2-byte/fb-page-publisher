"""
fbpub/notion_repo.py
====================
노션 DB-05(회차 원장) 조회·선점·결과 기록.

Notion API (Notion-Version 2022-06-28)
  - POST  /v1/databases/{id}/query
  - GET   /v1/databases/{id}
  - GET   /v1/pages/{id}
  - GET   /v1/pages/{id}/properties/{property_id}   (rich_text 전체 조회, 페이지네이션)
  - PATCH /v1/pages/{id}
  - 파일 URL 은 "valid for one hour" → 조회 직후 다운로드한다.
  - 속성 ID 는 응답에 URL 인코딩된 상태로 온다 → "Pass the returned ID as-is ... Don't encode it a second time."
    (developers.notion.com/reference/page-property-values)
v1.1.0 (2026-10-09 검토 반영)
  - H1 속성 ID 이중 인코딩 제거
  - M3 조회 페이지네이션 (find_ready, 릴스 일일 상한 집계) + 상한 집계 날짜 필터
  - M4 429·5xx·연결 오류 재시도 (최대 3회, Retry-After 준수) — 노션 호출은 전부 멱등(조회/동일값 갱신)
  - 발행중 잔류 행 조회 (find_stuck)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from fbpub import settings
from fbpub.redact import redact

VERSION = "1.1.0"

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
_RICH_TEXT_CHUNK = 1900  # rich_text 객체당 2000자 제한 여유
RETRY_MAX = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)
_sleep = time.sleep  # 테스트에서 교체


class NotionError(RuntimeError):
    """노션 API 실패 (메시지는 마스킹됨)."""


@dataclass
class Episode:
    page_id: str
    number: int | None
    state: str
    reviewed: bool
    schedule_at: datetime | None
    feed_id: str
    reel_id: str
    feed_files: list[dict] = field(default_factory=list)
    reel_files: list[dict] = field(default_factory=list)
    prop_ids: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """로그용 식별자 — 일지번호만 (본문·ID 미출력)."""
        return f"#{self.number:03d}" if isinstance(self.number, int) else "#???"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.NOTION_TOKEN}",
        "Notion-Version": settings.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _retry_wait(resp, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "")), 30.0)
    except (AttributeError, TypeError, ValueError):
        return float(2 ** attempt)


def _request(method: str, path: str, json_body: dict | None = None) -> dict:
    """노션 호출. 429·5xx·연결 오류는 최대 RETRY_MAX 회 재시도 (조회 또는 동일값 갱신이라 멱등)."""
    resp = None
    for attempt in range(RETRY_MAX + 1):
        try:
            resp = requests.request(
                method,
                f"{settings.NOTION_BASE}{path}",
                headers=_headers(),
                json=json_body,
                timeout=settings.HTTP_TIMEOUT_SEC,
            )
        except requests.exceptions.RequestException as e:
            if attempt < RETRY_MAX:
                logger.warning(f"[Notion] {method} {type(e).__name__} — 재시도 {attempt + 1}/{RETRY_MAX}")
                _sleep(float(2 ** attempt))
                continue
            raise NotionError(f"{method} request_error:{type(e).__name__}:{redact(e)[:120]}") from None
        if resp.status_code in RETRY_STATUSES and attempt < RETRY_MAX:
            logger.warning(f"[Notion] {method} http_{resp.status_code} — 재시도 {attempt + 1}/{RETRY_MAX}")
            _sleep(_retry_wait(resp, attempt))
            continue
        break
    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = f"{err.get('code', '')}:{str(err.get('message', ''))[:160]}"
        except ValueError:
            msg = (resp.text or "")[:160]
        raise NotionError(f"{method} http_{resp.status_code}:{redact(msg)}")
    try:
        return resp.json()
    except ValueError:
        raise NotionError(f"{method} invalid_json") from None


# ── 파싱 ──────────────────────────────────────────────────
def _plain(rich: list[dict]) -> str:
    return "".join(str(r.get("plain_text", "")) for r in rich or [])


def _parse_date(value: dict | None) -> datetime | None:
    if not value or not value.get("start"):
        return None
    raw = value["start"]
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # 날짜만 있거나 시간대 없음 → KST 로 해석
        dt = dt.replace(tzinfo=KST)
    return dt


def parse_episode(page: dict) -> Episode:
    props = page.get("properties", {})

    def p(name: str) -> dict:
        return props.get(name) or {}

    number = p(settings.P_NO).get("number")
    sel = p(settings.P_STATE).get("select") or {}
    return Episode(
        page_id=page["id"],
        number=int(number) if isinstance(number, (int, float)) else None,
        state=str(sel.get("name", "")),
        reviewed=bool(p(settings.P_REVIEWED).get("checkbox")),
        schedule_at=_parse_date(p(settings.P_SCHEDULE).get("date")),
        feed_id=_plain(p(settings.P_FEED_ID).get("rich_text")).strip(),
        reel_id=_plain(p(settings.P_REEL_ID).get("rich_text")).strip(),
        feed_files=list(p(settings.P_FEED_IMAGE).get("files") or []),
        reel_files=list(p(settings.P_REEL_VIDEO).get("files") or []),
        prop_ids={name: str(v.get("id", "")) for name, v in props.items()},
    )


def file_url(item: dict) -> str:
    """Files 속성 항목 → 다운로드 URL. 노션 업로드(file) / 외부 링크(external) 모두 지원."""
    kind = item.get("type")
    if kind == "file":
        return str((item.get("file") or {}).get("url", ""))
    if kind == "external":
        return str((item.get("external") or {}).get("url", ""))
    return ""


# ── 조회 ──────────────────────────────────────────────────
def retrieve_database() -> dict:
    return _request("GET", f"/databases/{settings.NOTION_DB_ID}")


def find_ready(now: datetime | None = None) -> Episode | None:
    """게시상태=승인 AND 검수완료 AND (예약일시 비어 있음 OR ≤ now), 일지번호 오름차순 첫 건."""
    current = now or datetime.now(timezone.utc)
    body = {
        "filter": {
            "and": [
                {"property": settings.P_STATE, "select": {"equals": settings.S_APPROVED}},
                {"property": settings.P_REVIEWED, "checkbox": {"equals": True}},
            ]
        },
        "sorts": [{"property": settings.P_NO, "direction": "ascending"}],
        "page_size": 20,
    }
    for page in _query_all(body):
        ep = parse_episode(page)
        if ep.schedule_at is None or ep.schedule_at <= current:
            return ep
    return None


def _query_all(body: dict, max_pages: int = 20):
    """DB query 페이지네이션 (has_more / next_cursor)."""
    cursor = None
    for _ in range(max_pages):
        payload = dict(body)
        if cursor:
            payload["start_cursor"] = cursor
        data = _request("POST", f"/databases/{settings.NOTION_DB_ID}/query", payload)
        yield from data.get("results", [])
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            return


def find_stuck() -> list[Episode]:
    """게시상태=발행중 으로 남은 행 (이전 실행 중단 등). 자동 처리하지 않고 알림만 한다."""
    body = {"filter": {"property": settings.P_STATE, "select": {"equals": settings.S_RUNNING}}, "page_size": 20}
    return [parse_episode(p) for p in _query_all(body, max_pages=1)]


def count_reels_published_on(day_kst: str) -> int:
    """KST 날짜(YYYY-MM-DD)에 발행일시가 있고 FB릴스ID 가 채워진 행 수."""
    # 날짜 필터는 시간대 해석 차이를 피하려고 하루 앞에서 자르고, KST 날짜 일치는 코드에서 판정한다
    since = (datetime.fromisoformat(day_kst) - timedelta(days=1)).date().isoformat()
    body = {
        "filter": {
            "and": [
                {"property": settings.P_REEL_ID, "rich_text": {"is_not_empty": True}},
                {"property": settings.P_PUBLISHED_AT, "date": {"on_or_after": since}},
            ]
        },
        "page_size": 100,
    }
    count = 0
    for page in _query_all(body):
        value = (page.get("properties", {}).get(settings.P_PUBLISHED_AT) or {}).get("date")
        dt = _parse_date(value)
        if dt and dt.astimezone(KST).date().isoformat() == day_kst:
            count += 1
    return count


def get_text(ep: Episode, prop_name: str) -> str:
    """rich_text 속성 전체 (페이지 객체는 25개 항목까지만 반환하므로 property item 엔드포인트 사용)."""
    prop_id = ep.prop_ids.get(prop_name, "")
    if not prop_id:
        raise NotionError(f"property_missing:{prop_name}")
    parts: list[str] = []
    cursor = None
    while True:
        path = f"/pages/{ep.page_id}/properties/{prop_id}"  # 응답의 ID 를 그대로 사용 (재인코딩 금지)
        if cursor:
            path += f"?start_cursor={quote(cursor, safe='')}"
        data = _request("GET", path)
        for item in data.get("results", []):
            parts.append(str((item.get("rich_text") or {}).get("plain_text", "")))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return "".join(parts)


def reload(ep: Episode) -> Episode:
    return parse_episode(_request("GET", f"/pages/{ep.page_id}"))


# ── 쓰기 ──────────────────────────────────────────────────
def _rich(text: str) -> dict:
    text = text or ""
    chunks = [text[i:i + _RICH_TEXT_CHUNK] for i in range(0, len(text), _RICH_TEXT_CHUNK)]
    return {"rich_text": [{"type": "text", "text": {"content": c}} for c in chunks]}


def update(ep: Episode, props: dict) -> None:
    _request("PATCH", f"/pages/{ep.page_id}", {"properties": props})


def set_state(ep: Episode, state: str) -> None:
    update(ep, {settings.P_STATE: {"select": {"name": state}}})


def claim(ep: Episode) -> bool:
    """승인 → 발행중. 재조회로 확인. 다른 실행이 먼저 바꿨으면 False."""
    fresh = reload(ep)
    if fresh.state != settings.S_APPROVED:
        return False
    set_state(ep, settings.S_RUNNING)
    return reload(ep).state == settings.S_RUNNING


def save_object_id(ep: Episode, kind: str, object_id: str) -> None:
    prop = settings.P_FEED_ID if kind == "photo" else settings.P_REEL_ID
    update(ep, {prop: _rich(object_id)})


def finalize(ep: Episode, state: str, result_text: str, published_at: datetime | None) -> None:
    props: dict = {
        settings.P_STATE: {"select": {"name": state}},
        settings.P_RESULT: _rich(redact(result_text, include_ids=False)[:1900]),
    }
    if published_at is not None:
        props[settings.P_PUBLISHED_AT] = {"date": {"start": published_at.astimezone(KST).isoformat()}}
    update(ep, props)
