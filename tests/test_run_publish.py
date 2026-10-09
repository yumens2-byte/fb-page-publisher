"""run_publish 통합 시나리오 — 가짜 노션·Graph 백엔드로 전 흐름을 실행한다."""
import json
import os
from datetime import datetime, timezone

import pytest
import requests
from conftest import FEED_OK, REEL_OK, make_image
from fake_backend import FakeBackend, _resp, make_row

from fbpub import run_publish, settings

NOW = datetime(2026, 10, 9, 11, 30, tzinfo=timezone.utc)  # KST 20:30
FEED_URL = "https://files.invalid/feed"
REEL_URL = "https://files.invalid/reel"


@pytest.fixture
def files(media_dir):
    return {FEED_URL: (media_dir / "ok.png").read_bytes(), REEL_URL: (media_dir / "ok.mp4").read_bytes()}


def _setup(monkeypatch, rows, files):
    be = FakeBackend(rows, files)
    monkeypatch.setattr(requests, "request", be.notion)
    monkeypatch.setattr(requests, "post", be.post)
    monkeypatch.setattr(requests, "get", be.get)
    return be


def _run(be, name):
    clock = {"t": 0.0}
    code = run_publish.run(now=lambda: NOW, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                           clock=lambda: clock["t"])
    out = os.environ.get("FBPUB_TRACE_DIR")
    if out:
        with open(os.path.join(out, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump({"exit_code": code, "trace": be.trace,
                       "final_rows": {pid: {k: v for k, v in _summary(p).items()} for pid, p in be.pages.items()}},
                      f, ensure_ascii=False, indent=2)
    return code


def _summary(page):
    props = page["properties"]

    def text(name):
        return "".join(x["plain_text"] for x in props[name]["rich_text"])

    return {
        "게시상태": props[settings.P_STATE]["select"]["name"],
        "FB피드ID": text(settings.P_FEED_ID),
        "FB릴스ID": text(settings.P_REEL_ID),
        "발행일시": (props[settings.P_PUBLISHED_AT].get("date") or {}).get("start"),
        "결과코드": text(settings.P_RESULT),
    }


def _row(**kw):
    base = dict(page_id="page-1", number=3, state=settings.S_APPROVED, feed_text=FEED_OK, reel_text=REEL_OK)
    base.update(kw)
    return make_row(**base)


def _calls(be, needle):
    return [t for t in be.trace if needle in t["url"]]


def test_happy_path(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    assert _run(be, "S01_happy") == 0
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_DONE
    assert s["FB피드ID"] == "1234567890123_ph1" and s["FB릴스ID"] == "vid1"
    assert s["발행일시"].startswith("2026-10-09T20:30")
    assert s["결과코드"] == "photo=ok; reel=ok"
    # 순서: 선점 → 사진 → 피드ID 기록 → start → video_id 선기록 → upload → finish → status → 최종
    order = [t["url"].split("/")[-1] if "graph" not in t["url"] else t["url"] for t in be.trace]
    i_photo = next(i for i, t in enumerate(be.trace) if t["url"].endswith("/photos"))
    i_start = next(i for i, t in enumerate(be.trace) if (t["payload"] or {}).get("upload_phase") == "start")
    i_finish = next(i for i, t in enumerate(be.trace) if (t["payload"] or {}).get("upload_phase") == "finish")
    i_vid_saved = next(i for i, t in enumerate(be.trace)
                       if t["method"] == "PATCH" and settings.P_REEL_ID in (t["payload"] or {}).get("properties", {}))
    assert i_photo < i_start < i_vid_saved < i_finish, order
    photo_payload = _calls(be, "/photos")[0]["payload"]
    assert photo_payload["caption"] == FEED_OK


def test_no_target_when_not_reviewed(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row(reviewed=False)], files)
    assert _run(be, "S02_not_reviewed") == 0
    assert not [t for t in be.trace if t["method"] == "PATCH"]


def test_future_schedule_skipped(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row(schedule="2026-10-09T21:00:00+09:00")], files)
    assert _run(be, "S03_future_schedule") == 0
    assert _summary(be.pages["page-1"])["게시상태"] == settings.S_APPROVED


def test_past_schedule_date_only_is_kst(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row(schedule="2026-10-09")], files)
    assert _run(be, "S03b_date_only") == 0
    assert _summary(be.pages["page-1"])["게시상태"] == settings.S_DONE


