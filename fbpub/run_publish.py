"""
fbpub/run_publish.py
====================
승인된 회차 1건 → Facebook 페이지 피드(사진) + 릴스 게시 → 노션 원장 기록.

실행: python -m fbpub.run_publish
종료코드: 0 = 정상(대상 없음·DRY_RUN 통과·발행완료·한도 이월 포함) / 2 = 실패·확인필요·부분완료 / 1 = 설정 오류

처리 순서
  1. 대상 조회 (게시상태=승인 AND 검수완료 AND 예약일시 도래) — 1건
  2. 선점 (승인 → 발행중)  ※ DRY_RUN 은 노션 쓰기 없음
  3. 본문·첨부 다운로드 + 규격·문구 검사 — 하나라도 불통과면 게시 시도 없이 실패
  4. 피드(사진) → ID 즉시 기록
  5. 릴스 → video_id 선기록 → 상태 폴링 → 결과
  6. 최종 상태 결정 + 알림
로그에는 일지번호·상태·사유 코드만 남긴다 (본문·ID 미출력).
"""
from __future__ import annotations

import logging
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fbpub import content_guard, graph_client, media_check, notify, notion_repo, settings
from fbpub.notion_repo import KST, Episode, NotionError
from fbpub.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger("fbpub.run")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_FAIL = 2


def decide_state(results: dict[str, dict]) -> str:
    """채널별 결과 → 게시상태. results 는 이번 실행에서 처리한 채널만 포함."""
    statuses = {k: v["status"] for k, v in results.items()}
    values = set(statuses.values())
    if "unknown" in values:
        return settings.S_CHECK
    if values <= {"ok", "already"}:
        return settings.S_DONE
    if "deferred" in values and not values & {"failed", "skipped"}:
        return settings.S_APPROVED  # 한도 이월 — 다음 실행에서 남은 채널만 게시
    if "ok" in values or "already" in values:
        return settings.S_PARTIAL
    return settings.S_FAILED


def _result_text(results: dict[str, dict], extra: str = "") -> str:
    parts = []
    for kind, r in results.items():
        code = f"/code={r['error_code']}" if r.get("error_code") is not None else ""
        reason = f"/{r['reason']}" if r.get("reason") else ""
        parts.append(f"{kind}={r['status']}{reason}{code}")
    if extra:
        parts.append(extra)
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
    if not settings.NOTION_TOKEN or not settings.NOTION_DB_ID:
        errors.append("notion_not_configured")
    if not settings.DRY_RUN and (not settings.PAGE_ID or not settings.PAGE_TOKEN):
        errors.append("page_not_configured")
    return errors


def run(
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    mode = "DRY_RUN" if settings.DRY_RUN else "LIVE"
    logger.info(f"[fbpub] run_publish v{VERSION} 시작 ({mode}, graph={settings.GRAPH_VERSION})")

    cfg = _config_errors()
    if cfg:
        logger.error(f"[fbpub] 설정 누락: {','.join(cfg)}")
        notify.send(f"⚠️ FB 게시 설정 누락: {','.join(cfg)}")
        return EXIT_CONFIG

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

    results: dict[str, dict] = {}
    wrote = False  # Graph 쓰기 요청을 보냈는지 (예외 시 상태 판단용)
    try:
        with tempfile.TemporaryDirectory(prefix="fbpub_") as tmp:
            prepared, errors = _prepare(ep, Path(tmp), need_photo, need_reel)
            if errors:
                logger.error(f"[fbpub] {label} 사전 검사 불통과: {','.join(errors)}")
                if not settings.DRY_RUN:
                    notion_repo.finalize(ep, settings.S_FAILED, "precheck:" + ",".join(errors), None)
                notify.send(f"⚠️ FB 게시 {label} 사전 검사 불통과: {','.join(errors)[:300]}")
                return EXIT_FAIL

            # 4) 피드
            if need_photo:
                wrote = not settings.DRY_RUN
                r = graph_client.post_photo(prepared["image_path"], prepared["caption"])
                results["photo"] = r
                if r["status"] == "ok":
                    notion_repo.save_object_id(ep, "photo", r["object_id"])
            else:
                results["photo"] = graph_client.result("already", "photo")

            # 5) 릴스
            if need_reel:
                photo_status = results["photo"]["status"]
                if photo_status not in ("ok", "already", "dry_run"):
                    results["reel"] = graph_client.result("skipped", "reel", reason=f"photo_{photo_status}")
                elif not settings.DRY_RUN and (
                    notion_repo.count_reels_published_on(now().astimezone(KST).date().isoformat())
                    >= settings.REELS_DAILY_CAP
                ):
                    results["reel"] = graph_client.result("deferred", "reel", reason="daily_cap")
                else:
                    wrote = wrote or not settings.DRY_RUN

                    def keep_video_id(video_id: str) -> None:
                        notion_repo.save_object_id(ep, "reel", video_id)

                    results["reel"] = graph_client.post_reel(
                        prepared["video_path"],
                        prepared["description"],
                        on_video_id=None if settings.DRY_RUN else keep_video_id,
                        sleep=sleep,
                        clock=clock,
                    )
                    if results["reel"]["status"] == "failed" and results["reel"].get("object_id"):
                        # 게시되지 않은 영상 객체 ID 는 원장에서 비워 재처리 시 새로 업로드
                        notion_repo.save_object_id(ep, "reel", "")
            else:
                results["reel"] = graph_client.result("already", "reel")
    except NotionError as e:
        state = settings.S_CHECK if wrote else settings.S_FAILED
        logger.error(f"[fbpub] {label} 노션 오류 → {state}: {e}")
        _safe_finalize(ep, state, f"notion_error:{e}; " + _result_text(results))
        notify.send(f"⚠️ FB 게시 {label} 노션 오류 → {state}")
        return EXIT_FAIL
    except Exception as e:  # noqa: BLE001 — 예기치 못한 오류도 원장 상태로 남긴다
        state = settings.S_CHECK if wrote else settings.S_FAILED
        logger.exception(f"[fbpub] {label} 예외 → {state}: {redact(e)}")
        _safe_finalize(ep, state, f"exception:{type(e).__name__}; " + _result_text(results))
        notify.send(f"⚠️ FB 게시 {label} 예외 → {state}")
        return EXIT_FAIL

    summary = _result_text(results)
    if settings.DRY_RUN:
        logger.info(f"[fbpub][DRY_RUN] {label} 검사 통과, 게시 생략: {summary}")
        return EXIT_OK

    state = decide_state(results)
    published_at = now() if any(r["status"] == "ok" for r in results.values()) else None
    _safe_finalize(ep, state, summary, published_at)
    logger.info(f"[fbpub] {label} 결과: {state} ({summary})")
    icon = "📓" if state in (settings.S_DONE, settings.S_APPROVED) else "⚠️"
    notify.send(f"{icon} FB 게시 {label}: {state} ({summary})")
    return EXIT_OK if state in (settings.S_DONE, settings.S_APPROVED) else EXIT_FAIL


def _safe_finalize(ep: Episode, state: str, text: str, published_at: datetime | None = None) -> None:
    try:
        notion_repo.finalize(ep, state, text, published_at)
    except NotionError as e:
        logger.error(f"[fbpub] {ep.label} 최종 상태 기록 실패({state}): {e}")
        notify.send(f"⚠️ FB 게시 {ep.label} 상태 기록 실패 — 노션 수동 확인 필요 ({state})")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    sys.exit(run())


if __name__ == "__main__":
    main()
