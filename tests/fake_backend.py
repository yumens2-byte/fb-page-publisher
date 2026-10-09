"""
노션 API + Graph API + 파일 다운로드를 흉내 내는 테스트용 가짜 백엔드.
requests.request / requests.post / requests.get 을 대체해 URL 로 라우팅한다.
모든 호출은 trace 에 기록된다 (토큰 값은 마스킹).
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from unittest.mock import MagicMock

from conftest import DB_ID, NOTION_TOKEN, PAGE, TOKEN

from fbpub import settings

PROP_IDS = {name: f"p{i:02d}" for i, name in enumerate(settings.REQUIRED_PROPERTIES)}


def _mask(obj):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    for secret in (TOKEN, NOTION_TOKEN):
        s = s.replace(secret, "***")
    return json.loads(s)


def _resp(status: int, body=None, content: bytes = b""):
    r = MagicMock()
    r.status_code = status
    if body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    r.text = json.dumps(body, ensure_ascii=False) if body is not None else ""
    r.iter_content.return_value = [content] if content else []
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


def rich(text: str) -> dict:
    return {"rich_text": [{"type": "text", "plain_text": text, "text": {"content": text}}] if text else []}


def make_row(page_id: str, number: int, state: str, reviewed: bool = True, feed_text: str = "", reel_text: str = "",
             feed_url: str = "https://files.invalid/feed", reel_url: str = "https://files.invalid/reel",
             schedule: str | None = None, feed_id: str = "", reel_id: str = "", published_at: str | None = None) -> dict:
    def files(url):
        return {"files": [{"name": "f", "type": "file", "file": {"url": url, "expiry_time": "2099-01-01T00:00:00Z"}}]
                if url else []}

    props = {
        settings.P_TITLE: {"title": [{"plain_text": f"수사일지 #{number:03d}"}]},
        settings.P_NO: {"number": number},
        settings.P_STATE: {"select": {"name": state}},
        settings.P_REVIEWED: {"checkbox": reviewed},
        settings.P_FEED_TEXT: rich(feed_text),
        settings.P_REEL_TEXT: rich(reel_text),
        settings.P_FEED_IMAGE: files(feed_url),
        settings.P_REEL_VIDEO: files(reel_url),
        settings.P_SCHEDULE: {"date": {"start": schedule} if schedule else None},
        settings.P_FEED_ID: rich(feed_id),
        settings.P_REEL_ID: rich(reel_id),
        settings.P_PUBLISHED_AT: {"date": {"start": published_at} if published_at else None},
        settings.P_RESULT: rich(""),
    }
    for name, v in props.items():
        v["id"] = PROP_IDS[name]
        v["type"] = settings.REQUIRED_PROPERTIES[name]
    return {"object": "page", "id": page_id, "properties": props}


class FakeBackend:
    def __init__(self, rows: list[dict], files: dict[str, bytes]):
        self.pages = {r["id"]: r for r in rows}
        self.files = files
        self.trace: list[dict] = []
        self.graph_post_override: dict[str, list] = {}  # step → [응답 또는 예외] 순서
        self.reel_status = [{"status": {"publishing_phase": {"status": "complete"},
                                        "processing_phase": {"status": "complete"},
                                        "uploading_phase": {"status": "complete"}, "video_status": "ready"}}]
        self.notion_fail = False
        self.on_patch = None  # 경쟁 상황 시뮬레이션용 훅
        self.video_seq = 0

    # ── 기록 ──
    def log(self, method, url, payload=None, status=None):
        url = re.sub(r"(access_token=)[^&]+", r"\1***", url)
        self.trace.append(_mask({"method": method, "url": url, "payload": payload, "status": status}))

    # ── 노션 ──
    def _match(self, page, cond) -> bool:
        prop = page["properties"][cond["property"]]
        if "select" in cond:
            return (prop.get("select") or {}).get("name") == cond["select"]["equals"]
        if "checkbox" in cond:
            return bool(prop.get("checkbox")) == cond["checkbox"]["equals"]
        if "rich_text" in cond:
            return bool("".join(x["plain_text"] for x in prop["rich_text"]))
        if "date" in cond:
            return bool(prop.get("date"))
        raise AssertionError(cond)

    def notion(self, method, url, headers=None, json=None, timeout=None):  # noqa: A002
        assert headers["Authorization"] == f"Bearer {NOTION_TOKEN}"
        assert headers["Notion-Version"] == "2022-06-28"
        path = url.replace(settings.NOTION_BASE, "")
        self.log(method, path, json)
        if self.notion_fail:
            return _resp(503, {"object": "error", "code": "service_unavailable", "message": "down"})
        if method == "POST" and path == f"/databases/{DB_ID}/query":
            conds = json["filter"]["and"]
            hits = [copy.deepcopy(p) for p in self.pages.values() if all(self._match(p, c) for c in conds)]
            hits.sort(key=lambda p: p["properties"][settings.P_NO]["number"])
            return _resp(200, {"results": hits, "has_more": False})
        if method == "GET" and path == f"/databases/{DB_ID}":
            return _resp(200, {"properties": {
                n: {"id": PROP_IDS[n], "type": t, **({"select": {"options": [{"name": s} for s in settings.STATE_OPTIONS]}}
                                                      if t == "select" else {})}
                for n, t in settings.REQUIRED_PROPERTIES.items()}})
        m = re.fullmatch(r"/pages/([^/?]+)/properties/([^/?]+)(\?start_cursor=(.+))?", path)
        if method == "GET" and m:
            page = self.pages[m.group(1)]
            name = next(n for n, pid in PROP_IDS.items() if pid == m.group(2))
            text = "".join(x["plain_text"] for x in page["properties"][name]["rich_text"])
            half = len(text) // 2  # 2페이지로 나눠 페이지네이션 검증
            if not m.group(4):
                return _resp(200, {"results": [{"type": "rich_text", "rich_text": {"plain_text": text[:half]}}],
                                   "has_more": True, "next_cursor": "c2"})
            return _resp(200, {"results": [{"type": "rich_text", "rich_text": {"plain_text": text[half:]}}],
                               "has_more": False, "next_cursor": None})
        m = re.fullmatch(r"/pages/([^/]+)", path)
        if m and method == "GET":
            return _resp(200, copy.deepcopy(self.pages[m.group(1)]))
        if m and method == "PATCH":
            page = self.pages[m.group(1)]
            for name, val in json["properties"].items():
                prop = page["properties"][name]
                if "select" in val:
                    prop["select"] = val["select"]
                elif "rich_text" in val:
                    prop["rich_text"] = [{"type": "text", "plain_text": x["text"]["content"], "text": x["text"]}
                                         for x in val["rich_text"]]
                elif "date" in val:
                    prop["date"] = val["date"]
            if self.on_patch:
                self.on_patch(page)
            return _resp(200, copy.deepcopy(page))
        raise AssertionError(f"unrouted notion {method} {path}")

    # ── Graph / 파일 / 알림 ──
    def _next_override(self, step):
        queue = self.graph_post_override.get(step)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    def post(self, url, data=None, files=None, headers=None, json=None, timeout=None):  # noqa: A002
        if url.startswith("https://graph.facebook.com/v25.0/"):
            path = url.replace("https://graph.facebook.com/v25.0", "")
            assert data["access_token"] == TOKEN
            payload = {k: v for k, v in data.items()}
            if path == f"/{PAGE}/photos":
                self.log("POST", path, {**payload, "source": "<binary>"})
                return self._next_override("photos") or _resp(200, {"id": "ph1", "post_id": f"{PAGE}_ph1"})
            if path == f"/{PAGE}/video_reels" and data["upload_phase"] == "start":
                self.log("POST", path, payload)
                self.video_seq += 1
                vid = f"vid{self.video_seq}"
                return self._next_override("start") or _resp(200, {
                    "video_id": vid, "upload_url": f"https://rupload.facebook.com/video-upload/v25.0/{vid}"})
            if path == f"/{PAGE}/video_reels" and data["upload_phase"] == "finish":
                self.log("POST", path, payload)
                return self._next_override("finish") or _resp(200, {"success": True})
        if url.startswith("https://rupload.facebook.com/"):
            assert headers["Authorization"] == f"OAuth {TOKEN}" and headers["offset"] == "0"
            self.log("POST", url, {"headers": headers, "body": "<binary>"})
            return self._next_override("upload") or _resp(200, {"success": True})
        if url.startswith("https://api.telegram.org/"):
            self.log("POST", "telegram", json)
            return _resp(200, {"ok": True})
        raise AssertionError(f"unrouted POST {url}")

    def get(self, url, params=None, stream=False, timeout=None):
        if url in self.files:
            self.log("GET", url)
            return _resp(200, None, content=self.files[url])
        if url.startswith("https://graph.facebook.com/v25.0/"):
            self.log("GET", url.replace("https://graph.facebook.com/v25.0", ""), {"fields": params.get("fields")})
            body = self.reel_status.pop(0) if len(self.reel_status) > 1 else self.reel_status[0]
            return _resp(200, body)
        raise AssertionError(f"unrouted GET {url}")


def kst(dt: str) -> datetime:
    return datetime.fromisoformat(dt)
