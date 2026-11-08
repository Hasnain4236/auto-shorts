"""Engagement learning loop:  python main.py learn   (run daily, before `run`)

1. Link ledger entries to live YouTube videos: the uploaded ID, or, for videos that were
   re-uploaded by hand, a channel upload whose title matches the ledger title.
2. Pull per-video analytics (YouTube Analytics API, free) for videos at least
   learning.min_age_days old: views, average % viewed, watch time, likes, shares, subs,
   and the audience-retention curve (where people swipe away).
3. Score videos relative to each other (percentile ranks) and update the experiment
   arms (e.g. which hook style performs best).
4. With enough data, ask the LLM to write a playbook of concrete do/avoid guidance from
   the best vs worst videos. trends and script inject it into their prompts.

Outputs (committed, so CI keeps them): data/performance.json, data/playbook.json
Honest limits: Shorts performance is noisy, so the playbook only kicks in after
learning.min_videos videos and says how confident it is. Private videos get no views,
so nothing is learned until videos are public.
"""
from __future__ import annotations

import logging
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline.config import Config
from pipeline.state import Ledger, atomic_write_json, read_json, similarity

log = logging.getLogger(__name__)

HOOK_TYPES = ["counterintuitive", "stakes", "question"]


def data_path(config: Config, name: str) -> Path:
    return config.ledger_path.parent / name


def load_playbook(config: Config) -> dict[str, Any]:
    p = data_path(config, "playbook.json")
    try:
        return read_json(p) if p.exists() else {}
    except ValueError:
        return {}


def playbook_prompt(config: Config, section: str) -> str:
    """Text block for prompts. section: 'script' or 'topics'."""
    pb = load_playbook(config).get("guidance")
    if not pb:
        return ""
    if section == "topics":
        items = pb.get("topic_guidance", [])
        return ("\nLEARNED FROM THIS CHANNEL'S ANALYTICS (prefer topics like the winners):\n"
                + "\n".join(f"- {x}" for x in items)) if items else ""
    items = pb.get("do", []) + [f"Avoid: {x}" for x in pb.get("avoid", [])] + pb.get("hook_guidance", []) \
        + pb.get("pacing_guidance", [])
    return ("\nLEARNED FROM THIS CHANNEL'S ANALYTICS (follow unless it conflicts with accuracy or the rules above; "
            f"confidence: {pb.get('confidence', 'low')}):\n" + "\n".join(f"- {x}" for x in items)) if items else ""


def choose_arm(config: Config, dimension: str, options: list[str], rng: random.Random | None = None) -> str:
    """Epsilon-greedy: explore with prob learning.explore_rate or while any arm has < 2 samples."""
    rng = rng or random.Random()
    arms = load_playbook(config).get("arms", {}).get(dimension, {})
    eps = float(config.get("learning.explore_rate", 0.3))
    under = [o for o in options if arms.get(o, {}).get("n", 0) < 2]
    if under:
        return rng.choice(under)
    if rng.random() < eps:
        return rng.choice(options)
    return max(options, key=lambda o: arms.get(o, {}).get("mean", 0.0))


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def channel_uploads(yt: Any, limit: int = 200) -> list[dict[str, Any]]:
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    if not ch.get("items"):
        return []
    playlist = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    out, token = [], None
    while len(out) < limit:
        resp = yt.playlistItems().list(part="snippet,contentDetails", playlistId=playlist, maxResults=50,
                                       pageToken=token).execute()
        for it in resp.get("items", []):
            out.append({"id": it["contentDetails"]["videoId"], "title": it["snippet"]["title"],
                        "published": it["contentDetails"].get("videoPublishedAt") or it["snippet"]["publishedAt"]})
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def link_videos(entries: list[dict[str, Any]], uploads: list[dict[str, Any]], statuses: dict[str, str]) -> None:
    """Point each ledger entry at its *public* video (manual re-uploads are matched by title)."""
    for e in entries:
        if e.get("youtube_id") and statuses.get(e["youtube_id"]) == "public":
            e["live_id"] = e["youtube_id"]
            continue
        best = max(uploads, key=lambda u: similarity(u["title"], e.get("title", "")), default=None)
        if best and similarity(best["title"], e.get("title", "")) > 0.85 and statuses.get(best["id"]) == "public":
            e["live_id"] = best["id"]


