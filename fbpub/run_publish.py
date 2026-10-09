"""
fbpub/run_publish.py
====================
승인된 회차 1건 → Facebook 페이지 피드(사진) + 릴스 게시 → 노션 원장 기록.

실행: python -m fbpub.run_publish
종료코드: 0 = 정상(대상 없음·DRY_RUN 통과·발행완료·한도 이월 포함) / 2 = 실패·확인필요·부분완료 / 1 = 설정 오류

처리 순서
  0. 발행중 잔류 행 알림 (자동 처리하지 않음)
  1. 대상 조회 (게시상태=승인 AND 검수완료 AND 예약일시 도래) — 1건
  2. 선점 (승인 → 발행중)  ※ DRY_RUN 은 노션 쓰기 없음
  3. 본문·첨부 다운로드 + 규격·문구 검사 — 하나라도 불통과면 게시 시도 없이 실패
  4. 피드(사진) → ID 즉시 기록
  5. 릴스 → video_id 선기록 → 상태 폴링 → 결과
  6. 최종 상태 결정 + 알림
로그에는 일지번호·상태·사유 코드만 남긴다 (본문·ID 미출력).

v1.1.0 (2026-10-09 검토 반영)
  - M1 DRY_RUN 값이 true/false/빈 값이 아니면 설정 오류로 중단
  - M2 중단 신호(SIGTERM·SIGINT)·BaseException 시 쓰기 이후면 확인필요로 기록 후 종료, 발행중 잔류 행 알림
  - M5 게시 성공 후 ID 원장 기록이 실패하면 확인필요 + 결과코드·알림에 ID 포함 (재게시 방지)
  - L3 응답 미확정(unknown) 도 발행일시 기록 → 릴스 일일 상한 집계에 포함
  - L7 urllib3 로그 레벨 WARNING 고정 (DEBUG 시 쿼리 문자열 토큰 노출 방지)
  - L8 예외 traceback 마스킹 후 출력
"""
from __future__ import annotations

import logging
import signal
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fbpub import content_guard, graph_client, media_check, notify, notion_repo, settings
from fbpub.notion_repo import KST, Episode, NotionError
from fbpub.redact import redact

VERSION = "1.1.0"

logger = logging.getLogger("fbpub.run")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_FAIL = 2


class Terminated(BaseException):
    """SIGTERM 수신 (Actions 취소·타임아웃). BaseException 이라 일반 except 에 잡히지 않는다."""


def decide_state(results: dict[str, dict], id_unsaved: bool = False) -> str:
    """채널별 결과 → 게시상태. results 는 이번 실행에서 처리한 채널만 포함."""
    if id_unsaved:
        return settings.S_CHECK  # 게시는 됐지만 원장에 ID 가 없다 → 재게시 방지
    values = {v["status"] for v in results.values()}
    if "unknown" in values:
        return settings.S_CHECK
    if values <= {"ok", "already"}:
        return settings.S_DONE
    if "deferred" in values and not values & {"failed", "skipped"}:
        return settings.S_APPROVED  # 한도 이월 — 다음 실행에서 남은 채널만 게시
    if "ok" in values or "already" in values:
        return settings.S_PARTIAL
    return settings.S_FAILED


def _result_text(results: dict[str, dict], extra: list[str] | None = None) -> str:
    parts = []
    for kind, r in results.items():
        code = f"/code={r['error_code']}" if r.get("error_code") is not None else ""
        reason = f"/{r['reason']}" if r.get("reason") else ""
        parts.append(f"{kind}={r['status']}{reason}{code}")
    parts.extend(extra or [])
    return "; ".join(parts)


def _prepare(ep: Episode, workdir: Path, need_photo: bool, need_reel: bool) -> tuple[dict, list[str]]:
    """본문·첨부 준비 + 검사. (자료, 위반 코드 목록)"""
    prepared: dict = {}
    errors: list[str] = []
    if need_photo:
        caption = notion_repo.get_text(ep, settings.P_FEED_TEXT)
        errors += content_guard.check_feed_text(caption)
        if settings.PHOTO_AI_NOTICE:
            caption = f"{caption.rstrip()}\n\n{settings.PHOTO_AI_NOTICE}"
        prepared["caption"] = caption
        if len(ep.feed_files) != 1:
            errors.append(f"image:count:{len(ep.feed_files)}")
        else:
            try:
                path = media_check.download(notion_repo.file_url(ep.feed_files[0]), str(workdir / "feed_image"))
                media_check.check_image(path)
                prepared["image_path"] = path
            except media_check.MediaError as e:
                errors.append(str(e))
    if need_reel:
        description = notion_repo.get_text(ep, settings.P_REEL_TEXT)
        errors += content_guard.check_reel_text(description)
        prepared["description"] = description
        if len(ep.reel_files) != 1:
            errors.append(f"video:count:{len(ep.reel_files)}")
        else:
            try:
                path = media_check.download(notion_repo.file_url(ep.reel_files[0]), str(workdir / "reel_video.mp4"))
                media_check.check_video(path)
                prepared["video_path"] = path
            except media_check.MediaError as e:
                errors.append(str(e))
    return prepared, errors


