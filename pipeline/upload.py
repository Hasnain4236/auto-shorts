"""Stage: upload — YouTube (and optionally Instagram), then record in the ledger.

YouTube:
  * resumable upload of final.mp4 with title/description/tags from script.json
  * status.containsSyntheticMedia = true (AI-assisted disclosure; field verified in the
    v3 discovery document), selfDeclaredMadeForKids = false, category 28 (Science & Tech)
  * upload.publish_mode:
      private   -> stays private; you publish in Studio
      scheduled -> uploaded private with status.publishAt, so YouTube itself makes it public
                   at upload.publish_time (a review window: stop it in Studio before then)
      public    -> public immediately
    The response is checked: if YouTube didn't keep the requested schedule/privacy, the
    Telegram review says so, and `learn` alerts about scheduled videos that never went live.
Idempotent: a duplicate topic in the ledger blocks the upload, and the ledger entry is
written immediately after a successful upload.

Output: upload.json
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.stages import AbortRun, StageContext
from pipeline.state import Ledger, atomic_write_json, read_json

log = logging.getLogger(__name__)

CATEGORY_SCIENCE_TECH = "28"
RETRIABLE_STATUS = {500, 502, 503, 504}


def _title(script: dict[str, Any]) -> str:
    t = script["title"].strip()
    return t if len(t) <= 100 else t[:97] + "..."


def build_description(ctx: StageContext, script: dict[str, Any]) -> str:
    """Script description (summary, sources, AI disclosure, hashtags) + music credit if the track needs one."""
    desc = script["description"].strip()
    meta = read_json(ctx.path("compose.json")) if ctx.path("compose.json").exists() else {}
    if meta.get("music_attribution"):
        desc += "\n\n" + meta["music_attribution"]
    return desc[:5000]


def compute_publish_at(now_utc: datetime, publish_time: str, tz_name: str, min_review_hours: float) -> datetime:
    """Next `publish_time` (local, in tz_name) that leaves at least min_review_hours to review."""
    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)
    hh, mm = (int(x) for x in publish_time.split(":"))
    target = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target - local_now < timedelta(hours=min_review_hours):
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def publish_plan(cfg: Any, now_utc: datetime | None = None) -> dict[str, Any]:
    """-> {mode, privacy (sent to YouTube), publish_at (RFC 3339 UTC or None), publish_at_local}."""
    mode = cfg.get("upload.publish_mode")
    if not mode:  # older config keys
        mode = "public" if cfg.get("upload.auto_publish_after_audit") else cfg.get("upload.youtube_privacy", "private")
    if mode == "public":
        return {"mode": "public", "privacy": "public", "publish_at": None, "publish_at_local": None}
    if mode == "scheduled":
        tz_name = cfg.get("upload.timezone", "Asia/Kolkata")
        at = compute_publish_at(now_utc or datetime.now(timezone.utc), str(cfg.get("upload.publish_time", "19:30")),
                                tz_name, float(cfg.get("upload.min_review_hours", 2)))
        local = at.astimezone(ZoneInfo(tz_name))
        return {"mode": "scheduled", "privacy": "private", "publish_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "publish_at_local": local.strftime("%H:%M %Z, %a %d %b")}
    return {"mode": "private", "privacy": "private", "publish_at": None, "publish_at_local": None}


def upload_youtube(ctx: StageContext, script: dict[str, Any]) -> dict[str, Any]:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    from pipeline.youtube_api import data_api

    cfg = ctx.config
    plan = publish_plan(cfg)
    privacy = plan["privacy"]
    lang = cfg.get("channel.language", "en")
    body = {
        "snippet": {
            "title": _title(script),
            "description": build_description(ctx, script),
            "tags": [t for t in script.get("tags", []) if t][:15],
            "categoryId": CATEGORY_SCIENCE_TECH,
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
            "embeddable": True,
            **({"publishAt": plan["publish_at"]} if plan["publish_at"] else {}),
        },
    }
    media = MediaFileUpload(str(ctx.path("final.mp4")), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = data_api().videos().insert(part="snippet,status", body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info("upload: %d%%", int(status.progress() * 100))
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS and retries < 5:
                retries += 1
                wait = 2**retries + random.random()
                log.warning("upload: HTTP %s, retrying in %.0fs", e.resp.status, wait)
                time.sleep(wait)
                continue
            if e.resp.status == 403 and b"quotaExceeded" in e.content:
                raise RuntimeError("YouTube Data API daily quota exceeded; rerun tomorrow to resume") from e
            raise
    ctx.state.add_quota("youtube.videos_insert")
    vid = response["id"]
    status = response.get("status", {})
    final_privacy = status.get("privacyStatus", privacy)
    scheduled_at = status.get("publishAt")
    problem = None
    if final_privacy != privacy:
        problem = f"YouTube set the video to {final_privacy} instead of {privacy}"
    elif plan["publish_at"] and not scheduled_at:
        problem = "YouTube did not keep the scheduled publish time"
    if problem:
        log.warning("upload: %s; publish it manually in Studio", problem)
    log.info("upload: YouTube video %s (%s%s)", vid, final_privacy,
             f", goes public {plan['publish_at_local']}" if scheduled_at else "")
    return {
        "platform": "youtube", "id": vid, "privacy": final_privacy, "mode": plan["mode"],
        "publish_at": scheduled_at, "publish_at_local": plan["publish_at_local"] if scheduled_at else None,
        "problem": problem,
        "url": f"https://youtube.com/shorts/{vid}",
        "studio_url": f"https://studio.youtube.com/video/{vid}/edit",
    }


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    out = ctx.path("upload.json")
    if ctx.dry_run:
        log.info("upload: dry run — skipping uploads")
        atomic_write_json(out, {"skipped": "dry_run", "uploads": []})
        return [out]

    script = read_json(ctx.path("script.json"))
    facts = read_json(ctx.path("facts.json"))
    topic = facts.get("angle") or facts["topic"]

    ledger = Ledger(cfg.ledger_path)
    dup = ledger.find_duplicate(topic, float(cfg.get("trends.duplicate_similarity", 0.85)))
    if dup and dup.get("run_id") != ctx.state.run_id:
        raise AbortRun(f"Topic already uploaded on {dup.get('ts')}: {dup.get('topic')}")

    uploads: list[dict[str, Any]] = []
    if cfg.get("upload.youtube", True):
        uploads.append(upload_youtube(ctx, script))
    if cfg.get("upload.instagram", False):
        log.warning("upload: Instagram is enabled in config but not built yet (Phase 6); skipping")

    # Everything the learning loop needs to relate performance back to choices made.
    ledger.append({
        "topic": topic,
        "title": script["title"],
        "run_id": ctx.state.run_id,
        "youtube_id": next((u["id"] for u in uploads if u["platform"] == "youtube"), None),
        "publish_at": next((u.get("publish_at") for u in uploads if u["platform"] == "youtube"), None),
        "features": {
            "hook": script.get("hook"),
            "hook_type": script.get("hook_type"),
            "trend_source": (read_json(ctx.path("topics.json")).get("selected") or {}).get("source"),
            "words": script.get("word_count"),
            "duration_s": read_json(ctx.path("timing.json")).get("duration"),
            "voice": read_json(ctx.path("timing.json")).get("voice"),
            "music": (read_json(ctx.path("compose.json")) if ctx.path("compose.json").exists() else {}).get("music"),
            "experiment": ctx.state.data.get("experiment"),
        },
    })
    atomic_write_json(out, {"uploads": uploads})
    return [out]
