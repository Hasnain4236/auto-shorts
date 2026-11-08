"""One-time: sign in with the YouTube channel's Google account and save a refresh token.

    python scripts/get_youtube_token.py

Opens a browser for Google sign-in, then writes YOUTUBE_REFRESH_TOKEN into .env.
Scopes requested (one consent covers upload + reading stats for the learning loop):
  * youtube              upload videos, change privacy (needed to publish after the audit)
  * yt-analytics.readonly  per-video views, watch time, retention
Needs YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET (a "Desktop app" OAuth client) in .env.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from pipeline.config import env  # noqa: E402

PORT = 8080

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def main() -> int:
    load_dotenv(ROOT / ".env")
    client = {
        "installed": {
            "client_id": env("YOUTUBE_CLIENT_ID"),
            "client_secret": env("YOUTUBE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client, SCOPES)
    print("A browser window will open. Sign in with the Google account that owns the channel.")
    print("If Google says 'app not verified', click Advanced -> Go to <app name> (it's your own app).")
    # Fixed port so a "Web application" OAuth client also works: add exactly
    # http://localhost:8080/ to its Authorized redirect URIs. ("Desktop app" clients need nothing.)
    print(f"Redirect URI in use: http://localhost:{PORT}/")
    creds = flow.run_local_server(port=PORT, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("No refresh token returned. Remove the app at https://myaccount.google.com/permissions and retry.")
        return 1

    env_path = ROOT / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    line = f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}"
    text = re.sub(r"^YOUTUBE_REFRESH_TOKEN=.*$", line, text, flags=re.M) if "YOUTUBE_REFRESH_TOKEN=" in text \
        else text.rstrip("\n") + "\n" + line + "\n"
    env_path.write_text(text, encoding="utf-8")
    print("Saved YOUTUBE_REFRESH_TOKEN to .env (also add it to GitHub Secrets for the scheduled run).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
