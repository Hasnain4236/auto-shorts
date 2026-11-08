"""Black-frame detection on real (tiny, generated) clips. Needs the bundled FFmpeg."""
import subprocess

from pipeline.media import black_segments, ffmpeg_bin


def _clip(path, colour: str, seconds: float) -> None:
    subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"color=c={colour}:s=108x192:r=10:d={seconds}", "-pix_fmt", "yuv420p", str(path)], check=True)


def test_black_clip_is_caught_within_the_part_that_is_shown(tmp_path):
    black, bright = tmp_path / "black.mp4", tmp_path / "bright.mp4"
    _clip(black, "black", 3)
    _clip(bright, "orange", 3)
    assert black_segments(black, seconds=2.0)  # a dead screen where the shot would be
    assert black_segments(bright, seconds=2.0) == []
