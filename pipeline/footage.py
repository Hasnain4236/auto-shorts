"""Stage: footage — a shot list aligned to the voice, with one stock clip per shot.

Slots come from timing.json: the hook, each beat (the cta rides on the last beat).
Beats longer than SPLIT_OVER_S get two shots (cut at the word nearest the middle)
using different keywords, a pattern interrupt every ~3–4 s to hold attention.

Search: Pexels (portrait filter) + Pixabay build a candidate pool; a vision model
(Gemini, free) looks at the top thumbnails next to the narration for that moment and
picks the best fit or rejects them all (stock tags alone are too loose: a "sparks"
query returns dogs in sweaters). No vision model -> tag matching. Nothing fits -> text card.
Clips are downloaded into a shared cache; attribution is stored per shot.

Graphics (video.graphics): a beat the writer gave a "graphic" (a number, comparison or
process) gets an animated graphic instead of stock, on the shot where the number is spoken
(pipeline/graphics.py).

Outputs: clips/ (shot media), footage.json
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from pipeline import graphics, http
from pipeline.cards import render_card
from pipeline.config import env
from pipeline.llm import LLMError
from pipeline.media import black_segments, font_path
from pipeline.stages import StageContext
from pipeline.state import atomic_write_json, read_json

log = logging.getLogger(__name__)

SPLIT_OVER_S = 5.5
PREFERRED_MAX_BYTES = 40_000_000
MAX_DOWNLOAD_BYTES = 150_000_000


NUMBER_WORDS = {"half", "double", "twice", "hundred", "thousand", "million", "billion", "trillion", "percent"}


def _number_word(words: list[dict[str, Any]], segment: str) -> dict[str, Any] | None:
    """The first spoken word in a beat that carries a number (digits or a number word)."""
    for w in words:
        token = re.sub(r"[^\w.]", "", str(w.get("text", "")).lower())
        if w.get("segment") == segment and (any(ch.isdigit() for ch in token) or token in NUMBER_WORDS):
            return w
    return None


def _attach_graphic(beat_shots: list[dict[str, Any]], graphic: dict[str, Any], segment: str,
                    words: list[dict[str, Any]]) -> None:
    """Put the beat's graphic on the shot where its number is spoken, and start the count there
    (early enough that the ~1.1 s count-up still finishes inside the shot). Steps have no number
    to wait for: they start the beat."""
    w = _number_word(words, segment) if graphic.get("type") != "steps" else None
    shot = next((s for s in beat_shots if w and s["start"] <= w["start"] < s["end"]), beat_shots[0])
    shot["graphic"] = graphic
    if w and shot["start"] <= w["start"] < shot["end"]:
        latest = max(0.2, shot["end"] - shot["start"] - 1.3)
        shot["graphic_at"] = round(min(max(0.2, w["start"] - shot["start"] - 0.1), latest), 2)
    else:
        shot["graphic_at"] = 0.3


def build_shots(script: dict[str, Any], timing: dict[str, Any], graphics: bool = False) -> list[dict[str, Any]]:
    segs = {s["name"]: s for s in timing["segments"]}
    words = timing.get("words", [])
    beats = script["beats"]
    total = timing["duration"]
    shots: list[dict[str, Any]] = []

    hook_kw = (beats[0].get("visual_keywords") or [script["hook"]])
    shots.append({"slot": "hook", "start": 0.0, "end": segs["hook"]["slot_end"],
                  "keywords": hook_kw[::-1], "on_screen_text": script["hook"]})
    for i, b in enumerate(beats, start=1):
        seg = segs[f"beat{i}"]
        start = seg["start"]
        end = total if i == len(beats) else seg["slot_end"]  # the cta rides on the last beat
        kws = list(b.get("visual_keywords") or [b.get("on_screen_text", "science")])
        first = len(shots)
        if end - start > SPLIT_OVER_S and len(kws) >= 2:
            mid = (start + end) / 2
            seg_words = [w for w in words if w["segment"] == f"beat{i}" and start + 1.5 < w["start"] < end - 1.5]
            cut = min(seg_words, key=lambda w: abs(w["start"] - mid))["start"] if seg_words else mid
            shots.append({"slot": f"beat{i}a", "start": start, "end": cut, "keywords": kws, "on_screen_text": b.get("on_screen_text", "")})
            shots.append({"slot": f"beat{i}b", "start": cut, "end": end, "keywords": kws[1:] + kws[:1], "on_screen_text": b.get("on_screen_text", "")})
        else:
            shots.append({"slot": f"beat{i}", "start": start, "end": end, "keywords": kws, "on_screen_text": b.get("on_screen_text", "")})
        if graphics and isinstance(b.get("graphic"), dict):
            _attach_graphic(shots[first:], b["graphic"], f"beat{i}", words)
    for s in shots:
        s["start"], s["end"] = round(s["start"], 3), round(s["end"], 3)
    return shots


# ---------------------------------------------------------------------------
# Providers: each returns candidates {id, provider, url, width, height, duration, page_url, author}
# ---------------------------------------------------------------------------

def search_pexels(query: str) -> list[dict[str, Any]]:
    key = env("PEXELS_API_KEY", required=False)
    if not key:
        return []
    r = http.session().get("https://api.pexels.com/videos/search", timeout=http.TIMEOUT,
                           headers={"Authorization": key},
                           params={"query": query, "orientation": "portrait", "per_page": 15, "size": "medium"})
    r.raise_for_status()
    out = []
    for v in r.json().get("videos", []):
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("height")]
        if not files:
            continue
        # smallest file that is at least 1080 wide in portrait, else the largest available
        good = [f for f in files if min(f["width"], f["height"]) >= 1080]
        f = min(good, key=lambda f: f["width"] * f["height"]) if good else max(files, key=lambda f: f["width"] * f["height"])
        # Pexels has no tags field; its page URL slug describes the clip ("/video/fireworks-at-night-123/")
        slug = (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        out.append({"id": f"pexels_{v['id']}_{f['width']}x{f['height']}", "provider": "pexels", "url": f["link"],
                    "width": f["width"], "height": f["height"], "duration": v.get("duration", 0),
                    "page_url": v.get("url", ""), "author": (v.get("user") or {}).get("name", ""), "tags": slug,
                    "thumb": v.get("image", "")})
    return out


def search_pixabay(query: str) -> list[dict[str, Any]]:
    key = env("PIXABAY_API_KEY", required=False)
    if not key:
        return []
    data = http.get_json("https://pixabay.com/api/videos/", {
        "key": key, "q": query[:100], "per_page": 20, "safesearch": "true", "video_type": "film"})
    out = []
    for h in data.get("hits", []):
        vids = h.get("videos", {})
        # 'large' is often 4K (100 MB+); take it only if reasonably small, else 'medium' (1080p)
        options = [vids[k] for k in ("large", "medium") if vids.get(k, {}).get("url") and vids[k].get("height", 0) >= 720]
        f = next((o for o in options if 0 < o.get("size", 0) <= PREFERRED_MAX_BYTES), options[-1] if options else None)
        if not f:
            continue
        out.append({"id": f"pixabay_{h['id']}_{f['width']}x{f['height']}", "provider": "pixabay", "url": f["url"],
                    "width": f["width"], "height": f["height"], "duration": h.get("duration", 0),
                    "page_url": h.get("pageURL", ""), "author": h.get("user", ""), "tags": h.get("tags", ""),
                    "thumb": f.get("thumbnail") or (vids.get("small") or {}).get("thumbnail", "")})
    return out


_STOP = {"a", "an", "the", "of", "in", "on", "and", "with", "for", "to", "at", "by", "close", "up", "shot", "view"}


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {w.rstrip("s") for w in words if w not in _STOP and len(w) > 2}


def relevance(query: str, c: dict[str, Any]) -> float:
    """Share of query terms found in the clip's tags / page slug. Stock search is loose
    (a 'sparks' query can return a dog in a sweater), so off-topic hits are rejected."""
    q = _terms(query)
    return len(q & _terms(c.get("tags", ""))) / len(q) if q else 0.0


def choose(cands: list[dict[str, Any]], query: str, need_s: float, used: set[str],
           min_relevance: float = 0.5) -> dict[str, Any] | None:
    def score(c: dict[str, Any]) -> float:
        s = 4.0 * c["relevance"]
        s += 2.0 if c["height"] > c["width"] else 0.0
        s += 1.0 if c["duration"] >= need_s else -1.5
        return s + min(c["height"], 1920) / 1920

    fresh = []
    for c in cands:
        if c["id"].rsplit("_", 1)[0] in used:
            continue
        c["relevance"] = relevance(query, c)
        if c["relevance"] >= min_relevance:
            fresh.append(c)
    return max(fresh, key=score) if fresh else None


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".part")
    with http.session().get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        size = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("clip too large")
                f.write(chunk)
    tmp.replace(dest)
    return dest


VISION_PROMPT = """You choose B-roll for a vertical science explainer short titled "{title}".
The attached images are thumbnails of candidate stock videos, in the order listed below.

