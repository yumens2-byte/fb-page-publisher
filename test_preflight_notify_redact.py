from unittest.mock import MagicMock, patch

import requests
from conftest import NOTION_TOKEN, PAGE, TOKEN
from fake_backend import FakeBackend

from fbpub import notify, preflight, settings
from fbpub.redact import redact


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


def test_redact_masks_all(live, monkeypatch):
    s = redact(f"access_token={TOKEN}&x=1 page {PAGE} Bearer {NOTION_TOKEN} Authorization: OAuth abc123def")
    assert TOKEN not in s and PAGE not in s and NOTION_TOKEN not in s and "abc123def" not in s


def test_preflight_all_pass(live, monkeypatch):
    monkeypatch.setattr(settings, "APP_ID", "app1")
    monkeypatch.setattr(settings, "APP_SECRET", "secretvalue123")
    be = FakeBackend([], {})
    monkeypatch.setattr(requests, "request", be.notion)

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/debug_token"):
            assert params["access_token"] == "app1|secretvalue123"
            return _resp(200, {"data": {"is_valid": True, "expires_at": 0,
                                        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]}})
        return _resp(200, {"id": PAGE, "name": "p"})

    monkeypatch.setattr(requests, "get", fake_get)
    assert preflight.run() == 0


def test_preflight_failures(live, monkeypatch):
    monkeypatch.setattr(settings, "APP_ID", "app1")
    monkeypatch.setattr(settings, "APP_SECRET", "secretvalue123")
    be = FakeBackend([], {})
    monkeypatch.setattr(requests, "request", be.notion)

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/debug_token"):
            return _resp(200, {"data": {"is_valid": False, "scopes": ["pages_show_list"]}})
        return _resp(400, {"error": {"code": 190}})

    monkeypatch.setattr(requests, "get", fake_get)
    errs = preflight.check_page() + preflight.check_token()
    assert "P1:page_access_failed:http_400:code=190" in errs
    assert "P2:token_invalid" in errs
    assert "P2:scopes_missing:pages_read_engagement,pages_manage_posts" in errs
    assert preflight.run() == 2


def test_preflight_schema_mismatch(live, monkeypatch):
    be = FakeBackend([], {})
    orig = be.notion

    def notion(method, url, **kw):
        r = orig(method, url, **kw)
        body = r.json.return_value
        body["properties"].pop(settings.P_REEL_ID)
        body["properties"][settings.P_STATE]["select"]["options"] = [{"name": settings.S_APPROVED}]
        return r

    monkeypatch.setattr(requests, "request", notion)
    errs = preflight.check_notion()
    assert f"P3:property:{settings.P_REEL_ID}:missing!=rich_text" in errs
    assert any(e.startswith("P3:state_options_missing") for e in errs)


def test_notify_unconfigured_returns_false(live):
    assert notify.send("x") is False


def test_notify_sends_masked(live, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "bot123456:abc")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "999888777")
    with patch.object(notify.requests, "post", return_value=_resp(200, {})) as post:
        assert notify.send(f"page {PAGE} token {TOKEN}") is True
    sent = post.call_args.kwargs["json"]["text"]
    assert PAGE not in sent and TOKEN not in sent
