from unittest.mock import MagicMock, patch

import pytest
import requests
from conftest import PAGE, TOKEN

from fbpub import graph_client as gc


def _resp(status, body=None, text=""):
    r = MagicMock()
    r.status_code = status
    if body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    r.text = text or str(body)
    return r


def _err(code):
    return {"error": {"code": code, "message": f"err {code} access_token={TOKEN}"}}


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG" + b"0" * 10)
    return str(p)


@pytest.fixture
def vid(tmp_path):
    p = tmp_path / "a.mp4"
    p.write_bytes(b"0" * 1000)
    return str(p)


# ── classify ──
@pytest.mark.parametrize(
    "code,http,expected",
    [
        (190, 400, "auth_error"), (None, 401, "auth_error"), (200, 403, "permission_error"),
        (10, 403, "permission_error"), (4, 400, "rate_limited"), (80001, 400, "rate_limited"),
        (613, 400, "rate_limited"), (368, 400, "policy_blocked"), (506, 400, "duplicate"),
        (6000, 400, "media_error"), (324, 400, "media_error"), (100, 400, "invalid_param"),
        (999, 400, "api_error"),
    ],
)
def test_classify(code, http, expected):
    assert gc.classify_error(code, http) == expected


# ── photo ──
def test_photo_dry_run(dry, img):
    with patch.object(gc.requests, "post") as post:
        r = gc.post_photo(img, "cap")
    assert r["status"] == "dry_run" and r["success"]
    post.assert_not_called()


def test_photo_not_configured(live, img, monkeypatch):
    monkeypatch.setattr(gc.settings, "PAGE_TOKEN", "")
    assert gc.post_photo(img, "cap")["reason"] == "not_configured"


def test_photo_empty_caption(live, img):
    assert gc.post_photo(img, " ")["reason"] == "empty_caption"


def test_photo_ok_request_shape(live, img):
    with patch.object(gc.requests, "post", return_value=_resp(200, {"id": "p1", "post_id": "PAGE_p1"})) as post:
        r = gc.post_photo(img, "캡션")
    assert r == {**r, "status": "ok", "object_id": "PAGE_p1", "success": True}
    url = post.call_args.args[0]
    kw = post.call_args.kwargs
    assert url == f"https://graph.facebook.com/v25.0/{PAGE}/photos"
    assert kw["data"] == {"caption": "캡션", "access_token": TOKEN}
    assert "source" in kw["files"]
    assert post.call_count == 1


@pytest.mark.parametrize(
    "side_effect,resp,status,reason",
    [
        (requests.exceptions.ConnectTimeout("c"), None, "failed", "photos:connect_timeout"),
        (requests.exceptions.ReadTimeout("r"), None, "unknown", "photos:request_error:ReadTimeout"),
        (requests.exceptions.ConnectionError("x"), None, "unknown", "photos:connection_error"),
        (None, _resp(500, {"error": {"code": 2}}), "unknown", "photos:http_500"),
        (None, _resp(200, None, "oops"), "unknown", "photos:invalid_json"),
        (None, _resp(200, {}), "unknown", "photos:no_post_id"),
        (None, _resp(400, _err(190)), "failed", "photos:auth_error"),
        (None, _resp(400, _err(368)), "failed", "photos:policy_blocked"),
        (None, _resp(400, _err(80001)), "failed", "photos:rate_limited"),
    ],
)
def test_photo_status_matrix_no_retry(live, img, side_effect, resp, status, reason):
    with patch.object(gc.requests, "post", side_effect=side_effect, return_value=resp) as post:
        r = gc.post_photo(img, "cap")
    assert r["status"] == status
    assert r["reason"] == reason
    assert post.call_count == 1  # 쓰기 재시도 금지


def test_photo_missing_file_is_failed(live, tmp_path):
    r = gc.post_photo(str(tmp_path / "none.png"), "cap")
    assert r["status"] == "failed" and r["reason"] == "photos:file_read_error"