{shots}

For EACH shot, pick the one image that best illustrates what the narrator says at that
moment, literally or as an obvious visual metaphor. Reject images that are unrelated, mostly
text/logos/watermarks, nearly black or blank, show a recognisable real person or celebrity,
or are distressing. Do not pick the same image for two shots.

Return JSON: {{"picks": [{{"shot": <shot number>, "best": <image number or null>,
"score": <0-10 fit>, "reason": "<few words>"}}]}}
Use null when no candidate for that shot scores at least 7. A clean text card is better than a loosely related clip."""

SHORTLIST = 4
SHOTS_PER_VISION_CALL = 6


def shot_narration(shot: dict[str, Any], words: list[dict[str, Any]]) -> str:
    return " ".join(w["text"] for w in words if shot["start"] - 0.05 <= w["start"] < shot["end"])


def gather_pool(ctx: StageContext, queries: list[str], used: set[str], need: float) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kw in queries:
        for search in (search_pexels, search_pixabay):
            try:
                results = search(kw)
                ctx.state.add_quota(f"footage.{search.__name__.removeprefix('search_')}")
            except Exception as e:
                log.warning("footage: %s failed for '%s': %s", search.__name__, kw, e)
                continue
            for c in results:
                base = c["id"].rsplit("_", 1)[0]
                if base in used or base in seen:
                    continue
                seen.add(base)
                c["query"], c["relevance"] = kw, relevance(kw, c)
                pool.append(c)
    pool.sort(key=lambda c: (-c["relevance"], -(c["height"] > c["width"]), -(c["duration"] >= need)))
    return pool


def _fetch_thumb(url: str) -> bytes | None:
    try:
        r = http.session().get(url, timeout=15)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def vision_pick_batch(ctx: StageContext, items: list[dict[str, Any]], title: str) -> dict[int, dict[str, Any] | None]:
    """items: [{idx, narration, headline, cands}] -> {idx: chosen candidate or None}.
    One vision call per SHOTS_PER_VISION_CALL shots. Raises LLMError if no vision model works."""
    picks: dict[int, dict[str, Any] | None] = {}
    for b in range(0, len(items), SHOTS_PER_VISION_CALL):
        chunk = items[b : b + SHOTS_PER_VISION_CALL]
        images: list[bytes] = []
        owners: list[tuple[int, dict[str, Any]]] = []   # image number -> (shot idx, candidate)
        lines = []
        for it in chunk:
            nums = []
            for c in it["cands"][:SHORTLIST]:
                data = _fetch_thumb(c.get("thumb", "")) if c.get("thumb") else None
                if data:
                    nums.append(len(images))
                    images.append(data)
                    owners.append((it["idx"], c))
            lines.append(f'Shot {it["idx"]}: narrator says "{it["narration"]}" (headline: "{it["headline"]}"). '
                         f'Candidate images: {", ".join(map(str, nums)) or "none"}')
        if not images:
            raise LLMError("no candidate thumbnails could be fetched")
        assigned: set[int] = set()
        result = ctx.llm_for("vision").complete_json(VISION_PROMPT.format(title=title, shots="\n".join(lines)),
                                       images=images, temperature=0.1)
        for pk in (result.get("picks", []) if isinstance(result, dict) else []):
            try:
                shot_i, best, score = int(pk.get("shot")), pk.get("best"), float(pk.get("score") or 0)
            except (TypeError, ValueError):
                continue
            # The model may pick an image fetched for a neighbouring shot (e.g. any fireworks clip
            # fits several beats). That's fine as long as no two shots get the same image.
            if (isinstance(best, int) and 0 <= best < len(owners) and score >= 7
                    and best not in assigned and shot_i in {it["idx"] for it in chunk}):
                assigned.add(best)
                cand = dict(owners[best][1])
                cand["vision"] = {"score": score, "reason": pk.get("reason", "")}
                picks[shot_i] = cand
        raw = result.get("picks", []) if isinstance(result, dict) else []
        accepted = sum(1 for it in chunk if picks.get(it["idx"]))
        log.info("footage: vision chunk %d shots, %d images -> %d accepted; raw: %s", len(chunk), len(images),
                 accepted, [(p.get("shot"), p.get("best"), p.get("score")) for p in raw if isinstance(p, dict)])
        for it in chunk:
            picks.setdefault(it["idx"], None)
    return picks


def select_footage(ctx: StageContext, shots: list[dict[str, Any]], words: list[dict[str, Any]],
                   title: str) -> dict[int, dict[str, Any] | None]:
    """Two rounds: specific keywords, then broader 2-word queries for shots nothing fitted."""
    chosen: dict[int, dict[str, Any] | None] = {i: None for i in range(len(shots))}
    used: set[str] = set()
    for rnd in range(2):
        items = []
        for i, shot in enumerate(shots):
            if chosen[i]:
                continue
            kws = list(shot["keywords"])
            queries = kws[:2] if rnd == 0 else [" ".join(k.split()[-2:]) for k in kws if len(k.split()) > 2] + kws[2:]
            pool = gather_pool(ctx, list(dict.fromkeys(q for q in queries if q)), used, shot["end"] - shot["start"])
            if pool:
                items.append({"idx": i, "narration": shot_narration(shot, words) or shot["on_screen_text"],
                              "headline": shot["on_screen_text"], "cands": pool, "need": shot["end"] - shot["start"]})
        if not items:
            continue
        try:
            picks = vision_pick_batch(ctx, items, title)
        except LLMError as e:
            log.warning("footage: vision check unavailable (%s); using tag matching", e)
            picks = {it["idx"]: choose(it["cands"], it["cands"][0]["query"], it["need"], used) for it in items}
        for idx, cand in picks.items():
            base = cand["id"].rsplit("_", 1)[0] if cand else None
            if cand and base not in used:
                chosen[idx] = cand
                used.add(base)
    return chosen


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    script = read_json(ctx.path("script.json"))
    timing = read_json(ctx.path("timing.json"))
    clips_dir = ctx.path("clips")
    clips_dir.mkdir(exist_ok=True)
    cache = cfg.runs_dir / "_cache" / "footage"
    cache.mkdir(parents=True, exist_ok=True)
    font = font_path(cfg)
    fps = int(cfg.get("video.fps", 30))

    shots = build_shots(script, timing, graphics=bool(cfg.get("video.graphics", False)))
    # graphics are drawn, not searched: only the other shots go to the stock search
    stock = [n for n, s in enumerate(shots) if not s.get("graphic")]
    found = select_footage(ctx, [shots[n] for n in stock], timing.get("words", []), script["title"]) if stock else {}
    chosen = {n: found.get(k) for k, n in enumerate(stock)}
    for n, shot in enumerate(shots):
        need = shot["end"] - shot["start"]
        if shot.get("graphic"):
            dest = clips_dir / f"{n:02d}_{shot['slot']}.mp4"
            try:
                # a little longer than the shot, so compose never has to loop it
                graphics.render(shot["graphic"], dest, need + 0.5, fps, font, palette_idx=n,
                                start_at=shot.get("graphic_at", 0.3))
                shot.update(kind="graphic", file=dest.name, attribution=None)
                log.info("footage: %-7s %.1fs graphic: %s", shot["slot"], need, graphics.spec_text(shot["graphic"]))
                continue
            except Exception as e:
                log.warning("footage: graphic for %s failed (%s); using a text card", shot["slot"], e)
            chosen[n] = None
        picked = chosen.get(n)
        if picked:
            try:
                cached = download(picked["url"], cache / f"{picked['id']}.mp4")
                # Specks on black, a starless night, a long fade-in: on a phone that reads as a dead
                # screen (and QA rejects the whole video for it), so a text card goes there instead.
                if black_segments(cached, seconds=need + 1.0):
                    raise ValueError("clip is mostly black where it would be shown")
                dest = clips_dir / f"{n:02d}_{shot['slot']}.mp4"
                shutil.copyfile(cached, dest)
                shot.update(kind="stock", file=dest.name, query=picked["query"], width=picked["width"],
                            height=picked["height"], clip_duration=picked["duration"], vision=picked.get("vision"),
                            attribution={k: picked[k] for k in ("provider", "author", "page_url")})
                log.info("footage: %-7s %.1fs '%s' -> %s %sx%s %s", shot["slot"], need, picked["query"],
                         picked["provider"], picked["width"], picked["height"],
                         (picked.get("vision") or {}).get("reason", ""))
                continue
            except Exception as e:
                log.warning("footage: can't use the clip for %s (%s); using a text card", shot["slot"], e)
        dest = clips_dir / f"{n:02d}_{shot['slot']}.jpg"
        render_card(shot["on_screen_text"] or script["title"], dest, font, palette_idx=n)
        shot.update(kind="card", file=dest.name, attribution=None)
        log.info("footage: %-7s %.1fs no fitting stock clip -> text card", shot["slot"], need)

    out = ctx.path("footage.json")
    atomic_write_json(out, {"shots": shots})
    return [out, clips_dir]
