"""Stage: trends — collect trending topics, dedupe, score niche fit, pick one.

Sources (each optional; a broken or unconfigured source is logged and skipped):
  * YouTube Data API v3  videos.list chart=mostPopular       (needs YOUTUBE_API_KEY, 1 unit/call)
  * Google Trends        public "trending now" RSS feed, then pytrends as a fallback
  * Reddit               official API via praw, read-only     (needs REDDIT_CLIENT_ID/SECRET)
Trends only ever supply the *topic*. No source video, audio or text is reused.

Output: topics.json = {candidates: [...], selected: {...}, rejected: []}
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pipeline import http
from pipeline.config import Config, env
from pipeline.learn import playbook_prompt
from pipeline.stages import AbortRun, StageContext
from pipeline.state import ABORTED, Ledger, RunState, atomic_write_json, read_json, similarity

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    title: str
    source: str
    url: str = ""
    popularity: float = 0.0          # 0–1, rank-normalised within its source
    context: str = ""                # short snippet used only to help the LLM understand the trend
    related_urls: list[str] = field(default_factory=list)  # news links (possible fact sources)
    sources: list[str] = field(default_factory=list)       # all sources that surfaced it (after dedupe)
    niche_relevance: float = 0.0
    hook_potential: float = 0.0
    score: float = 0.0
    angle: str = ""                  # the explainer question the video would answer
    search_queries: list[str] = field(default_factory=list)


def _rank_scores(n: int) -> list[float]:
    return [1.0 - i / max(n, 1) for i in range(n)]


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_youtube(config: Config, ctx: StageContext | None = None) -> list[Candidate]:
    key = env("YOUTUBE_API_KEY", required=False)
    if not key:
        log.info("YouTube trends skipped (YOUTUBE_API_KEY unset)")
        return []
    category_ids = config.get("trends.youtube_category_ids") or [None]
    out: list[Candidate] = []
    for cat in category_ids:
        params: dict[str, Any] = {
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "regionCode": config.get("trends.youtube_region", "IN"),
            "maxResults": 50,
            "key": key,
        }
        if cat is not None:
            params["videoCategoryId"] = str(cat)
        data = http.get_json("https://www.googleapis.com/youtube/v3/videos", params)
        if ctx:
            ctx.state.add_quota("youtube.units", 1)
        items = data.get("items", [])
        for item, pop in zip(items, _rank_scores(len(items))):
            sn = item.get("snippet", {})
            out.append(Candidate(
                title=sn.get("title", ""),
                source="youtube",
                url=f"https://www.youtube.com/watch?v={item.get('id')}",
                popularity=pop,
                context=(sn.get("description") or "")[:200],
            ))
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def collect_google_trends_rss(config: Config) -> list[Candidate]:
    geo = config.get("trends.google_trends_geo", "IN")
    xml_text = http.get_text("https://trends.google.com/trending/rss", {"geo": geo})
    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    out: list[Candidate] = []
    for item, pop in zip(items, _rank_scores(len(items))):
        title, news_titles, news_urls = "", [], []
        for child in item.iter():
            name = _local(child.tag)
            if name == "title" and not title:
                title = (child.text or "").strip()
            elif name == "news_item_title" and child.text:
                news_titles.append(child.text.strip())
            elif name == "news_item_url" and child.text:
                news_urls.append(child.text.strip())
        if title:
            out.append(Candidate(
                title=title, source="google_trends", popularity=pop,
                context=" | ".join(news_titles[:3]), related_urls=news_urls[:3],
                url=news_urls[0] if news_urls else "",
            ))
    return out


_PYTRENDS_PN = {"IN": "india", "US": "united_states", "GB": "united_kingdom", "CA": "canada", "AU": "australia"}


def collect_google_trends_pytrends(config: Config) -> list[Candidate]:
    from pytrends.request import TrendReq  # unofficial; frequently breaks

    geo = config.get("trends.google_trends_geo", "IN")
    df = TrendReq(hl="en-US", tz=330).trending_searches(pn=_PYTRENDS_PN.get(geo, "india"))
    titles = [str(t) for t in df[0].tolist()]
    return [Candidate(title=t, source="google_trends", popularity=p) for t, p in zip(titles, _rank_scores(len(titles)))]


def collect_google_trends(config: Config) -> list[Candidate]:
    try:
        items = collect_google_trends_rss(config)
        if items:
            return items
    except Exception as e:
        log.warning("Google Trends RSS failed (%s); trying pytrends", e)
    return collect_google_trends_pytrends(config)


def collect_reddit(config: Config) -> list[Candidate]:
    cid, secret = env("REDDIT_CLIENT_ID", required=False), env("REDDIT_CLIENT_SECRET", required=False)
    if not (cid and secret):
        log.info("Reddit trends skipped (REDDIT_CLIENT_ID/SECRET unset)")
        return []
    import praw

    reddit = praw.Reddit(
        client_id=cid, client_secret=secret,
        user_agent=env("REDDIT_USER_AGENT", required=False) or http.USER_AGENT,
    )
    reddit.read_only = True
    out: list[Candidate] = []
    for sub in config.get("trends.subreddits", []):
        posts = [p for p in reddit.subreddit(sub).hot(limit=30) if not p.stickied and not p.over_18][:20]
        for post, pop in zip(posts, _rank_scores(len(posts))):
            link = "" if post.is_self else post.url
            out.append(Candidate(
                title=post.title, source=f"reddit/r/{sub}",
                url=f"https://www.reddit.com{post.permalink}", popularity=pop,
                context=(post.selftext or "")[:200],
                related_urls=[link] if http.is_article_url(link) else [],
            ))
    return out


def collect_all(config: Config, ctx: StageContext | None = None) -> list[Candidate]:
    collectors = {
        "youtube": lambda: collect_youtube(config, ctx),
        "google_trends": lambda: collect_google_trends(config),
        "reddit": lambda: collect_reddit(config),
    }
    out: list[Candidate] = []
    for name, fn in collectors.items():
        try:
            got = fn()
            log.info("trends: %s returned %d candidates", name, len(got))
            out += got
        except Exception as e:  # one broken source must not kill the run
            log.warning("trends: %s failed: %s", name, e)
    return out


# ---------------------------------------------------------------------------
# Dedupe, score, pick
# ---------------------------------------------------------------------------

def dedupe(cands: list[Candidate], threshold: float = 0.85) -> list[Candidate]:
    kept: list[Candidate] = []
    for c in sorted(cands, key=lambda c: -c.popularity):
        if not c.title.strip():
            continue
        twin = next((k for k in kept if similarity(k.title, c.title) > threshold), None)
        if twin:
            twin.sources.append(c.source)
            twin.related_urls += [u for u in c.related_urls if u not in twin.related_urls]
            twin.popularity = min(1.0, twin.popularity + 0.1)  # surfaced by several sources => hotter
        else:
            c.sources = [c.source]
            kept.append(c)
    return kept


SCORE_SYSTEM = "You are the editor of a short-form science & tech explainer channel. You judge topics strictly and return JSON."

SCORE_PROMPT = """Channel niche: {niche}
Audience: {audience}
Never pick topics about: {avoid}

