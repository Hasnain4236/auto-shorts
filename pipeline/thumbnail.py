"""Thumbnail: a frame from the hook shot + the title in big outlined type."""
from __future__ import annotations

from pathlib import Path

from pipeline.cards import render_thumbnail
from pipeline.media import run_ffmpeg


def make_thumbnail(video: Path, title: str, out: Path, font: Path, at_s: float = 0.8) -> Path:
    frame = out.with_name("thumb_frame.jpg")
    run_ffmpeg(["-ss", f"{at_s:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(frame)], desc="thumb frame")
    return render_thumbnail(frame, title, out, font)
