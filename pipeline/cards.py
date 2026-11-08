"""Pillow-rendered images: fallback text cards and thumbnails."""
from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
PALETTES = [((14, 20, 48), (52, 18, 92)), ((6, 40, 52), (12, 92, 110)), ((40, 10, 24), (110, 30, 50))]


def _gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    w, h = size
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        col.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return col.resize((w, h))


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: Path, max_w: int, max_size: int,
              min_size: int = 60, max_lines: int = 4):
    for size in range(max_size, min_size - 1, -6):
        font = ImageFont.truetype(str(font_path), size)
        for width in (10, 14, 18, 24):
            lines = textwrap.wrap(text, width=width) or [""]
            if all(draw.textlength(line, font=font) <= max_w for line in lines) and len(lines) <= max_lines:
                return font, lines
    font = ImageFont.truetype(str(font_path), min_size)
    return font, textwrap.wrap(text, width=22)[:max_lines]


def _draw_block(img: Image.Image, text: str, font_path: Path, center_y: int, max_size: int,
                fill=(255, 255, 255), accent=(255, 214, 0), max_lines: int = 4) -> None:
    draw = ImageDraw.Draw(img)
    font, lines = _fit_text(draw, text, font_path, W - 160, max_size, max_lines=max_lines)
    line_h = int(font.size * 1.15)
    y = center_y - line_h * len(lines) // 2
    for i, line in enumerate(lines):
        x = (W - draw.textlength(line, font=font)) / 2
        draw.text((x, y), line, font=font, fill=accent if i == 0 and len(lines) > 1 else fill,
                  stroke_width=max(4, font.size // 14), stroke_fill=(0, 0, 0))
        y += line_h


def render_card(text: str, out: Path, font_path: Path, palette_idx: int = 0) -> Path:
    top, bottom = PALETTES[palette_idx % len(PALETTES)]
    img = _gradient((W, H), top, bottom)
    # upper-middle, clear of the caption band (~y 1080–1220) and the bottom UI zone
    _draw_block(img, text.upper(), font_path, 700, max_size=130, max_lines=3)
    img.save(out, quality=92)
    return out


def render_thumbnail(frame: Path, title: str, out: Path, font_path: Path) -> Path:
    base = Image.open(frame).convert("RGB").resize((W, H))
    base = base.filter(ImageFilter.GaussianBlur(2))
    shade = Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.blend(base, shade, 0.35)
    _draw_block(img, title.upper(), font_path, int(H * 0.40), max_size=130)
    img.save(out, "JPEG", quality=90)
    return out
