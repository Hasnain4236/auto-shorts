"""Authenticated YouTube Data + Analytics API clients from the stored refresh token."""
from __future__ import annotations

from typing import Any

from pipeline.config import env

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def credentials() -> Any:
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"),
        scopes=SCOPES,
    )


def data_api() -> Any:
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=credentials(), cache_discovery=False)


def analytics_api() -> Any:
    from googleapiclient.discovery import build

    return build("youtubeAnalytics", "v2", credentials=credentials(), cache_discovery=False)
