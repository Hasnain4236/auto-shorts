"""Shared HTTP helpers: one session, polite User-Agent, timeouts, HTML -> text."""
from __future__ import annotations

import html
import logging
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# Wikimedia (and other APIs) throttle anonymous bots, especially from shared cloud IPs like
# GitHub Actions. Their policy asks for "<name>/<version> (<contact URL or email>)", so the
# bot identifies itself with the app's public homepage, which lists a contact address.
USER_AGENT = os.environ.get("HTTP_USER_AGENT") or \
    "CrazyFindingsBot/0.1 (https://hasnain4236.github.io/) python-requests"
TIMEOUT = 20
MAX_BYTES = 2_000_000
RETRY_STATUS = {429, 503}
MAX_RETRIES = 3
MAX_WAIT_S = 30.0

_session: requests.Session | None = None
_sleep: Callable[[float], None] = time.sleep


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = USER_AGENT
    return _session


def _retry_wait(r: requests.Response, attempt: int) -> float:
    try:
        wait = float(r.headers.get("retry-after", ""))
    except ValueError:  # missing, or an HTTP date
        wait = 2.0 ** (attempt + 1)
    return min(MAX_WAIT_S, max(1.0, wait))


def _get(url: str, params: dict[str, Any] | None = None, stream: bool = False) -> requests.Response:
    """GET that waits and retries when a server says "slow down" (429/503)."""
    for attempt in range(MAX_RETRIES + 1):
        r = session().get(url, params=params, timeout=TIMEOUT, stream=stream)
        if r.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            wait = _retry_wait(r, attempt)
            log.info("http: %s from %s; retrying in %.0fs", r.status_code, urlparse(url).netloc, wait)
            r.close()
            _sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise AssertionError("unreachable")


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    return _get(url, params).json()


def get_text(url: str, params: dict[str, Any] | None = None) -> str:
    r = _get(url, params, stream=True)
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