def _config_errors() -> list[str]:
    errors = []
    if settings.DRY_RUN_INVALID:
        errors.append("dry_run_invalid(허용값: true|false)")
    if not settings.NOTION_TOKEN or not settings.NOTION_DB_ID:
        errors.append("notion_not_configured")
    if not settings.DRY_RUN and (not settings.PAGE_ID or not settings.PAGE_TOKEN):
        errors.append("page_not_configured")
    return errors


def _alert_stuck() -> None:
    try:
        stuck = notion_repo.find_stuck()
    except NotionError as e:
        logger.warning(f"[fbpub] 발행중 잔류 조회 실패(진행): {e}")
        return
    if stuck:
        labels = ",".join(ep.label for ep in stuck)
        logger.warning(f"[fbpub] 발행중 잔류 행: {labels} — 페이지 확인 후 수동 처리 필요")
        notify.send(f"⚠️ FB 게시 발행중 잔류: {labels} — 페이지 확인 후 상태 수동 정리 (자동 재게시 안 함)")


class _Run:
    """1건 처리 상태 — 예외·중단 시에도 기록할 정보를 보관한다."""

    def __init__(self, ep: Episode):
        self.ep = ep
        self.results: dict[str, dict] = {}
        self.extra: list[str] = []
        self.wrote = False        # Graph 쓰기 요청을 보냈는지
        self.id_unsaved = False   # 게시 성공 ID 를 원장에 못 남겼는지

    def record_id(self, kind: str, object_id: str) -> None:
        """게시 ID 원장 기록. 실패해도 예외를 내지 않고 결과코드·알림에 ID 를 남긴다 (M5)."""
        try:
            notion_repo.save_object_id(self.ep, kind, object_id)
        except NotionError as e:
            logger.error(f"[fbpub] {self.ep.label} {kind} ID 원장 기록 실패: {e}")
            self.id_unsaved = True
            self.extra.append(f"{kind}_id_unsaved={object_id}")

    def summary(self) -> str:
        return _result_text(self.results, self.extra)


