import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fbpub import settings  # noqa: E402

TOKEN = "EAAtesttoken1234567890abcdefXYZ"
PAGE = "1234567890123"
NOTION_TOKEN = "ntn_testnotiontoken1234567890abcd"
DB_ID = "db000000000000000000000000000001"

# 합성 본문 (실제 일지 본문 아님)
FEED_OK = (
    "📓 수사일지 #900 — 관찰\n"
    "[D+9 / 화요일 오후 / 일반 회의실]\n\n"
    "테스트용 합성 문장이다. 기록은 짧게 남긴다.\n\n"
    "⚖️ 체크: ①우위 ? ②적정범위 ? ③고통 ?\n\n"
    "다음 일지: #901 — 수요일 오전, 복도\n"
    "💬 테스트 질문입니다?\n"
    "(실명·회사명은 적지 말아 주세요. 확인 시 숨김 처리됩니다.)\n\n"
    "※ 여러 사례를 재구성한 픽션입니다. 법률 자문이 아닙니다.\n"
    "#사건파일76 #수사일지 #직장내괴롭힘"
)
REEL_OK = "테스트 요약 두 줄.\n💬 테스트 질문?\n※ 여러 사례를 재구성한 픽션입니다."


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    from fbpub import notion_repo
    monkeypatch.setattr(notion_repo, "_sleep", lambda s: None)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(settings, "DRY_RUN", False)
    monkeypatch.setattr(settings, "PAGE_ID", PAGE)
    monkeypatch.setattr(settings, "PAGE_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "NOTION_TOKEN", NOTION_TOKEN)
    monkeypatch.setattr(settings, "NOTION_DB_ID", DB_ID)
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(settings, "REEL_POLL_INTERVAL_SEC", 1)
    monkeypatch.setattr(settings, "REEL_POLL_MAX_SEC", 5)
    yield


@pytest.fixture
def dry(live, monkeypatch):
    monkeypatch.setattr(settings, "DRY_RUN", True)
    yield


def make_image(path: Path, size=(1080, 1350), fmt="PNG") -> Path:
    Image.new("RGB", size, (27, 34, 48)).save(path, fmt)
    return path


def make_video(path: Path, size="540x960", seconds=4, fps=30) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=0x1B2230:s={size}:d={seconds}:r={fps}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    make_image(d / "ok.png")
    make_image(d / "square.png", size=(1080, 1080))
    make_image(d / "ok.jpg", fmt="JPEG")
    Image.new("RGB", (1080, 1350)).save(d / "bad.gif", "GIF")
    make_video(d / "ok.mp4")
    make_video(d / "landscape.mp4", size="960x540")
    make_video(d / "short.mp4", seconds=2)
    make_video(d / "lowfps.mp4", fps=15)
    make_video(d / "small.mp4", size="360x640")
    return d
