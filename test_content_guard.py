from conftest import FEED_OK, REEL_OK

from fbpub import content_guard as cg


def test_feed_ok():
    assert cg.check_feed_text(FEED_OK) == []


def test_feed_with_check_marks_ok():
    text = FEED_OK.replace("①우위 ? ②적정범위 ? ③고통 ?", "①우위 ✔ ②적정범위 ? ③고통 ✖")
    assert cg.check_feed_text(text) == []


def test_feed_check_line_without_variation_selector_ok():
    text = FEED_OK.replace("⚖️", "⚖")
    assert cg.check_feed_text(text) == []


def test_feed_empty():
    assert cg.check_feed_text("  ") == ["feed:empty"]


def test_feed_missing_notices_and_tags():
    text = FEED_OK.replace("※ 여러 사례를 재구성한 픽션입니다. 법률 자문이 아닙니다.", "").replace("#수사일지", "")
    errs = cg.check_feed_text(text)
    assert "feed:fiction_notice_missing" in errs
    assert "feed:hashtag_missing:#수사일지" in errs


def test_feed_comment_notice_missing():
    text = FEED_OK.replace("(실명·회사명은 적지 말아 주세요. 확인 시 숨김 처리됩니다.)", "")
    assert "feed:comment_notice_missing" in cg.check_feed_text(text)


def test_feed_invalid_check_mark():
    text = FEED_OK.replace("①우위 ?", "①우위 O")
    assert "feed:check_line_invalid" in cg.check_feed_text(text)


def test_feed_relative_day_missing():
    text = FEED_OK.replace("D+9", "어느 날")
    assert "feed:relative_day_missing" in cg.check_feed_text(text)


def test_feed_real_dates_blocked():
    for date in ("2026-10-09", "2026.10.9", "10월 9일", "2026년 10월 9일"):
        text = FEED_OK.replace("테스트용 합성 문장이다.", f"{date} 테스트.")
        assert "feed:real_date_found" in cg.check_feed_text(text), date


def test_feed_time_is_not_date():
    text = FEED_OK.replace("테스트용 합성 문장이다.", "08시 41분. 10시 31분에 끝났다.")
    assert cg.check_feed_text(text) == []


def test_feed_pushy_phrase_blocked():
    text = FEED_OK.replace("💬 테스트 질문입니다?", "💬 댓글 달아 주세요")
    assert "feed:pushy_phrase" in cg.check_feed_text(text)


def test_reel_ok_and_missing_notice():
    assert cg.check_reel_text(REEL_OK) == []
    assert "reel:fiction_notice_missing" in cg.check_reel_text("요약만")
    assert cg.check_reel_text("") == ["reel:empty"]
