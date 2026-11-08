"""Animated graphics for numbers, comparisons and short processes, rendered in code.

Stock footage can't show "60 kW vs 10 kW" or "2.2 GW"; a graphic shows exactly what the
narration says, and the number starts counting the moment it's spoken. Frames are drawn
with Pillow and piped straight into FFmpeg, so a 4-second graphic renders in a few seconds.

Everything sits between y 220 and 1010: the spoken-word captions start around y 1050 and
YouTube's buttons and title cover the bottom of the screen.

Specs (written by the script writer; script.validate checks them and their numbers first):
  {"type": "counter", "value": "2.2", "unit": "GW", "label": "one AI data center"}
  {"type": "compare", "items": [{"label": "Normal rack", "value": 10, "unit": "kW"},
                                {"label": "AI rack", "value": 60, "unit": "kW"}]}
  {"type": "percent", "value": 70, "label": "of Earth is ocean"}
  {"type": "steps", "items": ["Sunlight hits air", "Blue scatters most", "Sky looks blue"]}
"""
from __future__ import annotations

import functools
import math
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from pipeline.cards import PALETTES, H, W, _gradient
from pipeline.media import ffmpeg_bin

TYPES = ("counter", "compare", "percent", "steps")
ACCENT = (255, 212, 0)
TOP, BOTTOM = 220, 1010  # usable band, clear of captions (~1050+) and the YouTube UI
MARGIN = 90


# ---------------------------------------------------------------------------
# Spec helpers (also used by the script validator and the fact-checker)
# ---------------------------------------------------------------------------

def _num(value: Any) -> tuple[float, int]:
    """'2.2' -> (2.2, 1 decimal); '1,000' -> (1000.0, 0)."""
    s = str(value).replace(",", "").strip()
    v = float(s)
    decimals = len(s.split(".")[1]) if "." in s else 0
    return v, min(decimals, 2)


def _fmt(v: float, decimals: int) -> str:
    return f"{v:,.{decimals}f}"


def spec_text(spec: dict[str, Any]) -> str:
    """What the graphic says, as plain text (for the fact-checker, number checks and logs)."""
    t = spec.get("type")
    if t == "counter":
        return f"{spec.get('value')} {spec.get('unit', '')} {spec.get('label', '')}".strip()
    if t == "compare":
        return " vs ".join(f"{i.get('label', '')}: {i.get('value')} {i.get('unit', '')}".strip()
                           for i in spec.get("items") or [])
    if t == "percent":
        return f"{spec.get('value')}% {spec.get('label', '')}".strip()
    if t == "steps":
        return " -> ".join(str(s) for s in spec.get("items") or [])
    return str(spec)


def _words(s: Any) -> int:
    return len(str(s).split())


def validate_spec(spec: Any) -> list[str]:
    """Shape problems in a graphic spec (empty list = renderable)."""
    if not isinstance(spec, dict) or spec.get("type") not in TYPES:
        return [f"type must be one of {', '.join(TYPES)}"]
    t, issues = spec["type"], []

    def number(v: Any, what: str) -> float | None:
        try:
            return _num(v)[0]
        except (TypeError, ValueError):
            issues.append(f"{what} must be a number written in digits, got {v!r}")
            return None

    if t == "counter":
        number(spec.get("value"), "value")
        if _words(spec.get("label", "")) > 5:
            issues.append("label must be max 5 words")
        if len(str(spec.get("unit", ""))) > 8:
            issues.append("unit must be max 8 characters")
    elif t == "compare":
        items = spec.get("items")
        if not isinstance(items, list) or len(items) != 2:
            return ["compare needs exactly 2 items"]
        for i in items:
            if not isinstance(i, dict) or not str(i.get("label", "")).strip():
                issues.append("each compare item needs a label")
                continue
            v = number(i.get("value"), f"value of '{i.get('label')}'")
            if v is not None and v < 0:
                issues.append("compare values must be positive")
            if _words(i["label"]) > 4:
                issues.append("compare labels must be max 4 words")
            if len(str(i.get("unit", ""))) > 8:
                issues.append("unit must be max 8 characters")
        units = {str(i.get("unit", "")).strip().lower() for i in items if isinstance(i, dict)}
        if len(units) > 1:
            # the bars share one scale: AQI 999 vs 45 cigarettes would be a meaningless picture
            issues.append("compare items must be the same kind of quantity in the same unit; "
                          "for two different quantities use a counter instead")
    elif t == "percent":
        v = number(spec.get("value"), "value")
        if v is not None and not 0 <= v <= 100:
            issues.append("percent value must be 0-100")
        if _words(spec.get("label", "")) > 6:
            issues.append("label must be max 6 words")
    elif t == "steps":
        items = spec.get("items")
        if not isinstance(items, list) or not 2 <= len(items) <= 4:
            return ["steps needs 2-4 items"]
        if any(_words(s) > 5 for s in items):
            issues.append("each step must be max 5 words")
    return issues


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


