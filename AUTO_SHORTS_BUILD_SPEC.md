# Build Spec: Zero-Cost Automated Shorts Pipeline

> **For Claude (Claude Code):** This is the full brief for a project you are building. Read it fully before writing code. Build in the phases below, one phase at a time. After each phase, stop, summarise what was built and how to test it, and wait for approval. Never invent API behaviour. If you are unsure about a current API limit or endpoint, say so and check the official docs.

---

## 1. Goal

A pipeline that runs on a daily schedule and does the following:

1. Finds trending topics (YouTube trending, Google Trends, Reddit).
2. Filters them to **one fixed niche** set in config.
3. Writes an original, fact-checked, informative script for a 45–60 s short.
4. Produces the video: voiceover, stock footage and text cards, word-timed captions, background music, 9:16 at 1080×1920.
5. Uploads it to YouTube as **private**, and optionally to Instagram Reels.
6. Sends me a review message (Telegram) with a preview link, the title and the script, so I can approve it and make it public.

## 2. Hard constraints (do not violate)

- **₹0 running cost.** Only free tiers and open-source tools. No paid APIs, no paid hosting, no trial credits that later bill.
- **Claude is not used at runtime.** I only have Claude Pro (chat and Claude Code) and no API access. The runtime LLM is the **Gemini API free tier**, called through a provider-agnostic `llm.py` wrapper so it can be swapped for Groq or OpenRouter free models later.
- **No scraping that breaks platform ToS.** No Instagram scraping and no logged-in browser automation for uploads. Use official APIs only.
- **YouTube uploads go up as `private`.** Unaudited API projects are forced to private anyway. Publishing is a human step, or happens through the API after the project passes the YouTube API compliance audit (keep this behind a config flag).
- **Originality.** Never reuse or re-cut other creators' videos or audio. Trends supply the *topic* only. Scripts must add real explanation, not reword the source. This keeps the channel clear of YouTube's "inauthentic / mass-produced content" policy.
- **Idempotent and resumable.** Every stage writes its output to `runs/<date>_<slug>/` together with a `state.json`. A rerun skips completed stages and never re-renders or re-uploads finished work. Before uploading, check a ledger of uploaded topics to avoid duplicates.
- **Secrets** live only in environment variables or GitHub Secrets. Never commit them. Ship a `.env.example`.

## 3. Configuration (`config.yaml`)

Fill in my answers. Leave TODO markers where I haven't decided yet.

```yaml
channel:
  niche: "TODO e.g. science & tech explained simply"
  language: "en"            # en | hi | te ...
  audience: "TODO"
  tone: "curious, clear, no hype"
  videos_per_day: 1
trends:
  youtube_region: "IN"
  youtube_category_ids: []   # optional filter
  google_trends_geo: "IN"
  subreddits: ["science", "technology", "explainlikeimfive"]
  min_niche_relevance: 0.7   # LLM-scored 0–1
video:
  duration_s: [45, 60]
  resolution: [1080, 1920]
  fps: 30
  voice: "en-IN-NeerjaNeural"   # edge-tts voice
  music_dir: "assets/music"     # royalty-free tracks I add
  font: "assets/fonts/Inter-Bold.ttf"
upload:
  youtube: true
  instagram: false
  youtube_privacy: "private"
  auto_publish_after_audit: false
review:
  telegram: true
```

## 4. Tech stack

| Stage | Tool |
|---|---|
| Language | Python 3.11 |
| Trends | YouTube Data API v3 (`videos.list chart=mostPopular`), `pytrends` (unofficial, may break, so wrap it in try/except), Reddit API via `praw` |
| LLM | `google-genai` SDK, Gemini Flash / Flash-Lite free tier, with retry and backoff on 429 |
| TTS | `edge-tts`, falling back to `kokoro` or `piper` (offline) |
| Footage | Pexels API and Pixabay API (free keys), with attribution stored in metadata |
| Captions | `faster-whisper` (CPU, `small` or `base` model) for word timestamps |
| Editing | FFmpeg as primary, `moviepy` only where it helps; Pillow for text cards and thumbnails |
| Upload | `google-api-python-client` (OAuth refresh token), Instagram Graph API (Business account) |
| Review | Telegram Bot API (`python-telegram-bot` or plain HTTP) |
| Scheduler | GitHub Actions cron (primary); also runnable from Colab or locally |

## 5. Repo layout

```
auto-shorts/
├── config.yaml
├── .env.example
├── requirements.txt
├── README.md                 # setup guide (see §8)
├── main.py                   # CLI: python main.py run | stage <name> | resume <run_id>
├── pipeline/
│   ├── state.py              # run dir, state.json, stage skip logic, uploaded ledger
│   ├── llm.py                # provider-agnostic LLM wrapper + JSON-mode + retries
│   ├── trends.py             # collect → dedupe → score niche relevance → pick topic
│   ├── research.py           # gather 3–5 supporting facts/sources for the topic
│   ├── script.py             # hook, body, CTA; on-screen text beats; title/desc/tags
│   ├── factcheck.py          # 2nd LLM pass: flag unsupported claims, rewrite or abort
│   ├── tts.py
│   ├── footage.py            # keyword per beat → Pexels/Pixabay search → download/cache
│   ├── captions.py           # whisper word timings → styled ASS subtitles
│   ├── compose.py            # FFmpeg assembly, 9:16 crop, ducking music, burn subs
│   ├── thumbnail.py
│   ├── upload_youtube.py
│   ├── upload_instagram.py
│   └── notify.py             # Telegram review message
├── assets/ (fonts, music, brand)
├── runs/ (gitignored, except ledger)
├── data/uploaded_ledger.json
└── .github/workflows/daily.yml
```

