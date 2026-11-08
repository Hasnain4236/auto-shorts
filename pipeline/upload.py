"""Stage: upload — YouTube (and optionally Instagram), then record in the ledger.

YouTube:
  * resumable upload of final.mp4 with title/description/tags from script.json
  * status.containsSyntheticMedia = true (AI-assisted disclosure; field verified in the
    v3 discovery document), selfDeclaredMadeForKids = false, category 28 (Science & Tech)
  * privacy = upload.youtube_privacy, or "public" when upload.auto_publish_after_audit is
    true (only works once the API project has passed YouTube's compliance audit; before
    that, YouTube forces API uploads to private)
Idempotent: a duplicate topic in the ledger blocks the upload, and the ledger entry is
written immediately after a successful upload.

Output: upload.json
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any

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


def upload_youtube(ctx: StageContext, script: dict[str, Any]) -> dict[str, Any]:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    from pipeline.youtube_api import data_api

    cfg = ctx.config
    privacy = "public" if cfg.get("upload.auto_publish_after_audit") else cfg.get("upload.youtube_privacy", "private")
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
    final_privacy = response.get("status", {}).get("privacyStatus", privacy)
    log.info("upload: YouTube video %s (%s)", vid, final_privacy)
    if final_privacy != privacy:
        log.warning("upload: requested %s but YouTube set %s (unaudited API project?)", privacy, final_privacy)
    return {
        "platform": "youtube", "id": vid, "privacy": final_privacy,
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
