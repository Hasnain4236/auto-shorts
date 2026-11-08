"""Stage: research — gather 3–5 facts, each backed by a source URL and a verbatim quote.

Sources are real documents fetched at runtime (Wikipedia + news articles linked from
the trend). The LLM only *extracts* facts from those documents, and every fact must
include a quote that we verify, in code, actually appears in the fetched source text.
So a fact (or URL) the model invented is dropped, never published.

If fewer than `content.min_facts` facts survive, the next candidate topic from
topics.json is tried (up to `content.max_topic_attempts`), then the run aborts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pipeline import http
from pipeline.stages import AbortRun, StageContext
from pipeline.state import atomic_write_json, read_json

log = logging.getLogger(__name__)

MAX_DOC_CHARS = 6000


@dataclass
class SourceDoc:
    id: str
    title: str
    url: str
    text: str


def fetch_wikipedia(query: str, lang: str = "en", limit: int = 2) -> list[SourceDoc]:
    api = f"https://{lang}.wikipedia.org/w/api.php"
    found = http.get_json(api, {
        "action": "query", "list": "search", "srsearch": query, "srlimit": limit,
        "format": "json", "formatversion": 2,
    })
    docs = []
    for hit in found.get("query", {}).get("search", []):
        title = hit["title"]
        page = http.get_json(api, {
            "action": "query", "prop": "extracts", "explaintext": 1, "titles": title,
            "format": "json", "formatversion": 2, "redirects": 1,
        })
        pages = page.get("query", {}).get("pages", [])
        text = (pages[0].get("extract") if pages else "") or ""
        if len(text) > 300:
            url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
            docs.append(SourceDoc(id="", title=f"Wikipedia: {title}", url=url, text=text[:MAX_DOC_CHARS]))
    return docs


def fetch_article(url: str) -> SourceDoc | None:
    if not http.is_article_url(url):
        return None
    raw = http.get_text(url)
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    text = http.html_to_text(raw)
    if len(text) < 400:
        return None
    title = " ".join(m.group(1).split()) if m else url
    return SourceDoc(id="", title=title[:120], url=url, text=text[:MAX_DOC_CHARS])


def gather_sources(candidate: dict[str, Any], lang: str) -> list[SourceDoc]:
    docs: list[SourceDoc] = []
    for url in candidate.get("related_urls", [])[:3]:
        try:
            d = fetch_article(url)
            if d:
                docs.append(d)
        except Exception as e:
            log.info("research: article fetch failed %s (%s)", url, e)
    queries = candidate.get("search_queries") or [candidate.get("angle") or candidate["title"]]
    seen = {d.url for d in docs}
    for q in queries[:3]:
        try:
            for d in fetch_wikipedia(q, lang):
                if d.url not in seen:
                    docs.append(d)
                    seen.add(d.url)
        except Exception as e:
            log.info("research: wikipedia query failed '%s' (%s)", q, e)
    for i, d in enumerate(docs[:6], start=1):
        d.id = f"S{i}"
    return docs[:6]


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    return " ".join(re.sub(r"[^a-z0-9%.' ]+", " ", s).split())


def quote_in_source(quote_text: str, source_text: str) -> bool:
    """Exact (normalised) substring, or ≥70% of the quote's 6-word shingles present."""
    q, s = _norm(quote_text), _norm(source_text)
    if len(q) < 20:
        return False
    if q in s:
        return True
    words = q.split()
    shingles = [" ".join(words[i : i + 6]) for i in range(max(1, len(words) - 5))]
    hits = sum(1 for sh in shingles if sh in s)
    return hits / len(shingles) >= 0.7


EXTRACT_SYSTEM = "You are a meticulous science researcher. You only state what the provided sources say. You return JSON."

