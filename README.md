# auto-shorts

Zero-cost pipeline: trending topic → fact-checked script → 9:16 Short → YouTube upload →
Telegram review → analytics-driven learning loop.
See [AUTO_SHORTS_BUILD_SPEC.md](AUTO_SHORTS_BUILD_SPEC.md) for the full brief.

## Status

| Phase | Stages | Status |
|---|---|---|
| 1 Skeleton | config, state/resume, llm wrapper, CLI | ✅ |
| 2 Content | trends, research, script, factcheck | ✅ tested live |
| 3 Media | tts, footage, captions, compose, qa | ✅ tested live (add music to `assets/music/`) |
| 4 Distribution | upload, notify, ledger, learn | ✅ live: uploads go public directly |
| 5 Automation | GitHub Actions, Colab | ✅ running: 8 Shorts a day on GitHub Actions |

## How it works

```
learn (daily)  ─► data/playbook.json ─┐
                                      ▼
trends → research → script → factcheck → tts → footage → captions → compose → qa → upload → notify
 (topic)  (sourced   (Nemotron   (Groq/Gemini            (vision-picked                (YouTube)  (Telegram:
          facts)     writes)     checks)                  stock clips)                            video + script)
```

Different free models handle different jobs (`llm.roles` in `config.yaml`). The writer and the
fact-checker are deliberately different model families, so the checker catches what the writer
invents.

## Quick start (local, Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then fill in keys (see Setup)
.\.venv\Scripts\python scripts\check_keys.py
.\.venv\Scripts\python main.py run --dry-run      # everything except upload
```

## CLI

```
python main.py run [--dry-run]                 # make one video (resumes today's unfinished run first)
python main.py resume <run_id> [--force]       # continue a specific run (--force retries an aborted one)
python main.py stage <name> [--run-id ID] [--force]
python main.py learn [--force]                 # pull YouTube analytics, update data/playbook.json
python main.py list                            # ✓ done  ✗ failed  … running  ! aborted  · pending
```

Every stage writes to `runs/<date>_<slug>/` and records itself in `state.json`. Rerunning skips
finished stages. Delete an output (or `stage <name> --force`) to redo one. Dry runs never upload
and are never resumed by a real run. `RUNS_DIR` moves the runs folder (e.g. to Google Drive in Colab).

## Setup

### 1. YouTube upload + analytics (one time, ~10 minutes)

1. https://console.cloud.google.com → create a project (or use the one your API key is in).
2. **APIs & Services → Library**: enable **YouTube Data API v3** and **YouTube Analytics API**.
3. **OAuth consent screen**: User type *External*, fill in app name + your email. Add yourself
   under *Test users*. Then click **Publish app** ("In production").
   ⚠️ If you leave it in *Testing*, Google expires the refresh token after **7 days** and uploads
   silently stop. You don't need Google's verification for your own account; you'll just see an
   "unverified app" warning when signing in.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**. Put the client ID and
   secret in `.env` as `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET`.
5. Run `.\.venv\Scripts\python scripts\get_youtube_token.py`, sign in with the **channel's**
   Google account, and allow access. It writes `YOUTUBE_REFRESH_TOKEN` into `.env`.

**Publishing (`upload.publish_mode` in `config.yaml`):**
- `public` (default): goes public the moment it's uploaded, and Telegram gets the same video
  with its link a few seconds later. To take one down: Studio → Visibility → Private.
- `scheduled`: the video is uploaded privately with a go-live time (`publish_time`,
  19:30 IST), and YouTube makes it public then. This gives you a window to stop it first.
- `private`: you publish everything yourself.

YouTube can restrict API uploads from unaudited projects to private. That didn't happen
on this channel: the first upload could be made public. If it ever does, the Telegram
review says so, and `learn` alerts you about any scheduled video that didn't go live. The fix
is the free **YouTube API compliance audit**
(https://support.google.com/youtube/contact/yt_api_form).

### 2. Telegram review (2 minutes)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}` into `TELEGRAM_CHAT_ID`.

Each run sends you the video plus title, script and sources. Failed or aborted runs send an alert.

### 3. Music

`assets/music/` ships with 5 tracks by Kevin MacLeod (incompetech.com, CC BY 4.0). The required
credit line is added to each video's description automatically from `assets/music/tracks.json`.
Every track is loudness-matched and ducked about 15 dB under the voice. To add your own (e.g.
from YouTube Studio → Audio Library), drop the file in. Add an entry to `tracks.json` only if
the licence needs a credit.

### 4. Run daily on GitHub Actions (no PC needed)

