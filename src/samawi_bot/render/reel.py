"""Template reels (spec §4, Phase 6): the carousel's slides in motion on the sky, 15–40 s, silent.

Rendered deterministically: each slide is placed on a 1080×1920 sky canvas, gently pushed in, and
cross-faded into the next with ffmpeg. No music (spec §6.5); the audio track is silence, which
Instagram requires to be present.
"""

from __future__ import annotations

import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw

REEL = (1080, 1920)
MIN_SECONDS, MAX_SECONDS = 15.0, 40.0
FADE = 0.5


def slide_seconds(n: int) -> float:
    """Per-slide duration keeping the reel within 15–40 s."""
    per = 4.0
    total = n * per - (n - 1) * FADE
    if total > MAX_SECONDS:
        per = (MAX_SECONDS + (n - 1) * FADE) / n
    elif total < MIN_SECONDS:
        per = (MIN_SECONDS + (n - 1) * FADE) / n
    return round(per, 3)


def sky_canvas(stops: list[str]) -> Image.Image:
    w, h = REEL
    img = Image.new("RGB", REEL)
    draw = ImageDraw.Draw(img)
    cols = [tuple(int(c.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)) for c in stops]
    for y in range(h):
        t = y / (h - 1) * (len(cols) - 1)
        i = min(int(t), len(cols) - 2)
        f = t - i
        c = tuple(int(cols[i][k] + (cols[i + 1][k] - cols[i][k]) * f) for k in range(3))
        draw.line([(0, y), (w, y)], fill=c)
    return img


def frame(slide_png: bytes, stops: list[str]) -> bytes:
    canvas = sky_canvas(stops)
    slide = Image.open(BytesIO(slide_png)).convert("RGB").resize((1000, 1250))
    canvas.paste(slide, ((REEL[0] - 1000) // 2, (REEL[1] - 1250) // 2))
    out = BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def encode(frames: list[bytes], seconds: float) -> bytes:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        inputs: list[str] = []
        for i, f in enumerate(frames):
            p = d / f"f{i:02d}.png"
            p.write_bytes(f)
            inputs += ["-loop", "1", "-t", f"{seconds}", "-i", str(p)]
        n = len(frames)
        total = n * seconds - (n - 1) * FADE
        filters = []
        for i in range(n):
            filters.append(f"[{i}:v]scale=1080:1920,zoompan=z='min(zoom+0.0006,1.06)':d={int(seconds * 30)}:s=1080x1920:fps=30,format=yuv420p,setsar=1[v{i}]")
        last = "v0"
        offset = 0.0
        for i in range(1, n):
            offset += seconds - FADE
            filters.append(f"[{last}][v{i}]xfade=transition=fade:duration={FADE}:offset={offset:.3f}[x{i}]")
            last = f"x{i}"
        out = d / "reel.mp4"
        cmd = [
            ffmpeg,
            "-y",
            *inputs,
            "-f",
            "lavfi",
            "-t",
            f"{total:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{last}]",
            "-map",
            f"{n}:a",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return out.read_bytes()


def render_reel(slides: list[bytes], stops: list[str]) -> bytes:
    """Encode already-rendered (and already-checked) slides as the reel."""
    frames = [frame(png, stops) for png in slides]
    return encode(frames, slide_seconds(len(frames)))
