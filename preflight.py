"""
fbpub/preflight.py
==================
운영 점검 (게시하지 않음). 주 1회 + 수동.

  P1 Graph : GET /{page-id}?fields=id,name — 페이지 토큰으로 페이지 접근 가능 여부
  P2 Graph : GET /debug_token (FBDET_APP_ID·FBDET_APP_SECRET 설정 시)
             is_valid, scopes ⊇ {pages_show_list, pages_read_engagement, pages_manage_posts}
  P3 Notion: GET /databases/{id} — DB-05 속성명·타입이 코드와 일치하는지, 게시상태 옵션 존재
실행: python -m fbpub.preflight   종료코드 0 = 전부 통과 / 2 = 하나 이상 실패
"""
from __future__ import annotations

import logging
import sys

from fbpub import graph_client, notify, notion_repo, settings
from fbpub.notion_repo import NotionError

VERSION = "1.0.0"

logger = logging.getLogger("fbpub.preflight")

REQUIRED_SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts")


def check_page() -> list[str]:
    if not settings.PAGE_ID or not settings.PAGE_TOKEN:
        return ["P1:page_not_configured"]
    status, body = graph_client.get_json(
        settings.PAGE_ID, {"fields": "id,name", "access_token": settings.PAGE_TOKEN}
    )
    if status != 200 or str(body.get("id", "")) != settings.PAGE_ID:
        code = (body.get("error") or {}).get("code")
        return [f"P1:page_access_failed:http_{status}:code={code}"]
    return []


def check_token() -> list[str]:
    if not settings.APP_ID or not settings.APP_SECRET:
        logger.info("[preflight] P2 생략 — FBDET_APP_ID/FBDET_APP_SECRET 미설정")
        return []
    status, body = graph_client.get_json(
        "debug_token",
        {"input_token": settings.PAGE_TOKEN, "access_token": f"{settings.APP_ID}|{settings.APP_SECRET}"},
    )
    data = body.get("data") or {}
    if status != 200 or not data:
        return [f"P2:debug_token_failed:http_{status}"]
    errors = []
    if not data.get("is_valid"):
        errors.append("P2:token_invalid")
    scopes = set(data.get("scopes") or [])
    missing = [s for s in REQUIRED_SCOPES if s not in scopes]
    if missing:
        errors.append(f"P2:scopes_missing:{','.join(missing)}")
    expires_at = data.get("expires_at")
    logger.info(f"[preflight] P2 토큰 유효={bool(data.get('is_valid'))} expires_at={expires_at}")
    return errors


def check_notion() -> list[str]:
    if not settings.NOTION_TOKEN or not settings.NOTION_DB_ID:
        return ["P3:notion_not_configured"]
    try:
        db = notion_repo.retrieve_database()
    except NotionError as e:
        return [f"P3:db_retrieve_failed:{e}"]
    props = db.get("properties") or {}
    errors = []
    for name, ptype in settings.REQUIRED_PROPERTIES.items():
        actual = (props.get(name) or {}).get("type")
        if actual != ptype:
            errors.append(f"P3:property:{name}:{actual or 'missing'}!={ptype}")
    options = {
        o.get("name")
        for o in ((props.get(settings.P_STATE) or {}).get("select") or {}).get("options", [])
    }
    missing = [s for s in settings.STATE_OPTIONS if s not in options]
    if missing:
        errors.append(f"P3:state_options_missing:{','.join(missing)}")
    return errors


def run() -> int:
    logger.info(f"[preflight] v{VERSION} 시작 (graph={settings.GRAPH_VERSION}, notion={settings.NOTION_VERSION})")
    errors = check_page() + check_token() + check_notion()
    if errors:
        logger.error(f"[preflight] 실패 {len(errors)}건: {'; '.join(errors)}")
        notify.send(f"⚠️ FB preflight 실패: {'; '.join(errors)[:600]}")
        return 2
    logger.info("[preflight] 전부 통과")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    sys.exit(run())


if __name__ == "__main__":
    main()
