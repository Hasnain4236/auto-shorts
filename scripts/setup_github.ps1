# One-time GitHub setup for auto-shorts. Run from the project folder:
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_github.ps1
#
# What it does (nothing else):
#   1. creates a PRIVATE repo <you>/auto-shorts and pushes this project (.env is git-ignored)
#   2. copies the keys from .env into that repo's encrypted Actions secrets (values never printed)
#   3. creates a small PUBLIC repo <you>/crazyfindings-site with only docs/*.html (homepage,
#      privacy policy, terms) and turns on GitHub Pages, for Google's OAuth consent screen
#   4. starts one DRY-RUN of the daily workflow (makes a video, sends it to Telegram, no upload)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$owner = gh api user --jq .login
$ghId = gh api user --jq .id
$noreply = "$ghId+$owner@users.noreply.github.com"
Write-Host "GitHub account: $owner"

# 1. private code repo
if (-not (git log -1 --oneline 2>$null)) { throw "No commit yet: commit the project first." }
gh repo create "$owner/auto-shorts" --private --source=. --remote=origin --push `
    --description "Zero-cost automated science Shorts pipeline for CrazyFindings"

# 2. secrets
.\.venv\Scripts\python scripts\push_secrets.py "$owner/auto-shorts"

# 3. public policy pages
$site = Join-Path $env:TEMP "crazyfindings-site"
if (Test-Path $site) { Remove-Item -Recurse -Force $site }
New-Item -ItemType Directory $site | Out-Null
Copy-Item docs\*.html $site
Push-Location $site
git init -q -b main
git add .
git -c user.name="$owner" -c user.email="$noreply" commit -q -m "Homepage, privacy policy and terms"
gh repo create "$owner/crazyfindings-site" --public --source=. --remote=origin --push `
    --description "Homepage and privacy policy for the CrazyFindings uploader"
gh api -X POST "repos/$owner/crazyfindings-site/pages" -f "source[branch]=main" -f "source[path]=/" | Out-Null
Pop-Location

# 4. test the workflow once without uploading
gh workflow run daily.yml --repo "$owner/auto-shorts" -f dry_run=true

$base = "https://$($owner.ToLower()).github.io/crazyfindings-site"
Write-Host ""
Write-Host "Done. Workflow: https://github.com/$owner/auto-shorts/actions"
Write-Host "Consent-screen links (live in ~1 minute):"
Write-Host "  Home page:      $base/"
Write-Host "  Privacy policy: $base/privacy.html"
Write-Host "  Terms:          $base/terms.html"
Write-Host "  Authorised domain: $($owner.ToLower()).github.io"
