"""Copy the keys from .env into GitHub Actions secrets (values are never printed).

    python scripts/push_secrets.py OWNER/REPO

Needs the GitHub CLI (`gh`) to be logged in. Re-run it whenever a key in .env changes,
e.g. after a new YouTube sign-in.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

NAMES = [
    "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
    "YOUTUBE_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY",
    "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = sys.argv[1]
    values = dotenv_values(ROOT / ".env")
    failed = 0
    for name in NAMES:
        value = (values.get(name) or "").strip()
        if not value or "YOUR_REDDIT_USERNAME" in value:
            print(f"  skip  {name} (not set in .env)")
            continue
        r = subprocess.run(["gh", "secret", "set", name, "--repo", repo], input=value, text=True,
                           capture_output=True)
        ok = r.returncode == 0
        failed += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f": {r.stderr.strip()}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