EXTRACT_PROMPT = """Topic: {title}
Explainer angle: {angle}

Using ONLY the source documents below, extract {min_f}–{max_f} facts that together let a
45–60 s video answer the angle in a surprising, concrete way. Prefer:
- the core mechanism (how/why it works),
- one striking number or comparison,
- one counter-intuitive detail that would make a viewer say "wait, what?".

Rules:
- Each fact must be directly stated in its source. No outside knowledge.
- "quote" must be copied VERBATIM from that source (one or two sentences, 15–60 words).
- If the sources cannot support the angle, return fewer facts. Do not pad.
- Never extract recipes, ingredient ratios, quantities or procedures for making explosives,
  weapons, drugs or dangerous chemicals. Extract the underlying principle instead.

"fact" is the same information rewritten so a 14-year-old understands it (no jargon;
keep any exact number). "quote" carries the precise wording.

Return JSON: {{"facts": [{{"id": "F1", "fact": "<plain-English restatement>",
"source_id": "<S1..>", "quote": "<verbatim quote>"}}],
"supports_angle": <true|false>}}

Sources:
{sources}"""


def extract_facts(ctx: StageContext, candidate: dict[str, Any], docs: list[SourceDoc]) -> list[dict[str, Any]]:
    cfg = ctx.config
    src_block = "\n\n".join(f"[{d.id}] {d.title} ({d.url})\n{d.text}" for d in docs)
    result = ctx.llm.complete_json(
        EXTRACT_PROMPT.format(
            title=candidate["title"], angle=candidate.get("angle") or candidate["title"],
            min_f=cfg.get("content.min_facts", 3), max_f=cfg.get("content.max_facts", 5), sources=src_block,
        ),
        system=EXTRACT_SYSTEM, temperature=0.1,
    )
    by_id = {d.id: d for d in docs}
    verified: list[dict[str, Any]] = []
    for f in result.get("facts", []):
        doc = by_id.get(str(f.get("source_id")))
        quote_text = str(f.get("quote", ""))
        if not doc or not quote_in_source(quote_text, doc.text):
            log.info("research: dropped unverifiable fact: %s", str(f.get("fact"))[:100])
            continue
        verified.append({
            "id": f"F{len(verified) + 1}",
            "fact": str(f.get("fact", "")).strip(),
            "quote": quote_text.strip(),
            "source_title": doc.title,
            "source_url": doc.url,
        })
    return verified[: int(cfg.get("content.max_facts", 5))]


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    topics_path = ctx.path("topics.json")
    topics = read_json(topics_path)
    min_facts = int(cfg.get("content.min_facts", 3))
    max_attempts = int(cfg.get("content.max_topic_attempts", 3))
    lang = cfg.get("channel.language", "en")

    rejected = topics.setdefault("rejected", [])
    rejected_titles = {r["title"] for r in rejected}
    queue = [c for c in topics["candidates"] if c["title"] not in rejected_titles]

    for candidate in queue[: max(0, max_attempts - len(rejected))]:
        log.info("research: trying '%s'", candidate["title"])
        docs = gather_sources(candidate, lang)
        facts = extract_facts(ctx, candidate, docs) if docs else []
        if len(facts) >= min_facts:
            topics["selected"] = candidate
            atomic_write_json(topics_path, topics)
            out = ctx.path("facts.json")
            atomic_write_json(out, {
                "topic": candidate["title"],
                "angle": candidate.get("angle", ""),
                "facts": facts,
                "sources_considered": [{"id": d.id, "title": d.title, "url": d.url} for d in docs],
            })
            log.info("research: %d verified facts from %d sources", len(facts), len({f['source_url'] for f in facts}))
            return [out]
        reason = f"only {len(facts)} verifiable facts from {len(docs)} sources"
        log.warning("research: rejecting '%s' (%s)", candidate["title"], reason)
        rejected.append({"title": candidate["title"], "reason": reason})
        atomic_write_json(topics_path, topics)  # persisted so a resume doesn't retry it

    raise AbortRun(f"No topic could be supported by ≥{min_facts} sourced facts (tried {len(rejected)})")
