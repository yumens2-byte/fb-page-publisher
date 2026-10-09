"""
fbpub/media_check.py
====================
첨부 파일 다운로드 + 게시 전 규격 검사.

  - 이미지 : JPEG/PNG, 10MB 이하 (Page Photos 레퍼런스), 4:5 (운영 규칙)
  - 영상   : mp4, 9:16, 최소 540x960, 3~90초, 24~60fps (Reels publishing 가이드) — ffprobe 사용
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from fractions import Fraction

import requests
from PIL import Image, UnidentifiedImageError

from fbpub import settings
from fbpub.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
_DOWNLOAD_CHUNK = 1024 * 1024
DOWNLOAD_MAX_BYTES = 500 * 1024 * 1024  # 비정상 파일 방어용 상한


class MediaError(ValueError):
    """규격 불일치·다운로드 실패 (게시 시도 전 확정 실패)."""


def download(url: str, dest_path: str) -> str:
    if not url:
        raise MediaError("download:empty_url")
    try:
        with requests.get(url, stream=True, timeout=settings.UPLOAD_TIMEOUT_SEC) as resp:
            if resp.status_code != 200:
                raise MediaError(f"download:http_{resp.status_code}")
            total = 0
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(_DOWNLOAD_CHUNK):
                    total += len(chunk)
                    if total > DOWNLOAD_MAX_BYTES:
                        raise MediaError("download:too_large")
                    fh.write(chunk)
    except requests.exceptions.RequestException as e:
        raise MediaError(f"download:request_error:{type(e).__name__}:{redact(e)[:80]}") from None
    if total == 0:
        raise MediaError("download:empty_file")
    return dest_path


def _ratio_ok(w: int, h: int, target: tuple[int, int], tol: float) -> bool:
    if w <= 0 or h <= 0:
        return False
    return abs((w / h) - (target[0] / target[1])) <= tol


def check_image(path: str) -> dict:
    size = os.path.getsize(path)
    if size > settings.PHOTO_MAX_BYTES:
        raise MediaError(f"image:too_large:{size}")
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            w, h = im.size
    except (UnidentifiedImageError, OSError):
        raise MediaError("image:unreadable") from None
    if fmt not in settings.PHOTO_ALLOWED_FORMATS:
        raise MediaError(f"image:format:{fmt}")
    if not _ratio_ok(w, h, settings.PHOTO_ASPECT, settings.PHOTO_ASPECT_TOLERANCE):
        raise MediaError(f"image:aspect:{w}x{h}")
    return {"format": fmt, "width": w, "height": h, "bytes": size}


def probe_video(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate:stream_tags=rotate:format=duration,format_name",
        "-of", "json", path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError:
        raise MediaError("video:ffprobe_missing") from None
    except subprocess.TimeoutExpired:
        raise MediaError("video:ffprobe_timeout") from None
    if out.returncode != 0:
        raise MediaError("video:unreadable")
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        raise MediaError("video:probe_invalid") from None
    streams = data.get("streams") or []
    if not streams:
        raise MediaError("video:no_video_stream")
    st = streams[0]
    fmt = data.get("format") or {}

    def fps_of(value: str | None) -> float:
        try:
            f = Fraction(value or "0/1")
            return float(f) if f.denominator else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    fps = fps_of(st.get("avg_frame_rate")) or fps_of(st.get("r_frame_rate"))
    width, height = int(st.get("width") or 0), int(st.get("height") or 0)
    rotate = str((st.get("tags") or {}).get("rotate", "0"))
    if rotate in ("90", "-90", "270", "-270"):
        width, height = height, width
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration": float(fmt.get("duration") or 0.0),
        "format_name": str(fmt.get("format_name", "")),
    }


def check_video(path: str) -> dict:
    info = probe_video(path)
    w, h = info["width"], info["height"]
    if "mp4" not in info["format_name"]:
        raise MediaError(f"video:container:{info['format_name']}")
    if not _ratio_ok(w, h, settings.REEL_ASPECT, settings.REEL_ASPECT_TOLERANCE):
        raise MediaError(f"video:aspect:{w}x{h}")
    if w < settings.REEL_MIN_WIDTH or h < settings.REEL_MIN_HEIGHT:
        raise MediaError(f"video:resolution:{w}x{h}")
    if not (settings.REEL_MIN_SEC <= info["duration"] <= settings.REEL_MAX_SEC):
        raise MediaError(f"video:duration:{info['duration']:.1f}")
    if not (settings.REEL_MIN_FPS <= info["fps"] <= settings.REEL_MAX_FPS):
        raise MediaError(f"video:fps:{info['fps']:.2f}")
    return info
