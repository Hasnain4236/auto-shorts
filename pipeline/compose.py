"""Stage: compose — assemble final.mp4 with FFmpeg.

1. Each shot is rendered to exactly its frame count (cumulative frame boundaries,
   so cuts never drift from the voice): stock clips are scaled/centre-cropped to
   1080x1920 (looped if short); text cards get a slow push-in.
2. Shots are joined (stream copy) into one silent video.
3. Captions are burned in; music from assets/music is looped under the voice and
   ducked with a sidechain compressor keyed on the voice.

Outputs: final.mp4 (H.264 veryfast CRF 23, AAC 128k), thumb.jpg, compose.json
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any

from pipeline.media import font_path, probe, run_ffmpeg
from pipeline.stages import StageContext
from pipeline.state import atomic_write_json, read_json
from pipeline.thumbnail import make_thumbnail

log = logging.getLogger(__name__)

MUSIC_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}
MUSIC_LUFS = -24


def music_attribution(music: Path | None) -> str | None:
    """Credit line from assets/music/tracks.json (CC-BY tracks need one in the description)."""
    if not music:
        return None
    manifest = music.parent / "tracks.json"
    if not manifest.exists():
        return None
    for t in read_json(manifest):
        if t.get("file") == music.name:
            return t.get("attribution")
    return None
SCALE_CROP = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"


def pick_music(music_dir: Path, run_id: str) -> Path | None:
    tracks = sorted(p for p in music_dir.glob("*") if p.suffix.lower() in MUSIC_EXT) if music_dir.exists() else []
    if not tracks:
        return None
    return tracks[int(hashlib.sha1(run_id.encode()).hexdigest(), 16) % len(tracks)]


def render_shot(shot: dict[str, Any], src: Path, frames: int, fps: int, out: Path) -> None:
    common = ["-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
              "-pix_fmt", "yuv420p", "-r", str(fps), str(out)]
    if shot["kind"] == "card":
        vf = f"{SCALE_CROP},zoompan=z='min(zoom+0.0009,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1080x1920:fps={fps},format=yuv420p"
        run_ffmpeg(["-loop", "1", "-i", str(src), "-vf", vf, *common], desc=f"card {shot['slot']}")
        return
    need = frames / fps
    clip_len = probe(src).duration
    skip = 0.4 if clip_len > need + 1.0 else 0.0  # skip fade-ins at the very start of stock clips
    vf = f"{SCALE_CROP},fps={fps},format=yuv420p"
    run_ffmpeg(["-stream_loop", "-1", "-ss", f"{skip}", "-i", str(src), "-vf", vf, *common], desc=f"shot {shot['slot']}")


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    fps = int(cfg.get("video.fps", 30))
    timing = read_json(ctx.path("timing.json"))
    shots = read_json(ctx.path("footage.json"))["shots"]
    script = read_json(ctx.path("script.json"))
    total = timing["duration"]
    work = ctx.path("compose_parts")
    work.mkdir(exist_ok=True)

    # 1. shots at exact frame boundaries
    list_lines = []
    for i, shot in enumerate(shots):
        f0 = round(shot["start"] * fps)
        f1 = round(total * fps) if i == len(shots) - 1 else round(shot["end"] * fps)
        out = work / f"shot_{i:02d}.mp4"
        render_shot(shot, ctx.path("clips") / shot["file"], max(1, f1 - f0), fps, out)
        list_lines.append(f"file '{out.name}'")
    (work / "shots.txt").write_text("\n".join(list_lines) + "\n", encoding="utf-8")

    # 2. join
    video = work / "video.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", "shots.txt", "-c", "copy", video.name], cwd=work, desc="concat")

    # 3. captions + audio mix. Relative paths with cwd=run_dir avoid Windows drive-letter
    #    escaping problems inside the subtitles filter.
    fonts_dir = ctx.path("fonts")
    fonts_dir.mkdir(exist_ok=True)
    font = font_path(cfg)
    shutil.copyfile(font, fonts_dir / font.name)

    music = pick_music(cfg.root / cfg.get("video.music_dir", "assets/music"), ctx.state.run_id)
    inputs = ["-i", f"compose_parts/{video.name}", "-i", "voice.mp3"]
    vf = "[0:v]subtitles=captions.ass:fontsdir=fonts[v]"
    fade_out = max(0.0, total - 0.6)
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music.resolve())]
        af = (
            "[1:a]aresample=44100,asplit=2[vo][key];"
            # level every track to the same loudness first, so any file dropped into assets/music
            # sits at the same place under the -16 LUFS voice; the voice then ducks it ~10 dB.
            f"[2:a]loudnorm=I={MUSIC_LUFS}:TP=-3:LRA=11,aresample=44100,"
            f"afade=t=in:d=0.8,afade=t=out:st={fade_out:.2f}:d=0.6[mus];"
            "[mus][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[duck];"
            "[vo][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89[a]"
        )
        log.info("compose: music %s", music.name)
    else:
        af = "[1:a]aresample=44100[a]"
        log.warning("compose: no tracks in %s, video will have voice only", cfg.get("video.music_dir"))

    final = ctx.path("final.mp4")
    run_ffmpeg([
        *inputs, "-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", final.name,
    ], cwd=ctx.run_dir, desc="final render")

    thumb = make_thumbnail(video, script["title"], ctx.path("thumb.jpg"), font)
    meta = ctx.path("compose.json")
    atomic_write_json(meta, {"music": music.name if music else None, "music_attribution": music_attribution(music),
                             "shots": len(shots), "duration": total})
    log.info("compose: final.mp4 %.1fs, %.1f MB", total, final.stat().st_size / 1e6)
    return [final, thumb, meta]
