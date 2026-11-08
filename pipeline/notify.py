"""Stage: notify — Telegram review message.

Sends the video itself (Bot API limit for uploads is 50 MB; ours are ~20 MB) so it can be
reviewed on a phone, then a message with title, script, sources and the Studio link.
If Telegram isn't configured the stage logs a warning and records sent=false instead of
failing the run (the video is already uploaded).

Output: notify.json
"""
from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

from pipeline import http
from pipeline.config import Config, env
from pipeline.stages import StageContext
from pipeline.state import RunState, atomic_write_json, read_json

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_VIDEO_BYTES = 49_000_000


def _creds() -> tuple[str, str] | None:
    token, chat = env("TELEGRAM_BOT_TOKEN", required=False), env("TELEGRAM_CHAT_ID", required=False)
    return (token, chat) if token and chat else None


def _call(method: str, token: str, *, data: dict[str, Any], files: dict[str, Any] | None = None,
          timeout: float = 120) -> dict[str, Any]:
    r = http.session().post(API.format(token=token, method=method), data=data, files=files, timeout=timeout)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
    return body["result"]


def send_text(token: str, chat: str, text: str) -> None:
    # Telegram messages max out at 4096 chars
    for i in range(0, len(text), 4000):
        _call("sendMessage", token, data={"chat_id": chat, "text": text[i : i + 4000], "parse_mode": "HTML",
                                           "disable_web_page_preview": "true"})


def build_message(script: dict[str, Any], facts: dict[str, Any], uploads: list[dict[str, Any]],
                  dry_run: bool, qa: dict[str, Any] | None) -> str:
    e = html.escape
    lines = [f"<b>🎬 New Short ready for review</b>{' (DRY RUN — not uploaded)' if dry_run else ''}",
             f"<b>{e(script['title'])}</b>", ""]
    for u in uploads:
        lines.append(f"YouTube ({e(u['privacy'])}): {e(u['url'])}\nStudio: {e(u['studio_url'])}")
    if qa:
        cards = qa.get("text_card_shots", 0)
        lines.append(f"QA passed · {next((c['value'] for c in qa['checks'] if c['name'] == 'duration'), '?')}s"
                     + (f" · {cards} text-card shots" if cards else ""))
    lines += ["", f"<b>Hook:</b> {e(script['hook'])}"]
    for i, b in enumerate(script["beats"], start=1):
        lines.append(f"<b>{i}.</b> {e(b['narration'])}")
    lines += [f"<b>CTA:</b> {e(script['cta'])}", "", "<b>Sources</b>"]
    seen = set()
    for f in facts.get("facts", []):
        if f["source_url"] not in seen:
            seen.add(f["source_url"])
            lines.append(f"• {e(f['source_title'])}: {e(f['source_url'])}")
    return "\n".join(lines)


def run(ctx: StageContext) -> list[Path]:
    out = ctx.path("notify.json")
    creds = _creds()
    if not creds:
        log.warning("notify: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping review message")
        atomic_write_json(out, {"sent": False, "reason": "telegram not configured"})
        return [out]
    if not ctx.config.get("review.telegram", True):
        atomic_write_json(out, {"sent": False, "reason": "disabled in config"})
        return [out]

    token, chat = creds
    script = read_json(ctx.path("script.json"))
    facts = read_json(ctx.path("facts.json"))
    uploads = read_json(ctx.path("upload.json")).get("uploads", [])
    qa = read_json(ctx.path("qa.json")) if ctx.path("qa.json").exists() else None

    video = ctx.path("final.mp4")
    video_sent = False
    if video.exists() and video.stat().st_size <= MAX_VIDEO_BYTES:
        size = video.stat().st_size
        try:
            with video.open("rb") as fh:
                # allow for slow home upload links (~50 KB/s); CI uploads take seconds
                _call("sendVideo", token, timeout=max(120, size / 50_000),
                      data={"chat_id": chat, "caption": script["title"][:1000],
                            "supports_streaming": "true", "width": 1080, "height": 1920},
                      files={"video": ("short.mp4", fh, "video/mp4")})
            video_sent = True
        except Exception as e:  # the text review still matters more than the attachment
            log.warning("notify: video attachment failed (%s); sending text only", e)
    message = build_message(script, facts, uploads, ctx.dry_run, qa)
    if not video_sent:
        message += "\n\n(Video attachment failed to send; watch it via the YouTube link above.)"
    send_text(token, chat, message)
    log.info("notify: review sent to Telegram (video attached: %s)", video_sent)
    atomic_write_json(out, {"sent": True, "video_attached": video_sent})
    return [out]


def send_alert(config: Config, state: RunState, message: str) -> None:
    """Out-of-band alert (run aborted / failed)."""
    creds = _creds()
    if not creds:
        log.warning("Alert (Telegram not configured): %s", message)
        return
    send_text(creds[0], creds[1], f"⚠️ <b>auto-shorts</b>\n{html.escape(message)}")