# ── reel ──
START = {"video_id": "v123", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/v123"}
PUBLISHED = {"status": {"video_status": "ready", "uploading_phase": {"status": "complete"},
                        "processing_phase": {"status": "complete"}, "publishing_phase": {"status": "complete"}}}
PENDING = {"status": {"video_status": "processing", "uploading_phase": {"status": "complete"},
                      "processing_phase": {"status": "in_progress"}, "publishing_phase": {"status": "not_started"}}}
ERRORED = {"status": {"video_status": "error", "processing_phase": {"status": "error"}}}


def _clock():
    t = {"v": 0.0}

    def clock():
        return t["v"]

    def sleep(sec):
        t["v"] += sec

    return clock, sleep


def test_reel_happy_path_request_sequence(live, vid):
    clock, sleep = _clock()
    seen = []
    with patch.object(gc.requests, "post", side_effect=[
        _resp(200, START), _resp(200, {"success": True}), _resp(200, {"success": True}),
    ]) as post, patch.object(gc.requests, "get", side_effect=[_resp(200, PENDING), _resp(200, PUBLISHED)]) as get:
        r = gc.post_reel(vid, "설명", on_video_id=seen.append, sleep=sleep, clock=clock)
    assert r["status"] == "ok" and r["object_id"] == "v123"
    assert seen == ["v123"]
    start, upload, finish = post.call_args_list
    assert start.kwargs["data"] == {"upload_phase": "start", "access_token": TOKEN}
    assert upload.args[0] == START["upload_url"]
    assert upload.kwargs["headers"] == {"Authorization": f"OAuth {TOKEN}", "offset": "0", "file_size": "1000"}
    assert finish.kwargs["data"] == {
        "access_token": TOKEN, "video_id": "v123", "upload_phase": "finish",
        "video_state": "PUBLISHED", "description": "설명", "is_ai_generated": "true",
    }
    assert get.call_args.kwargs["params"] == {"fields": "status", "access_token": TOKEN}


def test_reel_ai_flag_off(live, vid, monkeypatch):
    monkeypatch.setattr(gc.settings, "REEL_AI_FLAG", False)
    clock, sleep = _clock()
    with patch.object(gc.requests, "post", side_effect=[
        _resp(200, START), _resp(200, {"success": True}), _resp(200, {"success": True}),
    ]) as post, patch.object(gc.requests, "get", return_value=_resp(200, PUBLISHED)):
        gc.post_reel(vid, "설명", sleep=sleep, clock=clock)
    assert "is_ai_generated" not in post.call_args_list[2].kwargs["data"]


def test_reel_start_timeout_is_failed_not_unknown(live, vid):
    with patch.object(gc.requests, "post", side_effect=requests.exceptions.ReadTimeout("t")) as post:
        r = gc.post_reel(vid, "d")
    assert r["status"] == "failed" and post.call_count == 1


def test_reel_upload_failure_is_failed_with_video_id(live, vid):
    with patch.object(gc.requests, "post", side_effect=[_resp(200, START), requests.exceptions.ReadTimeout("t")]) as post:
        r = gc.post_reel(vid, "d")
    assert r["status"] == "failed" and r["object_id"] == "v123" and r["reason"].startswith("upload:")
    assert post.call_count == 2  # finish 호출 안 함


def test_reel_finish_timeout_is_unknown(live, vid):
    with patch.object(gc.requests, "post", side_effect=[
        _resp(200, START), _resp(200, {"success": True}), requests.exceptions.ReadTimeout("t"),
    ]):
        r = gc.post_reel(vid, "d")
    assert r["status"] == "unknown" and r["object_id"] == "v123"


def test_reel_finish_policy_block_failed(live, vid):
    with patch.object(gc.requests, "post", side_effect=[
        _resp(200, START), _resp(200, {"success": True}), _resp(400, _err(368)),
    ]):
        r = gc.post_reel(vid, "d")
    assert r["status"] == "failed" and r["reason"] == "finish:policy_blocked"


def test_reel_processing_error_failed(live, vid):
    clock, sleep = _clock()
    with patch.object(gc.requests, "post", side_effect=[
        _resp(200, START), _resp(200, {"success": True}), _resp(200, {"success": True}),
    ]), patch.object(gc.requests, "get", return_value=_resp(200, ERRORED)):
        r = gc.post_reel(vid, "d", sleep=sleep, clock=clock)
    assert r["status"] == "failed" and r["reason"] == "status:error_phase"


def test_reel_poll_timeout_unknown(live, vid):
    clock, sleep = _clock()
    with patch.object(gc.requests, "post", side_effect=[
        _resp(200, START), _resp(200, {"success": True}), _resp(200, {"success": True}),
    ]), patch.object(gc.requests, "get", return_value=_resp(200, PENDING)) as get:
        r = gc.post_reel(vid, "d", sleep=sleep, clock=clock)
    assert r["status"] == "unknown" and r["reason"].startswith("status:timeout")
    assert get.call_count >= 2


def test_reel_callback_exception_does_not_stop(live, vid):
    clock, sleep = _clock()

    def boom(_):
        raise RuntimeError("notion down")

    with patch.object(gc.requests, "post", side_effect=[
        _resp(200, START), _resp(200, {"success": True}), _resp(200, {"success": True}),
    ]), patch.object(gc.requests, "get", return_value=_resp(200, PUBLISHED)):
        r = gc.post_reel(vid, "d", on_video_id=boom, sleep=sleep, clock=clock)
    assert r["status"] == "ok"


def test_reel_dry_run_no_requests(dry, vid):
    with patch.object(gc.requests, "post") as post:
        assert gc.post_reel(vid, "d")["status"] == "dry_run"
    post.assert_not_called()


def test_interpret_status():
    assert gc.interpret_reel_status(PUBLISHED["status"]) == "published"
    assert gc.interpret_reel_status(PENDING["status"]) == "pending"
    assert gc.interpret_reel_status(ERRORED["status"]) == "error"


def test_logs_do_not_leak_token(live, img, caplog):
    with patch.object(gc.requests, "post", return_value=_resp(400, _err(190))):
        gc.post_photo(img, "cap")
    assert TOKEN not in caplog.text and PAGE not in caplog.text
