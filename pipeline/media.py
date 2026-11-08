"""FFmpeg location, probing (without ffprobe), and small media helpers."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pipeline.config import Config

log = logging.getLogger(__name__)


class MediaError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    """$FFMPEG_BIN, then ffmpeg on PATH (apt on CI), then the binary bundled with imageio-ffmpeg."""
    explicit = os.environ.get("FFMPEG_BIN", "").strip()
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise MediaError("FFmpeg not found. Install it (apt install ffmpeg) or pip install imageio-ffmpeg") from e


def run_ffmpeg(args: list[str], *, cwd: Path | None = None, desc: str = "ffmpeg") -> str:
    cmd = [ffmpeg_bin(), "-hide_banner", "-y", "-loglevel", "error", *args]
    log.debug("%s: %s", desc, " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise MediaError(f"{desc} failed ({r.returncode}): {r.stderr.strip()[-1500:]}")
    return r.stderr


@dataclass
class ProbeInfo:
    duration: float
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool
    has_video: bool


def probe(path: Path) -> ProbeInfo:
    """Parse `ffmpeg -i` output (the bundled build has no ffprobe)."""
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = r.stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        raise MediaError(f"Could not probe {path}: {out[-500:]}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    v = re.search(r"Stream #.*Video:.*?(\d{2,5})x(\d{2,5})", out)
    fps = re.search(r"(\d+(?:\.\d+)?) fps", out)
    return ProbeInfo(
        duration=duration,
        width=int(v.group(1)) if v else None,
        height=int(v.group(2)) if v else None,
        fps=float(fps.group(1)) if fps else None,
        has_audio=bool(re.search(r"Stream #.*Audio:", out)),
        has_video=bool(v),
    )


def silences(path: Path, min_gap_s: float = 1.5, noise_db: int = -40) -> list[tuple[float, float]]:
    stderr = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_gap_s}",
         "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?\d+(?:\.\d+)?)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?\d+(?:\.\d+)?)", stderr)]
    return list(zip(starts, ends + [float("inf")] * (len(starts) - len(ends))))


def black_segments(path: Path, min_s: float = 0.8) -> list[tuple[float, float]]:
    stderr = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path), "-vf", f"blackdetect=d={min_s}:pix_th=0.08",
         "-an", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    return [(float(a), float(b)) for a, b in
            re.findall(r"black_start:(\d+(?:\.\d+)?) black_end:(\d+(?:\.\d+)?)", stderr)]


_FALLBACK_FONTS = [
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def font_path(config: Config) -> Path:
    configured = config.root / config.get("video.font", "assets/fonts/Inter-Bold.ttf")
    if configured.exists():
        return configured
    for f in _FALLBACK_FONTS:
        if Path(f).exists():
            log.warning("Font %s missing; using %s", configured, f)
            return Path(f)
    raise MediaError(f"No usable font: add {configured}")


def font_family(path: Path) -> str:
    from PIL import ImageFont

    return ImageFont.truetype(str(path), 40).getname()[0]