@functools.lru_cache(maxsize=64)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _fit(draw: ImageDraw.ImageDraw, text: str, path: Path, max_w: float, size: int, min_size: int = 36):
    while size > min_size and draw.textlength(text, font=_font(str(path), size)) > max_w:
        size -= 4
    return _font(str(path), size)


def _alpha(rgb: tuple[int, int, int], a: float) -> tuple[int, int, int, int]:
    return (*rgb, int(255 * max(0.0, min(1.0, a))))


def _glow(d: ImageDraw.ImageDraw, t: float) -> None:
    """A soft accent glow drifting behind the graphic, so it never looks frozen."""
    x = W / 2 + math.sin(t * 0.7) * 160
    y = 600 + math.cos(t * 0.5) * 90
    for r, a in ((420, 10), (300, 14), (180, 18)):
        d.ellipse([x - r, y - r, x + r, y + r], fill=(*ACCENT, a))


def _label(d: ImageDraw.ImageDraw, text: str, path: Path, y: float, t: float, size: int = 76) -> None:
    a = ease(t / 0.35)
    text = text.upper()
    d.text((W / 2, y - (1 - a) * 30), text, font=_fit(d, text, path, W - 2 * MARGIN, size),
           fill=_alpha((255, 255, 255), a), anchor="mm")


def _counter(d, spec, t, at, path, seconds) -> None:
    _label(d, str(spec.get("label", "")), path, 330, t)
    v, dec = _num(spec["value"])
    p = ease((t - at) / 1.1)
    final, unit = _fmt(v, dec), str(spec.get("unit", ""))
    nf = _fit(d, final + (" " + unit if unit else ""), path, W - 2 * MARGIN, 300)
    uf = _font(str(path), max(40, int(nf.size * 0.42)))
    unit_w = d.textlength(" " + unit, font=uf) if unit else 0
    # right edge fixed to the final number, like an odometer, so counting doesn't wobble
    right = (W - d.textlength(final, font=nf) - unit_w) / 2 + d.textlength(final, font=nf)
    d.text((right, 640), _fmt(v * p, dec) if p < 1 else final, font=nf, fill=(255, 255, 255), anchor="rm")
    if unit:
        d.text((right, 640 + nf.size * 0.18), " " + unit, font=uf, fill=_alpha(ACCENT, ease((t - at) / 0.4)), anchor="lm")
    bar = (W - 2 * MARGIN) * p
    if bar > 2:
        d.rounded_rectangle([W / 2 - bar / 2, 860, W / 2 + bar / 2, 878], radius=9, fill=ACCENT)


def _compare(d, spec, t, at, path, seconds) -> None:
    items = spec["items"][:2]
    vals = [_num(i["value"]) for i in items]
    hi = max(v for v, _ in vals) or 1.0
    full_w = W - 2 * MARGIN
    for k, (item, (v, dec)) in enumerate(zip(items, vals)):
        y = 330 + k * 300
        a = ease((t - k * 0.25) / 0.35)
        label = str(item["label"]).upper()
        d.text((MARGIN, y - (1 - a) * 20), label, font=_fit(d, label, path, full_w, 64),
               fill=_alpha((255, 255, 255), a), anchor="lm")
        p = ease((t - at - k * 0.45) / 0.9)
        bw = full_w * (v / hi) * p
        biggest = v == hi
        if bw > 2:
            d.rounded_rectangle([MARGIN, y + 60, MARGIN + bw, y + 170], radius=22,
                                fill=ACCENT if biggest else (255, 255, 255, 140))
        txt = f"{_fmt(v * p if p < 1 else v, dec)} {item.get('unit', '')}".strip()
        vf = _font(str(path), 72)
        tw = d.textlength(txt, font=vf)
        if p > 0:
            if bw > tw + 50:  # inside the bar
                d.text((MARGIN + bw - 25, y + 115), txt, font=vf, fill=(20, 20, 20), anchor="rm")
            else:  # just after it
                d.text((MARGIN + bw + 25, y + 115), txt, font=vf, fill=(255, 255, 255), anchor="lm")
    lo = min(v for v, _ in vals)
    ratio = hi / lo if lo > 0 else 0
    # a computed ratio badge, only when it's a clean whole number (6x, not 5.87x)
    if ratio >= 1.5 and abs(ratio - round(ratio)) < 0.05 and t > at + 1.5:
        a = ease((t - at - 1.5) / 0.3)
        bf = _font(str(path), int(80 + 40 * a))
        d.text((W / 2, 930), f"{round(ratio)}×", font=bf, fill=_alpha(ACCENT, a), anchor="mm")