One command, from the project folder, after the first local commit:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_github.ps1
```

It creates a **private** repo and pushes the project (`.env` is git-ignored). It copies your
`.env` keys into GitHub's encrypted secrets and starts one dry run. Nothing public is created.
(The live repo was later made public for unlimited Actions minutes. Secrets stay encrypted,
and the workflow only runs on its schedule or by hand, never for other people's pull requests.)
After that, `.github/workflows/daily.yml` makes one Short per slot, every 2 hours
from 08:17 to 22:17 IST (8 a day), and each goes public as soon as it's uploaded:

`learn` (first slot of the day only) → `run` (make + upload one video) → prune → cache runs →
commit the ledger.

How many a day: `channel.videos_per_day` in `config.yaml` is the cap, and the `cron:` line sets
the slots. Change both together. The slots are spread out on purpose: back to back, the day's
videos would land within an hour or two and compete for the same first viewers.

A failed run resumes from the failed stage in the next slot and sends a Telegram alert. A run
that aborts (no topic survives research or fact-checking, or QA rejects the render) gets one
fresh retry with a different topic. Each video is also kept as a downloadable artifact for
3 days. Run one extra on demand: GitHub → Actions → Daily Short → Run workflow (still capped).

After a new YouTube sign-in, update the secret with
`.\.venv\Scripts\python scripts\push_secrets.py <you>/auto-shorts`.

### 5. Publish the Google consent screen (or the YouTube sign-in expires every 7 days)

Google needs a public homepage and privacy policy for the app, and proof that you own the site.

1. **Search Console** (https://search.google.com/search-console), signed in with the Google account
   that owns the Cloud project: *Add property → URL prefix* → `https://<you>.github.io/` →
   *HTML tag* → copy the `content="…"` code. Don't click Verify yet.
2. Publish the pages (homepage, privacy policy, terms) to `https://<you>.github.io/`:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\publish_site.ps1 -ContactEmail you@gmail.com -GoogleVerification <code>
   ```
3. After about 2 minutes, click **Verify** in Search Console.
4. **Google Auth Platform → Branding**: set *App name* to exactly `CrazyFindings` (it must match the
   homepage). Paste the home page, privacy policy and terms links the script printed, add
   `<you>.github.io` under *Authorised domains*, and save.
5. **Audience → Publish app.** An unverified app in production works fine for your own account
   (you just see an "unverified app" warning when signing in), and its sign-in no longer expires.
   Google's full verification is optional: it only removes that warning. Wait 24 h after step 3
   before requesting it.
6. Sign in once more so the long-lived token is issued, then send it to GitHub:
   `.\.venv\Scripts\python scripts\get_youtube_token.py` →
   `.\.venv\Scripts\python scripts\push_secrets.py <you>/auto-shorts`.

### 6. Or run it in Google Colab

```python
from google.colab import drive, userdata
drive.mount("/content/drive")
!git clone https://<github-token>@github.com/<you>/auto-shorts.git   # private repo: use a token
%cd auto-shorts
!apt-get -qq install -y ffmpeg && pip -q install -r requirements.txt
import os
for k in ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "YOUTUBE_API_KEY", "PIXABAY_API_KEY",
          "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
          "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
    os.environ[k] = userdata.get(k)          # add these in Colab's 🔑 Secrets panel
os.environ["RUNS_DIR"] = "/content/drive/MyDrive/auto-shorts/runs"   # resumes across sessions
!python main.py run
```

## The learning loop

`python main.py learn` (run daily, before `run`):
1. Links each ledger entry to its **public** video (the API upload, or your manual re-upload).
2. Pulls views, average % viewed, likes, shares, subscribers and the **retention curve** for
   videos ≥ 3 days old (YouTube Analytics API, free).
3. Scores videos against each other. Retention counts most, then reach, then how well the
   hook holds viewers.
4. Updates experiment stats. Each video tests one hook style (counterintuitive / stakes /
   question). 70% of videos use the best-performing style and 30% keep exploring.
5. After 6+ public videos, an LLM compares the best and worst videos and writes
   `data/playbook.json` (do / avoid / hook / pacing / topic guidance with a confidence level).
   The topic picker and script writer follow it on every later run.

Shorts performance is noisy. Treat the playbook as a hint until there are ~20 videos.

## Free-tier limits (as of 2026-11)

| Service | Limit | Use per video |
|---|---|---|
| GitHub Actions | unlimited on a public repo (2,000 min/month if private) | ~10–15 min |
| Gemini key: Flash models | ~20 requests/day **per model** | ~5–8 |
| Gemini key: Gemma 4 | much larger free allowance, separate from Flash | overflow |
| OpenRouter free models (Nemotron writes) | ~50 requests/day **per account**, shared by all free models | 1–4 (writer) |
| Groq (gpt-oss-120b, gpt-oss-20b, Qwen 3.8) | 1,000 requests/day **per model**, 8,000 tokens/min | 2–3 (checker) |
| YouTube Data API | uploads: own bucket of 100/day (1 unit each); 10,000 units/day for the rest | one upload + a few reads |
| Pixabay | 100 requests/minute | ~20 |

Models that hit a daily limit are skipped automatically until the quota resets.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
```
