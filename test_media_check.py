from unittest.mock import MagicMock, patch

import pytest

from fbpub import media_check as mc
from fbpub import settings


def test_image_ok(media_dir):
    assert mc.check_image(str(media_dir / "ok.png"))["format"] == "PNG"
    assert mc.check_image(str(media_dir / "ok.jpg"))["format"] == "JPEG"


def test_image_aspect_and_format(media_dir):
    with pytest.raises(mc.MediaError, match="image:aspect"):
        mc.check_image(str(media_dir / "square.png"))
    with pytest.raises(mc.MediaError, match="image:format:GIF"):
        mc.check_image(str(media_dir / "bad.gif"))


def test_image_too_large(media_dir, monkeypatch):
    monkeypatch.setattr(settings, "PHOTO_MAX_BYTES", 10)
    with pytest.raises(mc.MediaError, match="image:too_large"):
        mc.check_image(str(media_dir / "ok.png"))


def test_image_unreadable(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"not an image")
    with pytest.raises(mc.MediaError, match="image:unreadable"):
        mc.check_image(str(p))


def test_video_ok(media_dir):
    info = mc.check_video(str(media_dir / "ok.mp4"))
    assert (info["width"], info["height"]) == (540, 960)
    assert 29 <= info["fps"] <= 31
    assert 3.5 <= info["duration"] <= 4.5


@pytest.mark.parametrize(
    "name,expected",
    [
        ("landscape.mp4", "video:aspect"),
        ("short.mp4", "video:duration"),
        ("lowfps.mp4", "video:fps"),
        ("small.mp4", "video:resolution"),
    ],
)
def test_video_rejects(media_dir, name, expected):
    with pytest.raises(mc.MediaError, match=expected):
        mc.check_video(str(media_dir / name))


def test_video_unreadable(tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"garbage")
    with pytest.raises(mc.MediaError, match="video:unreadable"):
        mc.check_video(str(p))


def _stream_resp(status=200, chunks=(b"abc",)):
    r = MagicMock()
    r.status_code = status
    r.iter_content.return_value = list(chunks)
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


def test_download_ok(tmp_path):
    with patch.object(mc.requests, "get", return_value=_stream_resp()):
        path = mc.download("https://example.invalid/f", str(tmp_path / "f"))
    assert (tmp_path / "f").read_bytes() == b"abc"
    assert path.endswith("f")


def test_download_errors(tmp_path):
    with pytest.raises(mc.MediaError, match="empty_url"):
        mc.download("", str(tmp_path / "f"))
    with patch.object(mc.requests, "get", return_value=_stream_resp(status=403)):
        with pytest.raises(mc.MediaError, match="http_403"):
            mc.download("https://example.invalid/f", str(tmp_path / "f"))
    with patch.object(mc.requests, "get", return_value=_stream_resp(chunks=())):
        with pytest.raises(mc.MediaError, match="empty_file"):
            mc.download("https://example.invalid/f", str(tmp_path / "f"))
    with patch.object(mc.requests, "get", side_effect=mc.requests.exceptions.ReadTimeout("t")):
        with pytest.raises(mc.MediaError, match="request_error:ReadTimeout"):
            mc.download("https://example.invalid/f", str(tmp_path / "f"))
