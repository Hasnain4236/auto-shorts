"""Stage: captions — word-highlighted ASS subtitles + beat headlines.

Word timings come from edge-tts (timing.json). If the TTS fallback gave none,
faster-whisper (CPU, `base`) transcribes voice.mp3 for word timestamps.

Layout on the 1080x1920 frame (Shorts UI covers the bottom ~20% and a button
column on the right):
  * headline (beat on_screen_text): boxed, top third, y ≈ 400
  * captions: 1–3 words, centre, y ≈ 1150; the spoken word is highlighted and pops
Output: captions.ass
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from pipeline.media import font_family, font_path
from pipeline.stages import StageContext
from pipeline.state import read_json

log = logging.getLogger(__name__)

MAX_WORDS = 3
MAX_CHARS = 18
CAPTION_Y = 1150
HEADLINE_Y = 400
HIGHLIGHT = "&H0000D7FF&"   # gold, ASS is &HAABBGGRR
WHITE = "&H00FFFFFF&"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(word: str) -> str:
    return re.sub(r"[{}\\]", "", word).strip()


def whisper_words(audio: Path, language: str) -> list[dict[str, Any]]:
    from faster_whisper import WhisperModel

    log.info("captions: no TTS word timings; transcribing with faster-whisper")
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio), word_timestamps=True, language=language)
    return [{"text": w.word.strip(), "start": w.start, "end": w.end, "segment": "?"}
            for seg in segments for w in (seg.words or [])]


def group_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """≤3 words / ≤18 chars per line; break on pauses and segment changes."""
    lines: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for w in words:
        if cur:
            prev = cur[-1]
            chars = sum(len(x["text"]) + 1 for x in cur) + len(w["text"])
            if (len(cur) >= MAX_WORDS or chars > MAX_CHARS or w["start"] - prev["end"] > 0.35
                    or w.get("segment") != prev.get("segment") or re.search(r"[.!?,;:]$", prev["text"])):
                lines.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def build_ass(words: list[dict[str, Any]], headlines: list[tuple[float, float, str]], family: str) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{family},92,{WHITE},{WHITE},&H00000000&,&H64000000&,-1,0,0,0,100,100,0,0,1,7,3,5,90,90,0,1
Style: Head,{family},58,&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&HB4101010&,-1,0,0,0,100,100,1,0,3,18,0,5,150,150,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for start, end, text in headlines:
        if text.strip():
            events.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},Head,,0,0,0,,"
                          f"{{\\pos(540,{HEADLINE_Y})\\fad(120,80)}}{_clean(text).upper()}")
    lines = group_lines(words)
    for li, line in enumerate(lines):
        next_start = lines[li + 1][0]["start"] if li + 1 < len(lines) else line[-1]["end"] + 0.4
        line_end = min(line[-1]["end"] + 0.25, next_start)
        for k, w in enumerate(line):
            start = w["start"]
            end = line[k + 1]["start"] if k + 1 < len(line) else line_end
            parts = []
            for j, x in enumerate(line):
                t = _clean(x["text"]).upper()
                parts.append(f"{{\\c{HIGHLIGHT}\\fscx112\\fscy112}}{t}{{\\c{WHITE}\\fscx100\\fscy100}}" if j == k else t)
            pop = "\\t(0,80,\\fscx104\\fscy104)" if k == 0 else ""
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,"
                          f"{{\\pos(540,{CAPTION_Y}){pop}}}{' '.join(parts)}")
    return header + "\n".join(events) + "\n"


def headlines_from_shots(shots: list[dict[str, Any]]) -> list[tuple[float, float, str]]:
    """One headline per beat (split shots 'beat2a'/'beat2b' share one). No headline on the hook:
    the big spoken-word caption carries it."""
    out: list[tuple[float, float, str]] = []
    prev_base = None
    for shot in shots:
        base = re.sub(r"[ab]$", "", shot["slot"])
        # hook: the big spoken caption carries it; text cards already show the headline large
        if base == "hook" or shot.get("kind") == "card":
            prev_base = None
            continue
        if base == prev_base and out:
            out[-1] = (out[-1][0], shot["end"], out[-1][2])
        else:
            out.append((shot["start"], shot["end"], shot.get("on_screen_text", "")))
        prev_base = base
    return out


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    timing = read_json(ctx.path("timing.json"))
    footage = read_json(ctx.path("footage.json"))
    words = timing.get("words") or whisper_words(ctx.path("voice.mp3"), cfg.get("channel.language", "en"))
    if not words:
        raise RuntimeError("No word timings available for captions")

    family = font_family(font_path(cfg))
    out = ctx.path("captions.ass")
    out.write_text(build_ass(words, headlines_from_shots(footage["shots"]), family), encoding="utf-8")
    log.info("captions: %d words, %d lines, font %s", len(words), len(group_lines(words)), family)
    return [out]
