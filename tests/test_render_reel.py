"""Template reels: 15–40 s, sky canvas, H.264 + a silent AAC track (Instagram requires audio)."""

from __future__ import annotations

import re
import subprocess
from io import BytesIO
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image

from samawi_bot.render import reel
from samawi_bot.render.reel import FADE, MAX_SECONDS, MIN_SECONDS, REEL, encode, frame, render_reel, sky_canvas, slide_seconds


def _png(size: tuple[int, int], colour: tuple[int, int, int]) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def _total(n: int) -> float:
    per = slide_seconds(n)
    return n * per - (n - 1) * FADE


@pytest.mark.parametrize("n", range(1, 13))
def test_slide_seconds_keeps_reel_within_15_to_40(n: int) -> None:
    assert MIN_SECONDS - 0.01 <= _total(n) <= MAX_SECONDS + 0.01


def test_slide_seconds_branches() -> None:
    assert slide_seconds(1) == 15.0  # stretched up to the minimum
    assert slide_seconds(5) == 4.0  # 5·4 − 4·0.5 = 18 s: untouched
    assert slide_seconds(12) == pytest.approx((MAX_SECONDS + 11 * FADE) / 12, abs=1e-3)  # squeezed to 40 s
    assert slide_seconds(12) < 4.0


def test_sky_canvas_two_stop_endpoints() -> None:
    img = sky_canvas(["#0E1B44", "#24407F"])
    assert img.size == REEL
    assert img.getpixel((0, 0)) == (0x0E, 0x1B, 0x44)
    assert img.getpixel((REEL[0] - 1, REEL[1] - 1)) == (0x24, 0x40, 0x7F)
    # Monotone in between.
    mid = img.getpixel((500, REEL[1] // 2))
    assert 0x0E < mid[0] < 0x24 and 0x40 > mid[1] > 0x1B


def test_sky_canvas_three_stop_passes_through_mid() -> None:
    top, mid, bottom = "#E79A44", "#D06A78", "#4E356E"
    img = sky_canvas([top, mid, bottom])
    assert img.getpixel((0, 0)) == (0xE7, 0x9A, 0x44)
    assert img.getpixel((0, REEL[1] - 1)) == (0x4E, 0x35, 0x6E)
    centre = img.getpixel((0, (REEL[1] - 1) // 2))
    assert all(abs(a - b) <= 1 for a, b in zip(centre, (0xD0, 0x6A, 0x78), strict=True))


def test_frame_centres_slide_on_sky() -> None:
    out = Image.open(BytesIO(frame(_png((1080, 1350), (255, 0, 0)), ["#000000", "#000000"])))
    assert out.size == REEL
    assert out.getpixel((540, 960)) == (255, 0, 0)  # the slide
    assert out.getpixel((10, 10)) == (0, 0, 0)  # the sky around it
    assert out.getpixel((40, 335)) == (255, 0, 0) and out.getpixel((39, 335)) == (0, 0, 0)


def _probe(mp4: bytes, tmp_path: Path) -> str:
    p = tmp_path / "reel.mp4"
    p.write_bytes(mp4)
    proc = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(p)], capture_output=True, text=True)
    return proc.stderr  # ffmpeg -i with no output exits 1 and prints the stream info


def test_encode_produces_h264_1080x1920_with_aac(tmp_path: Path) -> None:
    frames = [frame(_png((108, 135), (200, 30, 30)), ["#0E1B44", "#24407F"]), frame(_png((108, 135), (30, 200, 30)), ["#0E1B44", "#24407F"])]
    mp4 = encode(frames, 1.0)
    info = _probe(mp4, tmp_path)
    video = next(line for line in info.splitlines() if "Video:" in line)
    audio = next(line for line in info.splitlines() if "Audio:" in line)
    assert "h264" in video and "1080x1920" in video and "yuv420p" in video
    assert "aac" in audio
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    assert m is not None
    seconds = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    assert seconds == pytest.approx(2 * 1.0 - FADE, abs=0.2)


def test_encode_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reel.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "false")
    with pytest.raises(subprocess.CalledProcessError):
        encode([_png((10, 10), (0, 0, 0))], 1.0)


def test_render_reel_frames_each_slide_and_times_them(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_encode(frames: list[bytes], seconds: float) -> bytes:
        seen["frames"], seen["seconds"] = frames, seconds
        return b"mp4"

    monkeypatch.setattr(reel, "encode", fake_encode)
    slides = [_png((1080, 1350), (i * 40, 0, 0)) for i in range(6)]
    assert render_reel(slides, ["#2F72C4", "#6EAAE6"]) == b"mp4"
    frames = seen["frames"]
    assert isinstance(frames, list) and len(frames) == 6
    assert Image.open(BytesIO(frames[0])).size == REEL
    assert seen["seconds"] == slide_seconds(6)