def run(
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    mode = "DRY_RUN" if settings.DRY_RUN else "LIVE"
    logger.info(f"[fbpub] run_publish v{VERSION} 시작 ({mode}, graph={settings.GRAPH_VERSION})")

    cfg = _config_errors()
    if cfg:
        logger.error(f"[fbpub] 설정 오류: {','.join(cfg)}")
        notify.send(f"⚠️ FB 게시 설정 오류: {','.join(cfg)}")
        return EXIT_CONFIG

    _alert_stuck()

    try:
        ep = notion_repo.find_ready(now())
    except NotionError as e:
        logger.error(f"[fbpub] 대상 조회 실패: {e}")
        notify.send(f"⚠️ FB 게시 대상 조회 실패: {e}")
        return EXIT_FAIL
    if ep is None:
        logger.info("[fbpub] 게시 대상 없음 — 종료")
        return EXIT_OK

    label = ep.label
    need_photo = not ep.feed_id
    need_reel = not ep.reel_id
    logger.info(f"[fbpub] 대상 {label} (photo={'필요' if need_photo else '기존'}, reel={'필요' if need_reel else '기존'})")

    if not settings.DRY_RUN:
        try:
            if not notion_repo.claim(ep):
                logger.info(f"[fbpub] {label} 선점 실패(다른 실행 또는 상태 변경) — 종료")
                return EXIT_OK
        except NotionError as e:
            logger.error(f"[fbpub] {label} 선점 중 오류: {e}")
            notify.send(f"⚠️ FB 게시 {label} 선점 오류: {e}")
            return EXIT_FAIL

    st = _Run(ep)
    try:
        code = _publish(st, need_photo, need_reel, now, sleep, clock)
        if code is not None:
            return code
    except NotionError as e:
        state = settings.S_CHECK if (st.wrote or st.id_unsaved) else settings.S_FAILED
        logger.error(f"[fbpub] {label} 노션 오류 → {state}: {e}")
        _safe_finalize(st, state, f"notion_error:{e}")
        return EXIT_FAIL
    except Exception as e:  # noqa: BLE001 — 예기치 못한 오류도 원장 상태로 남긴다
        state = settings.S_CHECK if (st.wrote or st.id_unsaved) else settings.S_FAILED
        logger.error(f"[fbpub] {label} 예외 → {state}: {redact(traceback.format_exc())[-1500:]}")
        _safe_finalize(st, state, f"exception:{type(e).__name__}")
        return EXIT_FAIL
    except BaseException as e:  # 중단 신호(SIGTERM·SIGINT) — 기록 후 다시 올린다
        state = settings.S_CHECK if (st.wrote or st.id_unsaved) else settings.S_FAILED
        logger.error(f"[fbpub] {label} 실행 중단({type(e).__name__}) → {state}")
        if not settings.DRY_RUN:
            _safe_finalize(st, state, f"interrupted:{type(e).__name__}")
        raise

    if settings.DRY_RUN:
        logger.info(f"[fbpub][DRY_RUN] {label} 검사 통과, 게시 생략: {st.summary()}")
        return EXIT_OK

    state = decide_state(st.results, st.id_unsaved)
    touched = any(r["status"] in ("ok", "unknown") for r in st.results.values())
    published_at = now() if touched else None
    _safe_finalize(st, state, "", published_at, notify_result=True)
    logger.info(f"[fbpub] {label} 결과: {state} ({_result_text(st.results)})")
    return EXIT_OK if state in (settings.S_DONE, settings.S_APPROVED) else EXIT_FAIL


def _publish(st: _Run, need_photo: bool, need_reel: bool, now, sleep, clock) -> int | None:
    """게시 본체. 조기 종료 시 종료코드, 정상 진행 시 None."""
    ep = st.ep
    with tempfile.TemporaryDirectory(prefix="fbpub_") as tmp:
        prepared, errors = _prepare(ep, Path(tmp), need_photo, need_reel)
        if errors:
            logger.error(f"[fbpub] {ep.label} 사전 검사 불통과: {','.join(errors)}")
            if not settings.DRY_RUN:
                notion_repo.finalize(ep, settings.S_FAILED, "precheck:" + ",".join(errors), None)
            notify.send(f"⚠️ FB 게시 {ep.label} 사전 검사 불통과: {','.join(errors)[:300]}")
            return EXIT_FAIL

        # 4) 피드
        if need_photo:
            st.wrote = not settings.DRY_RUN
            r = graph_client.post_photo(prepared["image_path"], prepared["caption"])
            st.results["photo"] = r
            if r["status"] == "ok":
                st.record_id("photo", r["object_id"])
        else:
            st.results["photo"] = graph_client.result("already", "photo")

        # 5) 릴스
        if not need_reel:
            st.results["reel"] = graph_client.result("already", "reel")
            return None
        photo_status = st.results["photo"]["status"]
        if photo_status not in ("ok", "already", "dry_run"):
            st.results["reel"] = graph_client.result("skipped", "reel", reason=f"photo_{photo_status}")
            return None
        if not settings.DRY_RUN and (
            notion_repo.count_reels_published_on(now().astimezone(KST).date().isoformat()) >= settings.REELS_DAILY_CAP
        ):
            st.results["reel"] = graph_client.result("deferred", "reel", reason="daily_cap")
            return None

        st.wrote = st.wrote or not settings.DRY_RUN
        r = graph_client.post_reel(
            prepared["video_path"],
            prepared["description"],
            on_video_id=None if settings.DRY_RUN else (lambda vid: st.record_id("reel", vid)),
            sleep=sleep,
            clock=clock,
        )
        st.results["reel"] = r
        if r["status"] == "failed" and r.get("object_id") and not st.id_unsaved:
            # 게시되지 않은 영상 객체 ID 는 원장에서 비워 재처리 시 새로 업로드
            notion_repo.save_object_id(ep, "reel", "")
    return None


def _safe_finalize(st: _Run, state: str, prefix: str, published_at: datetime | None = None,
                   notify_result: bool = False) -> None:
    summary = st.summary()
    text = "; ".join(x for x in (prefix, summary) if x)
    try:
        notion_repo.finalize(st.ep, state, text, published_at)
        recorded = True
    except NotionError as e:
        logger.error(f"[fbpub] {st.ep.label} 최종 상태 기록 실패({state}): {e}")
        recorded = False
    ok_states = (settings.S_DONE, settings.S_APPROVED)
    icon = "📓" if state in ok_states else "⚠️"
    tail = "" if recorded else " — 노션 기록 실패, 수동 확인 필요"
    if notify_result or state not in ok_states or not recorded:
        notify.send(f"{icon} FB 게시 {st.ep.label}: {state} ({text}){tail}")


def _on_sigterm(signum, frame):  # noqa: ARG001
    raise Terminated(f"signal {signum}")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # 쿼리 문자열 토큰 노출 방지 (L7)
    signal.signal(signal.SIGTERM, _on_sigterm)
    sys.exit(run())


if __name__ == "__main__":
    main()
