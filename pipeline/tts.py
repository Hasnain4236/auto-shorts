"""Stage: tts — voiceover, one synthesis per segment (hook, beats, cta).

Synthesising each segment separately gives exact segment timings, so video cuts
land precisely on beat boundaries. edge-tts also returns word timings, which the
captions stage uses directly (faster-whisper is only needed for the offline fallback).

If the total length falls outside video.duration_s, the speaking rate is adjusted
and the voice re-synthesised (max 2 retries).

Outputs: voice.mp3 (loudness-normalised to -16 LUFS), timing.json
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pipeline.media import MediaError, probe, run_ffmpeg
from pipeline.stages import StageContext
from pipeline.state import atomic_write_json, read_json

log = logging.getLogger(__name__)

GAP_AFTER_HOOK = 0.30
GAP_BETWEEN = 0.18


def segments_from_script(script: dict[str, Any]) -> list[dict[str, str]]:
    segs = [{"name": "hook", "text": script["hook"].strip()}]
    segs += [{"name": f"beat{i}", "text": b["narration"].strip()} for i, b in enumerate(script["beats"], start=1)]
    segs.append({"name": "cta", "text": script["cta"].strip().lstrip(".… ").strip()})
    return segs


def _rate_str(pct: int) -> str:
    return f"{'+' if pct >= 0 else ''}{pct}%"


async def _edge_segment(text: str, voice: str, rate_pct: int, out: Path) -> list[dict[str, Any]]:
    import edge_tts

    comm = edge_tts.Communicate(text, voice, rate=_rate_str(rate_pct), boundary="WordBoundary")
    words: list[dict[str, Any]] = []
    with out.open("wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append({"text": chunk["text"], "start": start, "end": start + chunk["duration"] / 1e7})
    if out.stat().st_size == 0:
        raise MediaError("edge-tts returned no audio")
    return words


def _piper_segment(text: str, out: Path) -> list[dict[str, Any]]:
    """Offline fallback: needs the `piper` binary and PIPER_MODEL (path to a .onnx voice)."""
    model = os.environ.get("PIPER_MODEL")
    if not (model and shutil.which("piper")):
        raise MediaError("Piper fallback unavailable (install piper and set PIPER_MODEL)")
    wav = out.with_suffix(".wav")
    subprocess.run(["piper", "--model", model, "--output_file", str(wav)], input=text, text=True, check=True,
                   capture_output=True)
    run_ffmpeg(["-i", str(wav), "-c:a", "libmp3lame", "-b:a", "128k", str(out)], desc="piper->mp3")
    return []  # no word timings; captions will run whisper


def _edge_with_retry(text: str, voice: str, rate_pct: int, out: Path, tries: int = 3) -> list[dict[str, Any]]:
    for attempt in range(1, tries + 1):
        try:
            return asyncio.run(_edge_segment(text, voice, rate_pct, out))
        except Exception as e:
            if attempt == tries:
                raise
            log.warning("edge-tts error (%s); retrying", e)
    raise AssertionError("unreachable")


def synthesise(segs: list[dict[str, str]], voice: str, rate_pct: int, work: Path) -> tuple[str, list[list[dict[str, Any]]]]:
    """Synthesise every segment to work/<name>.mp3; returns (engine, per-segment word timings).

    edge-tts is an unofficial client of Microsoft's free online voice; if it fails,
    every segment is redone with offline Piper so the voice stays consistent.
    """
    try:
        return "edge-tts", [_edge_with_retry(s["text"], voice, rate_pct, work / f"{s['name']}.mp3") for s in segs]
    except Exception as e:
        log.error("edge-tts failed (%s); falling back to offline Piper", e)
        return "piper", [_piper_segment(s["text"], work / f"{s['name']}.mp3") for s in segs]


def assemble(segs: list[dict[str, str]], seg_words: list[list[dict[str, Any]]], work: Path, out: Path) -> dict[str, Any]:
    """Concatenate segments with short gaps, normalise loudness, and compute absolute timings."""
    inputs: list[str] = []
    filters: list[str] = []
    timeline: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    t = 0.0
    for i, seg in enumerate(segs):
        path = work / f"{seg['name']}.mp3"
        dur = probe(path).duration
        gap = GAP_AFTER_HOOK if seg["name"] == "hook" else (0.0 if i == len(segs) - 1 else GAP_BETWEEN)
        inputs += ["-i", str(path)]
        filters.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=mono,apad=pad_dur={gap}[a{i}]")
        timeline.append({**seg, "start": round(t, 3), "end": round(t + dur, 3), "slot_end": round(t + dur + gap, 3)})
        if i < len(seg_words):
            words += [{**w, "start": round(w["start"] + t, 3), "end": round(w["end"] + t, 3), "segment": seg["name"]}
                      for w in seg_words[i]]
        t += dur + gap
    concat = "".join(f"[a{i}]" for i in range(len(segs)))
    filters.append(f"{concat}concat=n={len(segs)}:v=0:a=1,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100[out]")
    run_ffmpeg([*inputs, "-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "libmp3lame",
                "-b:a", "192k", str(out)], desc="voice assemble")
    return {"segments": timeline, "words": words, "duration": round(probe(out).duration, 3)}


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    script = read_json(ctx.path("script.json"))
    voice = cfg.get("video.voice", "en-IN-NeerjaNeural")
    rate = int(cfg.get("video.voice_rate_pct", 8))
    min_s, max_s = cfg.get("video.duration_s", [45, 60])
    target = (min_s + max_s) / 2
    segs = segments_from_script(script)
    work = ctx.path("tts_parts")
    work.mkdir(exist_ok=True)
    out = ctx.path("voice.mp3")

    for attempt in range(3):
        engine, seg_words = synthesise(segs, voice, rate, work)
        timing = assemble(segs, seg_words, work, out)
        dur = timing["duration"]
        log.info("tts: %.1fs at rate %s via %s", dur, _rate_str(rate), engine)
        if min_s <= dur <= max_s - 0.5 or engine != "edge-tts":
            break
        # speech time scales ~inversely with rate; aim for the middle of the window
        new_rate = round((1 + rate / 100) * dur / target * 100 - 100)
        rate = max(-10, min(35, new_rate))
        log.info("tts: outside %s–%ss, retrying at %s", min_s, max_s, _rate_str(rate))

    timing.update(engine=engine, voice=voice, rate=_rate_str(rate))
    tpath = ctx.path("timing.json")
    atomic_write_json(tpath, timing)
    return [out, tpath]
