"""v1.1.0 검토 반영 항목 회귀 테스트 (H1, M1~M5, L3)."""
import pytest
import requests
from conftest import FEED_OK, REEL_OK, TOKEN
from fake_backend import PROP_IDS, _resp, make_row
from test_run_publish import NOW, _run, _setup, _summary

from fbpub import graph_client, notion_repo, run_publish, settings


def _row(**kw):
    base = dict(page_id="page-1", number=3, state=settings.S_APPROVED, feed_text=FEED_OK, reel_text=REEL_OK)
    base.update(kw)
    return make_row(**base)


@pytest.fixture
def files(media_dir):
    return {"https://files.invalid/feed": (media_dir / "ok.png").read_bytes(),
            "https://files.invalid/reel": (media_dir / "ok.mp4").read_bytes()}


# H1 — 속성 ID 를 응답 그대로 경로에 사용
def test_h1_property_id_used_as_is(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    assert _run(be, "R01_encoded_prop_id") == 0
    prop_calls = [t["url"] for t in be.trace if "/properties/" in t["url"]]
    assert prop_calls and all("%3Ap" in u and "%25" not in u for u in prop_calls)
    assert PROP_IDS[settings.P_FEED_TEXT].startswith("%3A")


# M1 — DRY_RUN 파싱
@pytest.mark.parametrize("raw,expected", [("", True), ("true", True), ("TRUE", True), (" false ", False),
                                          ("1", None), ("yes", None), ("ture", None), ("0", None)])
def test_m1_parse_dry_run(raw, expected):
    assert settings.parse_dry_run(raw) is expected


def test_m1_invalid_dry_run_stops(live, monkeypatch, files):
    monkeypatch.setattr(settings, "DRY_RUN_INVALID", True)
    be = _setup(monkeypatch, [_row()], files)
    assert _run(be, "R02_dry_run_invalid") == 1
    assert not [t for t in be.trace if t["method"] == "PATCH"]


# M2 — 중단 신호 시 확인필요 기록 후 재발생
def test_m2_interrupt_after_write_marks_check(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    orig = graph_client.post_reel

    def interrupted(*a, **kw):
        raise run_publish.Terminated("signal 15")

    monkeypatch.setattr(graph_client, "post_reel", interrupted)
    with pytest.raises(run_publish.Terminated):
        run_publish.run(now=lambda: NOW)
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_CHECK and "interrupted:Terminated" in s["결과코드"]
    assert s["FB피드ID"]
    monkeypatch.setattr(graph_client, "post_reel", orig)


def test_m2_interrupt_before_write_marks_failed(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)

    def interrupted(*a, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(run_publish.media_check, "check_video", interrupted)  # 사전 검사 중 중단
    with pytest.raises(KeyboardInterrupt):
        run_publish.run(now=lambda: NOW)
    assert _summary(be.pages["page-1"])["게시상태"] == settings.S_FAILED
    assert not [t for t in be.trace if t["url"].endswith("/photos")]


def test_m2_interrupt_during_photo_request_marks_check(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)

    def interrupted(*a, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(graph_client, "post_photo", interrupted)  # 요청 전송 여부 불명 → 보수적으로 확인필요
    with pytest.raises(KeyboardInterrupt):
        run_publish.run(now=lambda: NOW)
    assert _summary(be.pages["page-1"])["게시상태"] == settings.S_CHECK


def test_m2_stuck_rows_alerted(live, monkeypatch, files):
    stuck = make_row("page-9", 9, settings.S_RUNNING)
    be = _setup(monkeypatch, [stuck], files)
    sent = []
    monkeypatch.setattr(run_publish.notify, "send", lambda m: sent.append(m) or True)
    assert _run(be, "R03_stuck_alert") == 0
    assert any("발행중 잔류: #009" in m for m in sent)
    assert _summary(be.pages["page-9"])["게시상태"] == settings.S_RUNNING  # 자동 처리 안 함


def test_m2_poll_timeout_bounded_by_deadline(live, monkeypatch):
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append(timeout)
        return _resp(200, {"status": {"publishing_phase": {"status": "not_started"}}})

    monkeypatch.setattr(requests, "get", fake_get)
    t = {"v": 0.0}
    r = graph_client.wait_reel_published("v1", sleep=lambda s: t.__setitem__("v", t["v"] + s), clock=lambda: t["v"])
    assert r["status"] == "unknown"
    assert all(x <= settings.REEL_POLL_MAX_SEC for x in seen) and seen[-1] <= 1.0 + 1e-9
    assert t["v"] <= settings.REEL_POLL_MAX_SEC


# M3 — 페이지네이션
def test_m3_find_ready_paginates(live, monkeypatch, files):
    rows = [_row(page_id=f"fut-{i}", number=i, schedule="2026-12-31") for i in range(1, 26)]
    rows.append(_row(page_id="due", number=30))
    be = _setup(monkeypatch, rows, files)
    ep = notion_repo.find_ready(NOW)
    assert ep is not None and ep.page_id == "due"
    assert sum(1 for t in be.trace if t["url"].endswith("/query")) >= 2


def test_m3_daily_cap_counts_beyond_first_page(live, monkeypatch, files):
    old = [make_row(f"old-{i}", i, settings.S_DONE, reel_id=f"v{i}", feed_id="f",
                    published_at="2026-10-08T20:00:00+09:00") for i in range(1, 120)]
    today = make_row("today", 500, settings.S_DONE, reel_id="vT", feed_id="f", published_at="2026-10-09T09:00:00+09:00")
    _setup(monkeypatch, old + [today], files)
    assert notion_repo.count_reels_published_on("2026-10-09") == 1


# M4 — 노션 429 재시도
def test_m4_notion_429_retried(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    be.notion_errors = [429, 429]
    assert _run(be, "R04_notion_429") == 0
    assert _summary(be.pages["page-1"])["게시상태"] == settings.S_DONE


def test_m4_notion_retry_exhausted(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    be.notion_errors = [503] * 10
    assert _run(be, "R05_notion_exhausted") == 2


# M5 — 게시 후 ID 기록 실패 → 확인필요 + ID 보존
def test_m5_photo_id_unsaved_keeps_id(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row()], files)
    be.patch_fail_props = {settings.P_FEED_ID}
    sent = []
    monkeypatch.setattr(run_publish.notify, "send", lambda m: sent.append(m) or True)
    assert _run(be, "R06_photo_id_unsaved") == 2
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_CHECK
    assert "photo_id_unsaved=1234567890123_ph1" in s["결과코드"]  # 원장에는 게시물 ID 를 온전히 남긴다
    assert any("photo_id_unsaved=" in m for m in sent)


def test_m5_reel_id_unsaved_keeps_id(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row(feed_id="EXISTING")], files)
    be.patch_fail_props = {settings.P_REEL_ID}
    assert _run(be, "R07_reel_id_unsaved") == 2
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_CHECK and "reel_id_unsaved=vid1" in s["결과코드"]


# L3 — unknown 도 발행일시 기록
def test_l3_unknown_sets_published_at(live, monkeypatch, files):
    be = _setup(monkeypatch, [_row(feed_id="EXISTING")], files)
    be.graph_post_override["finish"] = [requests.exceptions.ReadTimeout("t")]
    assert _run(be, "R08_unknown_published_at") == 2
    s = _summary(be.pages["page-1"])
    assert s["게시상태"] == settings.S_CHECK and s["발행일시"]


def test_token_not_in_logs_after_traceback(live, monkeypatch, files, caplog):
    be = _setup(monkeypatch, [_row()], files)

    def boom(*a, **kw):
        raise RuntimeError(f"bad access_token={TOKEN}")

    monkeypatch.setattr(graph_client, "post_photo", boom)
    assert _run(be, "R09_exception_redacted") == 2
    assert TOKEN not in caplog.text