Below are trending items. Many are NOT in the niche (sports scores, film releases, politics...).
For each item, decide whether it can become an ORIGINAL 45–60 s explainer that teaches the viewer
how or why something works. A trend can qualify through a science/tech angle
(e.g. a cricket match trending -> "why does a cricket ball swing?") but only if that angle is
genuinely interesting, not forced.

The channel has ALREADY covered these questions. Give each item an angle that is a genuinely
different question; if the only good angle would repeat one of them, mark it "covered":
{covered}

Return JSON: {{"items": [{{
  "i": <item number>,
  "niche_relevance": <0–1, how well the best explainer angle fits the niche>,
  "hook_potential": <0–1, how likely a casual viewer stops scrolling: surprise, everyday relevance, "wait, what?">,
  "angle": "<the single question the video answers, max 15 words>",
  "search_queries": ["<2–3 Wikipedia-style search terms for researching the angle>"],
  "avoid": <true if it touches the avoid list or is about a specific real private person>,
  "covered": <true if the best angle basically repeats an already-covered question>
}}]}}
Include every item.
{playbook}
Items:
{items}"""


def covered_topics(ledger: Ledger, n: int = 40) -> str:
    """The channel's recent questions, for prompts. Fuzzy string matching misses rewordings
    ("video games simulate car crashes" vs "BeamNG.Drive simulate crashes"); the model doesn't."""
    return "; ".join(e.get("topic", "") for e in ledger.entries[-n:] if e.get("topic")) or "none"


def score_candidates(ctx: StageContext, cands: list[Candidate], covered: str = "none") -> list[Candidate]:
    cfg = ctx.config
    limit = int(cfg.get("trends.max_candidates_scored", 60))
    batch = cands[:limit]
    lines = "\n".join(
        f"{i}. [{c.source}] {c.title}" + (f" — {c.context[:150]}" if c.context else "")
        for i, c in enumerate(batch)
    )
    result = ctx.llm.complete_json(
        SCORE_PROMPT.format(
            niche=cfg.get("channel.niche"), audience=cfg.get("channel.audience"),
            avoid=", ".join(cfg.get("content.avoid", [])), items=lines,
            playbook=playbook_prompt(cfg, "topics"), covered=covered,
        ),
        system=SCORE_SYSTEM, temperature=0.2,
    )
    by_i = {int(r["i"]): r for r in result.get("items", []) if isinstance(r, dict) and "i" in r}
    scored: list[Candidate] = []
    for i, c in enumerate(batch):
        r = by_i.get(i)
        if not r or r.get("avoid"):
            continue
        if r.get("covered"):
            log.info("trends: skipping '%s' (repeats a covered question: %s)", c.title, r.get("angle", ""))
            continue
        c.niche_relevance = float(r.get("niche_relevance", 0))
        c.hook_potential = float(r.get("hook_potential", 0))
        c.angle = str(r.get("angle", "")).strip()
        c.search_queries = [str(q) for q in r.get("search_queries", [])][:3]
        c.score = round(0.5 * c.niche_relevance + 0.3 * c.hook_potential + 0.2 * c.popularity, 4)
        scored.append(c)
    return scored


