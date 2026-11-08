"""Stage: qa — automated checks on final.mp4. Any failure stops the run (resumable after a fix).

Checks: duration within video.duration_s, exact resolution and fps, audio present,
no silent gap > 1.5 s, file size sane, captions file non-empty, every stock shot attributed.
Output: qa.json
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.media import black_segments, probe, silences
from pipeline.stages import AbortRun, StageContext
from pipeline.state import atomic_write_json, read_json

log = logging.getLogger(__name__)


class QAFailed(AbortRun):
    """A rejected render aborts the run instead of failing it: resuming would only re-check
    the same file, and a failed run would block the day's later runs from starting fresh."""


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    final = ctx.path("final.mp4")
    info = probe(final)
    min_s, max_s = cfg.get("video.duration_s", [45, 60])
    w, h = cfg.get("video.resolution", [1080, 1920])
    fps = int(cfg.get("video.fps", 30))
    size_mb = final.stat().st_size / 1e6
    gaps = [(round(s, 2), round(e, 2)) for s, e in silences(final, 1.5)]
    shots = read_json(ctx.path("footage.json"))["shots"]

    checks: list[dict[str, Any]] = [
        {"name": "duration", "ok": min_s - 1 <= info.duration <= max_s + 0.5, "value": round(info.duration, 2),
         "expected": f"{min_s}–{max_s}s"},
        {"name": "resolution", "ok": (info.width, info.height) == (w, h), "value": f"{info.width}x{info.height}"},
        {"name": "fps", "ok": info.fps is not None and abs(info.fps - fps) < 0.5, "value": info.fps},
        {"name": "audio_present", "ok": info.has_audio},
        {"name": "no_silent_gaps", "ok": not gaps, "value": gaps},
        {"name": "file_size", "ok": 1 <= size_mb <= 200, "value": f"{size_mb:.1f} MB"},
        {"name": "no_black_frames", "ok": not (black := black_segments(final)), "value": black},
        {"name": "captions", "ok": ctx.path("captions.ass").stat().st_size > 500},
        {"name": "attribution", "ok": all(s.get("attribution") for s in shots if s["kind"] == "stock")},
    ]
    passed = all(c["ok"] for c in checks)
    out = ctx.path("qa.json")
    atomic_write_json(out, {"passed": passed, "checks": checks,
                            "text_card_shots": sum(1 for s in shots if s["kind"] == "card")})
    for c in checks:
        log.info("qa: %-15s %s %s", c["name"], "PASS" if c["ok"] else "FAIL", c.get("value", ""))
    if not passed:
        raise QAFailed("QA failed: " + ", ".join(c["name"] for c in checks if not c["ok"]))
    return [out]
