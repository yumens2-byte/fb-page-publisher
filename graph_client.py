"""
fbpub/graph_client.py
=====================
Facebook Graph API — 페이지 사진 게시 / 릴스 게시 / 릴스 상태 조회.

공식 문서
  - 사진 : POST /{page-id}/photos  source(multipart) + caption → id, post_id
           (Page Photos 레퍼런스: message 는 Deprecated, caption 사용 / 10MB 제한)
  - 릴스 : POST /{page-id}/video_reels upload_phase=start → video_id, upload_url
           POST upload_url (rupload.facebook.com) 헤더 Authorization: OAuth, offset, file_size
           POST /{page-id}/video_reels upload_phase=finish, video_state=PUBLISHED, description
           GET  /{video-id}?fields=status
           (Reels publishing 가이드, page/video_reels 레퍼런스: is_ai_generated)

운영 원칙
  - 쓰기 요청은 재시도하지 않는다 (중복 게시 방지). 읽기(status)만 폴링.
  - status
      ok       : 게시 확정
      failed   : 게시되지 않음이 확정 (4xx, 연결 수립 실패, 설정 누락, 처리 오류)
      unknown  : 요청 전송 후 결과 미확정 (읽기 타임아웃, 연결 끊김, 5xx, 폴링 시간 초과)
      dry_run  : DRY_RUN — 요청 없음
  - 예외를 밖으로 던지지 않는다. 토큰·ID 는 결과·로그에 남기지 않는다.
v1.1.0 (2026-10-09 검토 반영): 상태 폴링이 마감 시각을 넘지 않도록 요청 타임아웃을 남은 시간으로 제한 (M2)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

import requests

from fbpub import settings
from fbpub.redact import redact

VERSION = "1.1.0"

logger = logging.getLogger(__name__)


def result(
    status: str,
    kind: str,
    object_id: str = "",
    reason: str = "",
    error_code: int | None = None,
    extra: dict | None = None,
) -> dict:
    out = {
        "success": status in ("ok", "dry_run"),
        "status": status,
        "kind": kind,
        "object_id": object_id,
        "reason": reason,
        "error_code": error_code,
        "dry_run": status == "dry_run",
    }
    if extra:
        out.update(extra)
    return out


def classify_error(code: int | None, http_status: int) -> str:
    if code == 190 or http_status == 401:
        return "auth_error"
    if code == 10 or (code is not None and 200 <= code <= 299):
        return "permission_error"
    if code in (4, 17, 32, 613, 80001):
        return "rate_limited"
    if code == 368:
        return "policy_blocked"
    if code == 506:
        return "duplicate"
    if code in (324, 6000):
        return "media_error"
    if code == 100:
        return "invalid_param"
    return "api_error"


def _parse_error(resp: requests.Response) -> tuple[int | None, str]:
    try:
        err = resp.json().get("error", {}) or {}
        return err.get("code"), str(err.get("message", ""))[:200]
    except ValueError:
        return None, (resp.text or "")[:200]


def _not_configured() -> bool:
    return not settings.PAGE_ID or not settings.PAGE_TOKEN


def _write(kind: str, step: str, send: Callable[[], requests.Response]) -> tuple[dict | None, dict | None]:
    """쓰기 요청 1회. (성공 시 body, 실패 시 result) 중 하나를 채워 반환."""
    try:
        resp = send()
    except requests.exceptions.ConnectTimeout as e:
        logger.error(f"[Graph] {kind}/{step} 연결 실패(미전송): {redact(e)}")
        return None, result("failed", kind, reason=f"{step}:connect_timeout")
    except requests.exceptions.ConnectionError as e:
        # 연결 수립 단계 오류인지 전송 후 끊김인지 구분할 수 없으므로 보수적으로 unknown
        logger.error(f"[Graph] {kind}/{step} 연결 오류 — 게시 여부 확인 필요: {redact(e)}")
        return None, result("unknown", kind, reason=f"{step}:connection_error")
    except requests.exceptions.RequestException as e:
        logger.error(f"[Graph] {kind}/{step} 응답 미확정 — 게시 여부 확인 필요: {redact(e)}")
        return None, result("unknown", kind, reason=f"{step}:request_error:{type(e).__name__}")
    except OSError as e:
        logger.error(f"[Graph] {kind}/{step} 파일 읽기 실패: {redact(e)}")
        return None, result("failed", kind, reason=f"{step}:file_read_error")

    if resp.status_code == 200:
        try:
            return resp.json(), None
        except ValueError:
            logger.error(f"[Graph] {kind}/{step} 200 응답 JSON 파싱 실패 — 확인 필요")
            return None, result("unknown", kind, reason=f"{step}:invalid_json")

    code, message = _parse_error(resp)
    if resp.status_code >= 500:
        logger.error(f"[Graph] {kind}/{step} 서버 오류 {resp.status_code} (code={code}) — 확인 필요: {redact(message)}")
        return None, result("unknown", kind, reason=f"{step}:http_{resp.status_code}", error_code=code)

    reason = classify_error(code, resp.status_code)
    logger.error(f"[Graph] {kind}/{step} 실패 {resp.status_code} {reason} (code={code}): {redact(message)}")
    return None, result("failed", kind, reason=f"{step}:{reason}", error_code=code)


# ── 사진 ──────────────────────────────────────────────────
def post_photo(image_path: str, caption: str) -> dict:
    kind = "photo"
    if not caption or not caption.strip():
        return result("failed", kind, reason="empty_caption")
    if settings.DRY_RUN:
        logger.info(f"[Graph][DRY_RUN] POST /{{page-id}}/photos 생략 (caption {len(caption)}자)")
        return result("dry_run", kind)
    if _not_configured():
        return result("failed", kind, reason="not_configured")

    url = f"{settings.GRAPH_BASE}/{settings.PAGE_ID}/photos"

    def send() -> requests.Response:
        with open(image_path, "rb") as fh:
            return requests.post(
                url,
                data={"caption": caption, "access_token": settings.PAGE_TOKEN},
                files={"source": fh},
                timeout=settings.UPLOAD_TIMEOUT_SEC,
            )

    body, fail = _write(kind, "photos", send)
    if fail:
        return fail
    post_id = str(body.get("post_id") or body.get("id") or "")
    if not post_id:
        logger.error("[Graph] photo 200 응답에 id 없음 — 확인 필요")
        return result("unknown", kind, reason="photos:no_post_id")
    logger.info("[Graph] photo 게시 완료")
    return result("ok", kind, object_id=post_id)


# ── 릴스 ──────────────────────────────────────────────────
def get_reel_status(video_id: str, timeout: float | None = None) -> dict:
    """읽기 요청. 반환: {"ok": bool, "status": {...} | None, "reason": str}"""
    try:
        resp = requests.get(
            f"{settings.GRAPH_BASE}/{video_id}",
            params={"fields": "status", "access_token": settings.PAGE_TOKEN},
            timeout=timeout if timeout is not None else settings.HTTP_TIMEOUT_SEC,
        )
    except requests.exceptions.RequestException as e:
        return {"ok": False, "status": None, "reason": f"status_request_error:{type(e).__name__}:{redact(e)[:80]}"}
    if resp.status_code != 200:
        code, _ = _parse_error(resp)
        return {"ok": False, "status": None, "reason": f"status_http_{resp.status_code}:code={code}"}
    try:
        return {"ok": True, "status": resp.json().get("status") or {}, "reason": ""}
    except ValueError:
        return {"ok": False, "status": None, "reason": "status_invalid_json"}


def interpret_reel_status(status: dict) -> str:
    """'published' | 'error' | 'pending'"""
    for phase in ("uploading_phase", "processing_phase", "publishing_phase"):
        if str((status.get(phase) or {}).get("status", "")).lower() == "error":
            return "error"
    if str(status.get("video_status", "")).lower() == "error":
        return "error"
    if str((status.get("publishing_phase") or {}).get("status", "")).lower() == "complete":
        return "published"
    return "pending"


def wait_reel_published(
    video_id: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    kind = "reel"
    deadline = clock() + settings.REEL_POLL_MAX_SEC
    last_reason = ""
    while True:
        remaining = max(deadline - clock(), 1.0)
        st = get_reel_status(video_id, timeout=min(float(settings.HTTP_TIMEOUT_SEC), remaining))
        if st["ok"]:
            verdict = interpret_reel_status(st["status"])
            if verdict == "published":
                logger.info("[Graph] reel 게시 완료(publishing complete)")
                return result("ok", kind, object_id=video_id)
            if verdict == "error":
                logger.error("[Graph] reel 처리 오류 상태")
                return result("failed", kind, object_id=video_id, reason="status:error_phase")
            last_reason = "pending"
        else:
            last_reason = st["reason"]
        if clock() >= deadline:
            logger.error(f"[Graph] reel 상태 확인 시간 초과 — 확인 필요 ({last_reason})")
            return result("unknown", kind, object_id=video_id, reason=f"status:timeout:{last_reason}")
        sleep(min(float(settings.REEL_POLL_INTERVAL_SEC), max(deadline - clock(), 0.0)))


def post_reel(
    video_path: str,
    description: str,
    on_video_id: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """
    start → upload → finish → status 폴링.
    on_video_id: start 성공 직후 호출 (원장 선기록용). 콜백 예외는 로그만 남기고 진행한다.
    """
    kind = "reel"
    if not description or not description.strip():
        return result("failed", kind, reason="empty_description")
    if settings.DRY_RUN:
        logger.info(f"[Graph][DRY_RUN] video_reels start/upload/finish 생략 (description {len(description)}자)")
        return result("dry_run", kind)
    if _not_configured():
        return result("failed", kind, reason="not_configured")
    try:
        file_size = os.path.getsize(video_path)
    except OSError:
        return result("failed", kind, reason="video_missing")

    endpoint = f"{settings.GRAPH_BASE}/{settings.PAGE_ID}/video_reels"

    # 1) start
    body, fail = _write(kind, "start", lambda: requests.post(
        endpoint,
        data={"upload_phase": "start", "access_token": settings.PAGE_TOKEN},
        timeout=settings.HTTP_TIMEOUT_SEC,
    ))
    if fail:
        # start 응답 미확정이어도 업로드·finish 전이므로 게시물은 생기지 않는다
        if fail["status"] == "unknown":
            fail["status"] = "failed"
            fail["success"] = False
        return fail
    video_id = str(body.get("video_id") or "")
    upload_url = str(body.get("upload_url") or "")
    if not video_id or not upload_url:
        return result("failed", kind, reason="start:missing_video_id_or_upload_url")
    if on_video_id:
        try:
            on_video_id(video_id)
        except Exception as e:  # noqa: BLE001 — 원장 기록 실패가 게시 흐름을 막지 않게
            logger.warning(f"[Graph] video_id 선기록 실패(진행): {redact(e)}")

    # 2) upload (rupload)
    def send_upload() -> requests.Response:
        with open(video_path, "rb") as fh:
            return requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {settings.PAGE_TOKEN}",
                    "offset": "0",
                    "file_size": str(file_size),
                },
                data=fh,
                timeout=settings.UPLOAD_TIMEOUT_SEC,
            )

    body, fail = _write(kind, "upload", send_upload)
    if fail:
        # finish 전이므로 게시물은 생기지 않는다
        fail.update(object_id=video_id, status="failed", success=False)
        return fail
    if not body.get("success"):
        return result("failed", kind, object_id=video_id, reason="upload:success_false")

    # 3) finish
    finish_data: dict[str, Any] = {
        "access_token": settings.PAGE_TOKEN,
        "video_id": video_id,
        "upload_phase": "finish",
        "video_state": "PUBLISHED",
        "description": description,
    }
    if settings.REEL_AI_FLAG:
        finish_data["is_ai_generated"] = "true"
    body, fail = _write(kind, "finish", lambda: requests.post(
        endpoint, data=finish_data, timeout=settings.HTTP_TIMEOUT_SEC,
    ))
    if fail:
        fail["object_id"] = video_id
        return fail
    if not body.get("success"):
        return result("unknown", kind, object_id=video_id, reason="finish:success_false")

    # 4) status 폴링 (읽기)
    return wait_reel_published(video_id, sleep=sleep, clock=clock)


# ── Preflight 용 읽기 ─────────────────────────────────────
def get_json(path: str, params: dict) -> tuple[int, dict]:
    try:
        resp = requests.get(f"{settings.GRAPH_BASE}/{path}", params=params, timeout=settings.HTTP_TIMEOUT_SEC)
    except requests.exceptions.RequestException as e:
        return 0, {"error": {"message": redact(e)[:200]}}
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}
