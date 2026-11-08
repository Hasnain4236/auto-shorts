# One-time: put the project on GitHub (PRIVATE repo) and switch on the daily run.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_github.ps1
#
# What it does (nothing public):
#   1. creates a PRIVATE repo <you>/auto-shorts and pushes this project (.env is git-ignored),
#      or pushes the latest commits if the repo already exists
#   2. copies the keys from .env into that repo's encrypted Actions secrets (values never printed)
#   3. starts one DRY RUN of the daily workflow (makes a video, sends it to Telegram, no upload)
# The public policy pages are a separate step: scripts\publish_site.ps1

function Fail($msg) { Write-Host "FAILED: $msg" -ForegroundColor Red; exit 1 }

Set-Location (Join-Path $PSScriptRoot "..")
$owner = gh api user --jq .login
if ($LASTEXITCODE -ne 0 -or -not $owner) { Fail "GitHub CLI is not logged in (run: gh auth login)" }
$repo = "$owner/auto-shorts"

git log -1 --oneline *> $null
if ($LASTEXITCODE -ne 0) { Fail "this folder has no git commit yet" }

gh repo view $repo --json name *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "$repo exists: pushing the latest commits"
    if (-not (git remote)) { git remote add origin "https://github.com/$repo.git" }
    git push -u origin main
    if ($LASTEXITCODE -ne 0) { Fail "git push" }
} else {
    gh repo create $repo --private --source=. --remote=origin --push `
        --description "Zero-cost automated science Shorts pipeline for CrazyFindings"
    if ($LASTEXITCODE -ne 0) { Fail "could not create $repo" }
}

.\.venv\Scripts\python scripts\push_secrets.py $repo
if ($LASTEXITCODE -ne 0) { Fail "setting the Actions secrets" }

# GitHub needs a few seconds to register a newly pushed workflow.
$started = $false
for ($i = 0; $i -lt 8 -and -not $started; $i++) {
    Start-Sleep -Seconds 5
    gh workflow run daily.yml --repo $repo -f dry_run=true *> $null
    $started = ($LASTEXITCODE -eq 0)
}
if (-not $started) { Fail "could not start the test run: start it from GitHub > Actions > Daily Short > Run workflow" }

Write-Host ""
Write-Host "Done. The daily run is switched on (14:47 IST)." -ForegroundColor Green
Write-Host "Test run (dry, no upload) started: https://github.com/$repo/actions"
