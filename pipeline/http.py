"""Shared HTTP helpers: one session, polite User-Agent, timeouts, HTML -> text."""
from __future__ import annotations

import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

USER_AGENT = "auto-shorts/0.1 (open-source educational shorts pipeline)"
TIMEOUT = 20
MAX_BYTES = 2_000_000

_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = USER_AGENT
    return _session


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    r = session().get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_text(url: str, params: dict[str, Any] | None = None) -> str:
    r = session().get(url, params=params, timeout=TIMEOUT, stream=True)
    r.raise_for_status()
    content = r.raw.read(MAX_BYTES, decode_content=True)
    r.close()
    return content.decode(r.encoding or "utf-8", errors="replace")


_DROP_BLOCKS = re.compile(r"<(script|style|noscript|svg|nav|footer|header|aside|form)\b.*?</\1>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_BLOCK_TAGS = re.compile(r"</?(p|div|br|li|h[1-6]|tr|section|article)\b[^>]*>", re.I)


def html_to_text(raw_html: str) -> str:
    """Crude but dependency-free article text extraction."""
    text = _DROP_BLOCKS.sub(" ", raw_html)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    # keep sentence-like lines; drop menus/buttons
    return "\n".join(line for line in lines if len(line) > 60)


# Platforms we never treat as fact sources (they are other creators' content or need login).
_NON_SOURCE_DOMAINS = (
    "youtube.com", "youtu.be", "reddit.com", "redd.it", "instagram.com", "tiktok.com",
    "twitter.com", "x.com", "facebook.com", "imgur.com", "v.redd.it", "i.redd.it",
)


def is_article_url(url: str | None) -> bool:
    if not url:
        return False
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return False
    host = p.netloc.lower()
    if any(host == d or host.endswith("." + d) for d in _NON_SOURCE_DOMAINS):
        return False
    return not re.search(r"\.(jpg|jpeg|png|gif|mp4|webm|pdf)$", p.path, re.I)