def _percent(d, spec, t, at, path, seconds) -> None:
    _label(d, str(spec.get("label", "")), path, 290, t, size=70)
    v, dec = _num(spec["value"])
    v = max(0.0, min(100.0, v))
    cx, cy, r, w = W / 2, 640, 270, 56
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 50), width=w)
    p = ease((t - at) / 1.2)
    if p > 0 and v > 0:
        d.arc([cx - r, cy - r, cx + r, cy + r], start=-90, end=-90 + 360 * (v / 100) * p, fill=ACCENT, width=w)
    shown = (_fmt(v * p, dec) if p < 1 else _fmt(v, dec)) + "%"
    d.text((cx, cy), shown, font=_font(str(path), 170), fill=(255, 255, 255), anchor="mm")


def _steps(d, spec, t, at, path, seconds) -> None:
    items = [str(s) for s in spec["items"]][:4]
    n = len(items)
    gap = (BOTTOM - TOP) / n
    span = max(0.6, seconds - at - 0.8)  # spread the reveals over the shot
    alphas = [ease((t - at - k * span / n) / 0.35) for k in range(n)]
    for k, text in enumerate(items):
        a = alphas[k]
        if a <= 0:
            continue
        y = TOP + k * gap + gap / 2
        cx = MARGIN + 55 - (1 - a) * 60
        if k < n - 1 and alphas[k + 1] > 0:  # the connector arrives with the step it leads to
            d.line([MARGIN + 55, y + 58, MARGIN + 55, y + gap - 58], fill=(255, 255, 255, int(90 * alphas[k + 1])), width=6)
        d.ellipse([cx - 52, y - 52, cx + 52, y + 52], fill=_alpha(ACCENT, a))
        d.text((cx, y), str(k + 1), font=_font(str(path), 64), fill=_alpha((20, 20, 20), a), anchor="mm")
        label = text.upper()
        d.text((cx + 90, y), label, font=_fit(d, label, path, W - MARGIN - (cx + 90), 66),
               fill=_alpha((255, 255, 255), a), anchor="lm")


DRAWERS = {"counter": _counter, "compare": _compare, "percent": _percent, "steps": _steps}


def frame(spec: dict[str, Any], t: float, font_path: Path, seconds: float, palette_idx: int = 0,
          start_at: float = 0.3, background: Image.Image | None = None) -> Image.Image:
    img = (background or _gradient((W, H), *PALETTES[palette_idx % len(PALETTES)])).copy()
    d = ImageDraw.Draw(img, "RGBA")
    _glow(d, t)
    DRAWERS[spec["type"]](d, spec, t, start_at, font_path, seconds)
    return img


def render(spec: dict[str, Any], out: Path, seconds: float, fps: int, font_path: Path,
           palette_idx: int = 0, start_at: float = 0.3) -> Path:
    """Render the graphic to an H.264 MP4 of exactly `seconds` at 1080x1920."""
    problems = validate_spec(spec)
    if problems:
        raise ValueError("bad graphic: " + "; ".join(problems))
    frames = max(1, round(seconds * fps))
    bg = _gradient((W, H), *PALETTES[palette_idx % len(PALETTES)])
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-frames:v", str(frames), "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for f in range(frames):
            proc.stdin.write(frame(spec, f / fps, font_path, seconds, start_at=start_at, background=bg).tobytes())
    finally:
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", errors="replace")
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"graphic render failed: {err[:300]}")
    return out