def video_statuses(yt: Any, ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(part="status", id=",".join(ids[i : i + 50])).execute()
        out.update({v["id"]: v["status"]["privacyStatus"] for v in resp.get("items", [])})
    return out


def check_overdue(config: Config, ledger: Ledger, statuses: dict[str, str]) -> list[dict[str, Any]]:
    """Scheduled videos whose go-live time has passed but are still not public -> one alert each."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    overdue = [e for e in ledger.entries
               if e.get("publish_at") and e["publish_at"] < now and e.get("youtube_id")
               and statuses.get(e["youtube_id"]) not in ("public", None) and not e.get("overdue_alerted")]
    if overdue:
        from pipeline import notify

        for e in overdue:
            msg = (f"Scheduled Short is still {statuses.get(e['youtube_id'])} after its go-live time: "
                   f"{e.get('title')} https://studio.youtube.com/video/{e['youtube_id']}/edit")
            log.warning("learn: %s", msg)
            try:
                notify.send_alert(config, None, msg)
            except Exception:
                log.exception("learn: alert failed")
            e["overdue_alerted"] = True
        ledger.save()
    return overdue


def video_metrics(an: Any, vid: str, published: str) -> dict[str, Any]:
    start = published[:10]
    end = date.today().isoformat()
    base = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,shares,comments,subscribersGained"
    row: dict[str, Any] = {}
    for metrics in (base + ",engagedViews", base):  # engagedViews (Shorts) may not exist on every API version
        try:
            r = an.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics=metrics,
                                   filters=f"video=={vid}").execute()
            heads = [h["name"] for h in r.get("columnHeaders", [])]
            if r.get("rows"):
                row = dict(zip(heads, r["rows"][0]))
            break
        except Exception as e:
            log.debug("analytics query with %s failed: %s", metrics, e)
    try:
        r = an.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics="audienceWatchRatio",
                               dimensions="elapsedVideoTimeRatio", filters=f"video=={vid}").execute()
        row["retention"] = [[float(a), float(b)] for a, b in r.get("rows", [])]
    except Exception as e:
        log.debug("retention query failed for %s: %s", vid, e)
    return row


def _at(curve: list[list[float]], x: float) -> float | None:
    return min(curve, key=lambda p: abs(p[0] - x))[1] if curve else None


def summarise_retention(curve: list[list[float]]) -> dict[str, Any]:
    """Hook hold (~first 5%), mid hold, end hold, and the biggest drop position."""
    if len(curve) < 5:
        return {}
    drops = [(curve[i][0], curve[i - 1][1] - curve[i][1]) for i in range(1, len(curve))]
    worst = max(drops, key=lambda d: d[1])
    return {"hook_hold": _at(curve, 0.05), "mid_hold": _at(curve, 0.5), "end_hold": _at(curve, 0.95),
            "biggest_drop_at": round(worst[0], 2)}


# ---------------------------------------------------------------------------
# Scoring, arms, playbook
# ---------------------------------------------------------------------------

def _pct_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for r, i in enumerate(order):
        ranks[i] = r / max(len(values) - 1, 1)
    return ranks


def score_videos(rows: list[dict[str, Any]]) -> None:
    """Relative score: retention matters most for Shorts distribution, then reach, then hook hold."""
    if not rows:
        return
    pct = _pct_ranks([r.get("averageViewPercentage") or 0 for r in rows])
    views = _pct_ranks([math.log1p(r.get("views") or 0) for r in rows])
    hook = _pct_ranks([r.get("hook_hold") or 0 for r in rows])
    for r, a, b, c in zip(rows, pct, views, hook):
        r["score"] = round(0.45 * a + 0.35 * b + 0.20 * c, 3)


def arm_stats(rows: list[dict[str, Any]], dimension: str) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for r in rows:
        arm = (r.get("features") or {}).get(dimension) or ((r.get("features") or {}).get("experiment") or {}).get(dimension)
        if arm:
            s = stats.setdefault(arm, {"n": 0, "sum": 0.0})
            s["n"] += 1
            s["sum"] += r["score"]
    return {k: {"n": v["n"], "mean": round(v["sum"] / v["n"], 3)} for k, v in stats.items()}


PLAYBOOK_PROMPT = """You are the growth analyst for a YouTube Shorts channel.
Niche: {niche}
Below are the channel's videos with real analytics, best first. "avg_view_pct" is the
average % of the video watched (can exceed 100 with replays), "hook_hold" is the share
of viewers still watching at ~5% of the video, "biggest_drop_at" is where most viewers left
(0 = start, 1 = end).

{table}

Compare the top third with the bottom third. Write SPECIFIC, actionable guidance that the
script writer and topic picker can follow, grounded only in patterns visible in this data.
With fewer than ~20 videos, patterns may be noise: say so via "confidence".

Return JSON: {{"summary": "<2 sentences>",
 "do": ["<3-5 concrete script instructions>"],
 "avoid": ["<2-4 concrete things to stop doing>"],
 "hook_guidance": ["<1-3 instructions about hooks>"],
 "pacing_guidance": ["<1-2 instructions about length/pacing>"],
 "topic_guidance": ["<2-4 kinds of topics/angles that work or don't>"],
 "confidence": "low|medium|high"}}"""


def write_playbook(config: Config, rows: list[dict[str, Any]], llm: Any) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: -r["score"])
    table = "\n".join(
        f"- score {r['score']:.2f} | views {r.get('views', 0)} | avg_view_pct {r.get('averageViewPercentage')} | "
        f"hook_hold {r.get('hook_hold')} | biggest_drop_at {r.get('biggest_drop_at')} | "
        f"{(r.get('features') or {}).get('duration_s')}s | topic: {r.get('topic')} | title: {r.get('title')} | "
        f"hook ({(r.get('features') or {}).get('hook_type')}): {(r.get('features') or {}).get('hook')}"
        for r in rows
    )
    return llm.complete_json(PLAYBOOK_PROMPT.format(niche=config.get("channel.niche"), table=table), temperature=0.3)


def learned_recently(config: Config, now: datetime | None = None) -> str | None:
    """When analytics were last pulled, if within learning.every_hours (else None).

    Several runs a day all start with `learn`; analytics only change daily, so only the
    first run of the day pulls them (and spends the playbook LLM call)."""
    path = data_path(config, "performance.json")
    last = read_json(path).get("updated") if path.exists() else None
    if not last:
        return None
    age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(last)
    return last if age < timedelta(hours=float(config.get("learning.every_hours", 20))) else None


def run_learning(config: Config, llm_factory: Any = None, force: bool = False) -> dict[str, Any]:
    from pipeline.youtube_api import analytics_api, data_api

    if not force and (last := learned_recently(config)):
        log.info("learn: analytics already pulled at %s; skipping (use --force to pull again)", last)
        return {}
    ledger = Ledger(config.ledger_path)
    entries = [e for e in ledger.entries if e.get("title")]
    if not entries:
        log.info("learn: ledger is empty; nothing to learn yet")
        return {}
    yt, an = data_api(), analytics_api()
    uploads = channel_uploads(yt)
    ids = list({u["id"] for u in uploads} | {e["youtube_id"] for e in entries if e.get("youtube_id")})
    statuses = video_statuses(yt, ids)
    check_overdue(config, ledger, statuses)
    link_videos(entries, uploads, statuses)
    published = {u["id"]: u["published"] for u in uploads}

    min_age = int(config.get("learning.min_age_days", 3))
    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for e in entries:
        vid = e.get("live_id")
        if not vid:
            continue
        pub = published.get(vid) or e.get("ts")
        age = (now - datetime.fromisoformat(pub.replace("Z", "+00:00"))).days
        if age < min_age:
            continue
        m = video_metrics(an, vid, pub)
        m.update(summarise_retention(m.get("retention", [])))
        rows.append({"video_id": vid, "age_days": age, "topic": e.get("topic"), "title": e.get("title"),
                     "features": e.get("features", {}), **{k: v for k, v in m.items() if k != "retention"},
                     "retention": m.get("retention", [])})
    private = sum(1 for e in entries if not e.get("live_id"))
    log.info("learn: %d public videos with data, %d not public yet", len(rows), private)

    score_videos(rows)
    atomic_write_json(data_path(config, "performance.json"),
                      {"updated": now.isoformat(timespec="seconds"), "videos": rows})

    playbook = load_playbook(config)
    playbook["arms"] = {"hook_type": arm_stats(rows, "hook_type")}
    playbook["updated"] = now.isoformat(timespec="seconds")
    playbook["videos_analysed"] = len(rows)
    if len(rows) >= int(config.get("learning.min_videos", 6)) and llm_factory:
        playbook["guidance"] = write_playbook(config, rows, llm_factory())
        log.info("learn: playbook updated (%s confidence): %s", playbook["guidance"].get("confidence"),
                 playbook["guidance"].get("summary"))
    else:
        log.info("learn: need %s+ public videos for a playbook (have %d)", config.get("learning.min_videos", 6), len(rows))
    atomic_write_json(data_path(config, "playbook.json"), playbook)
    return playbook