def test_dry_run_no_writes(dry, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    assert _run(be, "S04_dry_run") == 0
    assert not [t for t in be.trace if t["method"] == "PATCH"]
    assert not _calls(be, "graph") and not _calls(be, "/photos") and not _calls(be, "rupload")


def test_precheck_failure_blocks_all_posts(live, monkeypatch, media_dir, tmp_path):
    bad = {FEED_URL: make_image(tmp_path / "sq.png", size=(1000, 1000)).read_bytes(),
           REEL_URL: (media_dir / "ok.mp4").read_bytes()}
    be = _setup(monkeypatch, [_row(feed_text=FEED_OK.replace("D+9", "어느 날"))], bad)
    assert _run(be, "S05_precheck_fail") == 2
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_FAILED
    assert "feed:relative_day_missing" in s["결과코드"] and "image:aspect:1000x1000" in s["결과코드"]
    assert not _calls(be, "/photos") and not _calls(be, "video_reels")


def test_reel_finish_timeout_needs_check(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    be.graph_post_override["finish"] = [requests.exceptions.ReadTimeout("t")]
    assert _run(be, "S06_finish_unknown") == 2
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_CHECK
    assert s["FB피드ID"] and s["FB릴스ID"] == "vid1"  # unknown 은 video_id 유지


def test_photo_auth_failure_skips_reel(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    be.graph_post_override["photos"] = [_resp(400, {"error": {"code": 190, "message": "expired"}})]
    assert _run(be, "S07_photo_auth_fail") == 2
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_FAILED
    assert "photo=failed/photos:auth_error/code=190" in s["결과코드"] and "reel=skipped/photo_failed" in s["결과코드"]
    assert not _calls(be, "video_reels")


def test_reprocess_partial_posts_only_missing_channel(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row(feed_id="EXISTING_FEED")], files)
    assert _run(be, "S08_reprocess_reel_only") == 0
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_DONE and s["FB피드ID"] == "EXISTING_FEED"
    assert not _calls(be, "/photos")
    assert s["결과코드"] == "photo=already; reel=ok"


def test_daily_cap_defers_reel(live, monkeypatch, files):
    other = make_row("page-0", 2, settings.S_DONE, reel_id="vidX", feed_id="f", published_at="2026-10-09T09:00:00+09:00")
    be = _setup(monkeypatch, [other, _row()], files)
    assert _run(be, "S09_daily_cap") == 0
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_APPROVED  # 다음 실행에서 릴스만
    assert s["FB피드ID"] and not s["FB릴스ID"]
    assert "reel=deferred/daily_cap" in s["결과코드"]


def test_daily_cap_ignores_other_days(live, monkeypatch, files):
    other = make_row("page-0", 2, settings.S_DONE, reel_id="vidX", feed_id="f", published_at="2026-10-08T21:00:00+09:00")
    be = _setup(monkeypatch, [other, _row()], files)
    assert _run(be, "S09b_cap_other_day") == 0
    assert _summary(be.pages["page-1"])["게시상태"] == settings.S_DONE


def test_reel_upload_failure_partial_and_clears_video_id(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    be.graph_post_override["upload"] = [_resp(400, {"error": {"code": 6000, "message": "bad file"}})]
    assert _run(be, "S10_upload_fail") == 2
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_PARTIAL
    assert s["FB피드ID"] and s["FB릴스ID"] == ""


def test_claim_race_lost(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)

    def steal(page):  # 선점 직후 다른 실행이 상태를 바꾼 상황
        page["properties"][settings.P_STATE]["select"] = {"name": settings.S_REVIEW}
        be.on_patch = None

    be.on_patch = steal
    assert _run(be, "S11_claim_lost") == 0
    assert not _calls(be, "/photos")


def test_notion_down(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    be.notion_fail = True
    assert _run(be, "S12_notion_down") == 2


def test_config_missing(live, monkeypatch, files):
    monkeypatch.setattr(settings, "NOTION_TOKEN", "")
    be = _setup(monkeypatch, [_row()], files)
    assert _run(be, "S13_config_missing") == 1


def test_logs_have_no_body_or_ids(live, monkeypatch, files, caplog):
    be = _setup(monkeypatch, [_row()], files)
    caplog.set_level("DEBUG")
    _run(be, "S14_log_check")
    for secret in ("테스트용 합성 문장", "1234567890123", "vid1", "page-1", "EAAtest", "ntn_test"):
        assert secret not in caplog.text, secret


@pytest.mark.parametrize(
    "results,expected",
    [
        ({"photo": {"status": "ok"}, "reel": {"status": "ok"}}, settings.S_DONE),
        ({"photo": {"status": "already"}, "reel": {"status": "ok"}}, settings.S_DONE),
        ({"photo": {"status": "ok"}, "reel": {"status": "unknown"}}, settings.S_CHECK),
        ({"photo": {"status": "ok"}, "reel": {"status": "failed"}}, settings.S_PARTIAL),
        ({"photo": {"status": "ok"}, "reel": {"status": "deferred"}}, settings.S_APPROVED),
        ({"photo": {"status": "failed"}, "reel": {"status": "skipped"}}, settings.S_FAILED),
        ({"photo": {"status": "already"}, "reel": {"status": "failed"}}, settings.S_PARTIAL),
    ],
)
def test_decide_state(results, expected):
    assert run_publish.decide_state(results) == expected
