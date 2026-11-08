"""Check which API keys in .env work. Prints status only, never key values.

    python scripts/check_keys.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from pipeline.config import load_config  # noqa: E402

T = 20


def check_gemini(cfg) -> str:
    from google import genai

    model = next(p["model"] for p in cfg.get("llm.providers") if p["name"] == "gemini")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    names = [m.name for m in client.models.list()]
    flash = sorted(n.removeprefix("models/") for n in names if "flash" in n)
    r = client.models.generate_content(model=model, contents="Reply with the single word OK")
    return f"OK ({model} replied {r.text.strip()!r}); flash models available: {', '.join(flash[:8])}"


def check_openai_compat(base: str, key_env: str, model: str) -> str:
    r = requests.post(f"{base}/chat/completions", timeout=T,
                      headers={"Authorization": f"Bearer {os.environ[key_env]}"},
                      json={"model": model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 5})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return f"OK ({model})"


def check_youtube_key() -> str:
    r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=T, params={
        "part": "snippet", "chart": "mostPopular", "regionCode": "IN", "maxResults": 1,
        "key": os.environ["YOUTUBE_API_KEY"]})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.json().get('error', {}).get('message', r.text[:200])}")
    return "OK (trending list readable, 1 quota unit used)"


def check_pixabay() -> str:
    r = requests.get("https://pixabay.com/api/videos/", timeout=T,
                     params={"key": os.environ["PIXABAY_API_KEY"], "q": "earth", "per_page": 3})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return f"OK ({r.json().get('totalHits', 0)} hits for 'earth')"


def check_pexels() -> str:
    r = requests.get("https://api.pexels.com/videos/search", timeout=T,
                     headers={"Authorization": os.environ["PEXELS_API_KEY"]}, params={"query": "earth", "per_page": 1})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return "OK"


def check_telegram() -> str:
    r = requests.get(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/getMe", timeout=T)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return f"OK (bot @{r.json()['result']['username']})"


def main() -> int:
    cfg = load_config()
    providers = {p["name"]: p for p in cfg.get("llm.providers")}
    checks = [
        ("GEMINI_API_KEY", lambda: check_gemini(cfg)),
        ("GROQ_API_KEY", lambda: check_openai_compat(providers["groq"]["base_url"], "GROQ_API_KEY", providers["groq"]["model"])),
        ("OPENROUTER_API_KEY", lambda: check_openai_compat(providers["openrouter"]["base_url"], "OPENROUTER_API_KEY", providers["openrouter"]["model"])),
        ("YOUTUBE_API_KEY", check_youtube_key),
        ("PEXELS_API_KEY", check_pexels),
        ("PIXABAY_API_KEY", check_pixabay),
        ("TELEGRAM_BOT_TOKEN", check_telegram),
    ]
    for name, fn in checks:
        if not os.environ.get(name, "").strip():
            print(f"  --  {name:<22} not set")
            continue
        try:
            print(f"  OK  {name:<22} {fn()}")
        except Exception as e:
            print(f"  XX  {name:<22} {type(e).__name__}: {str(e)[:250]}")
    for name in ["REDDIT_CLIENT_ID", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "TELEGRAM_CHAT_ID"]:
        print(f"  {'set' if os.environ.get(name, '').strip() else '-- '} {name:<22} (not tested here)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