EVERGREEN_PROMPT = """Channel niche: {niche}
Audience: {audience}
No trending data is available today. Suggest 12 evergreen explainer topics that a broad audience
would stop scrolling for: everyday mysteries, surprising mechanisms, counter-intuitive facts.
Avoid: {avoid}. Avoid these already-covered topics: {covered}

Return JSON: {{"topics": [{{"title": "<topic>", "angle": "<question the video answers>",
"hook_potential": <0–1>, "search_queries": ["<2–3 Wikipedia search terms>"]}}]}}"""


def evergreen_candidates(ctx: StageContext, ledger: Ledger) -> list[Candidate]:
    cfg = ctx.config
    covered = covered_topics(ledger)
    result = ctx.llm.complete_json(
        EVERGREEN_PROMPT.format(
            niche=cfg.get("channel.niche"), audience=cfg.get("channel.audience"),
            avoid=", ".join(cfg.get("content.avoid", [])), covered=covered,
        ),
        temperature=0.9,
    )
    out = []
    for t in result.get("topics", []):
        c = Candidate(
            title=str(t.get("title", "")), source="evergreen", sources=["evergreen"],
            niche_relevance=1.0, hook_potential=float(t.get("hook_potential", 0.5)),
            angle=str(t.get("angle", "")), search_queries=[str(q) for q in t.get("search_queries", [])][:3],
        )
        c.score = round(0.5 + 0.3 * c.hook_potential, 4)
        if c.title:
            out.append(c)
    return out


def recently_failed_titles(ctx: StageContext, days: int = 2) -> set[str]:
    """Topics that recent aborted runs couldn't turn into a video (too few sources, failed
    fact-check, rejected render). Runs on the same day see the same trends, so without this
    every new run would walk into the same dead end."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    titles: set[str] = set()
    for r in RunState.list_runs(ctx.config.runs_dir):
        if r.status != ABORTED or r.data.get("date", "") < cutoff or r.run_id == ctx.state.run_id:
            continue
        path = r.run_dir / "topics.json"
        if path.exists():
            topics = read_json(path)
            titles.update(x.get("title", "") for x in topics.get("rejected", []))
            titles.add((topics.get("selected") or {}).get("title", ""))
    titles.discard("")
    return titles


def rank(cands: list[Candidate], ledger: Ledger, min_relevance: float, dup_threshold: float,
         skip: set[str] | frozenset[str] = frozenset()) -> list[Candidate]:
    ok = []
    for c in sorted(cands, key=lambda c: -c.score):
        if c.niche_relevance < min_relevance:
            continue
        if c.title in skip:
            log.info("trends: skipping '%s' (a recent run already failed with it)", c.title)
            continue
        dup = ledger.find_duplicate(c.angle or c.title, dup_threshold) or ledger.find_duplicate(c.title, dup_threshold)
        if dup:
            log.info("trends: skipping '%s' (already covered: %s)", c.title, dup.get("topic"))
            continue
        ok.append(c)
    return ok


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    ledger = Ledger(cfg.ledger_path)
    min_rel = float(cfg.get("trends.min_niche_relevance", 0.7))
    dup_t = float(cfg.get("trends.duplicate_similarity", 0.85))

    raw = collect_all(cfg, ctx)
    cands = dedupe(raw, dup_t)
    log.info("trends: %d raw -> %d after dedupe", len(raw), len(cands))
    failed = recently_failed_titles(ctx)
    ranked = rank(score_candidates(ctx, cands, covered_topics(ledger)), ledger, min_rel, dup_t, failed) if cands else []

    if not ranked and cfg.get("trends.evergreen_fallback", True):
        log.warning("trends: no in-niche trending topic today; falling back to evergreen topics")
        ranked = rank(evergreen_candidates(ctx, ledger), ledger, min_rel, dup_t, failed)
    if not ranked:
        raise AbortRun("No in-niche, non-duplicate topic found today")

    for c in ranked[:5]:
        log.info("trends: %.2f  %s  -> %s", c.score, c.title, c.angle)
    out = ctx.path("topics.json")
    atomic_write_json(out, {
        "candidates": [asdict(c) for c in ranked],
        "selected": asdict(ranked[0]),
        "rejected": [],
    })
    return [out]