## 6. Stage contracts

Each stage is a pure function: `run(run_dir, config) -> output file(s)`. It updates `state.json` with `{stage: "done", outputs: [...], ts}`.

1. **trends**: writes `topics.json` with candidates `{title, source, url, score, niche_relevance}`. Pick the top candidate not already in the ledger (fuzzy match, similarity > 0.85 counts as duplicate).
2. **research**: writes `facts.json`, 3–5 facts, each with a source URL. If fewer than 3 facts can be supported, skip the topic and try the next one.
3. **script**: writes `script.json` with `hook` (≤ 2 s spoken), `beats[]` (each with `narration`, `on_screen_text`, `visual_keywords`), `cta`, `title` (≤ 70 chars, no clickbait lies), `description` (with sources), `tags`. Target 130–160 spoken words.
4. **factcheck**: writes `factcheck.json`. Every claim maps to a fact in `facts.json`. Unsupported claims trigger a rewrite. After 2 failed rewrites, abort the run and notify me.
5. **tts**: writes `voice.mp3`, normalised to -16 LUFS.
6. **footage**: writes `clips/` and `footage.json`. One clip per beat, portrait preferred, otherwise crop to centre. Fall back to an animated text card if nothing is found.
7. **captions**: writes `captions.ass`, word-level highlighted, max 3–4 words per line, inside a safe zone (avoid the bottom 20% where the UI overlays).
8. **compose**: writes `final.mp4` (H.264, `-preset veryfast`, CRF 23, AAC 128k) and `thumb.jpg`. Cuts align to beat timings. Music is ducked under the voice.
9. **qa**: automated checks on duration, resolution, audio present, no silent gaps > 1.5 s, file size. A failure stops the run.
10. **upload**: writes `upload.json` with the video ID(s) and appends to the ledger. Mark the YouTube upload as AI-assisted where the API supports it. Set `containsSyntheticMedia` if available, and check the current API field first.
11. **notify**: sends a Telegram message with the title, script, sources and the YouTube Studio link.

## 7. Scheduling and runtime

- `.github/workflows/daily.yml`: cron once a day (offset from :00, e.g. `17 3 * * *` UTC). Install FFmpeg with apt, cache the pip and whisper models, run `python main.py run`, then upload `runs/<id>/` as a workflow artifact and commit only `data/uploaded_ledger.json`.
- Must also work in **Colab**: `git pull` then `python main.py run`, with an optional `RUNS_DIR` env var pointing to Google Drive so that runs checkpoint and resume across sessions.
- Respect free quotas: the YouTube Data API daily quota, Gemini RPM/RPD (sleep and back off), and the Instagram publishing limit. Log the quota use for each run.

## 8. README setup guide (write this for a non-expert)

Step-by-step, with what to click:
1. Google Cloud project → enable YouTube Data API v3 → OAuth consent screen → Desktop OAuth client → run `scripts/get_youtube_token.py` locally once to get the refresh token. Add a note on applying for the **YouTube API compliance audit** so uploads can be public.
2. Gemini API key from Google AI Studio.
3. Pexels and Pixabay API keys.
4. Telegram: create a bot with BotFather, get the chat ID.
5. (Optional) Instagram: convert to a Business account, link a Facebook Page, create a Meta app, get the `instagram_business_content_publish` permission, get a long-lived token. Videos must be at a public URL for the Graph API, so document a free way to host them temporarily (e.g. a GitHub release asset or a temporary public file host) and say which one you chose.
6. Add all keys as GitHub Secrets, then trigger the workflow manually once.

## 9. Build phases (stop after each one)

1. **Skeleton**: repo layout, config loader, `state.py` with resume logic, `llm.py`, CLI, `.env.example`. Test: a dry-run that creates a run dir and resumes correctly.
2. **Content**: trends → research → script → factcheck. Test: produces a good `script.json` for a real trending topic in the niche.
3. **Media**: tts → footage → captions → compose → qa. Test: `final.mp4` plays correctly with readable captions.
4. **Distribution**: YouTube upload (private), Telegram notify, ledger. Test: one real private upload.
5. **Automation**: GitHub Actions workflow plus the Colab notebook cell. Test: a manual workflow dispatch succeeds end to end.
6. **(Optional) Instagram** and the auto-publish flag for after the audit.

## 10. Quality bar

- Hook in the first 2 s. No filler like "In this video we will…".
- Every factual claim is traceable to a source listed in the description.
- No copyrighted music. Only tracks I put in `assets/music` (YouTube Audio Library or similar).
- No faces of real people from stock footage implied to be someone specific.
- Readable code: type hints, logging (not prints), one module per stage, and a `--dry-run` that skips uploads.

## 11. Things to tell me plainly if they come up

- A free tier limit or ToS rule that changed and breaks a stage.
- Anything that can't be done at ₹0. Say so instead of quietly adding a paid service.
- Any step that still needs me (OAuth consent, audit, approving videos).
