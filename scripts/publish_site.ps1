# Publish the app's homepage, privacy policy and terms at https://<you>.github.io/
# (a public GitHub Pages "user site"). Google's OAuth consent screen and the YouTube API
# audit need these pages.
#
#   powershell -ExecutionPolicy Bypass -File scripts\publish_site.ps1 -ContactEmail you@gmail.com
#
# Optional:
#   -GoogleVerification <code>   Google Search Console "HTML tag" code (the content="..." value,
#                                or paste the whole <meta ...> tag); proves you own the site
#   -AppName "CrazyFindings"     must match the App name on the OAuth consent screen exactly
#
# Safe to re-run: it updates the site in place, and it keeps an existing Search Console code
# if you don't pass a new one.

param(
    [string]$ContactEmail = "",
    [string]$AppName = "CrazyFindings",
    [string]$GoogleVerification = ""
)

function Fail($msg) { Write-Host "FAILED: $msg" -ForegroundColor Red; exit 1 }

$owner = gh api user --jq .login
if ($LASTEXITCODE -ne 0 -or -not $owner) { Fail "GitHub CLI is not logged in (run: gh auth login)" }
$ghId = gh api user --jq .id
$siteHost = "$($owner.ToLower()).github.io"
$repo = "$owner/$siteHost"

if (-not $ContactEmail) { $ContactEmail = Read-Host "Contact email to show on the privacy policy" }
if ($ContactEmail -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { Fail "'$ContactEmail' is not an email address" }
if ($GoogleVerification -match 'content="([^"]+)"') { $GoogleVerification = $Matches[1] }

$work = Join-Path $env:TEMP "user-site-publish"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }

gh repo view $repo --json name *> $null
$exists = ($LASTEXITCODE -eq 0)
if ($exists) {
    gh repo clone $repo $work -- -q
    if ($LASTEXITCODE -ne 0) { Fail "could not clone $repo" }
    $old = Join-Path $work "index.html"
    if (-not $GoogleVerification -and (Test-Path $old)) {
        if ((Get-Content $old -Raw) -match 'google-site-verification" content="([^"]+)"') { $GoogleVerification = $Matches[1] }
    }
} else {
    New-Item -ItemType Directory $work | Out-Null
    git -C $work init -q -b main
}

$meta = ""
if ($GoogleVerification) { $meta = "<meta name=""google-site-verification"" content=""$GoogleVerification"">" }
$utf8 = New-Object System.Text.UTF8Encoding($false)
foreach ($f in "index.html", "privacy.html", "terms.html") {
    $html = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "..\docs\$f"), $utf8)
    $html = $html.Replace("__APP_NAME__", $AppName).Replace("__CONTACT_EMAIL__", $ContactEmail)
    $html = $html.Replace("<!--GOOGLE_VERIFICATION-->", $meta)
    [IO.File]::WriteAllText((Join-Path $work $f), $html, $utf8)
}

git -C $work add -A
$changes = git -C $work status --porcelain
if ($changes) {
    git -C $work -c user.name="$owner" -c user.email="$ghId+$owner@users.noreply.github.com" commit -q -m "Update homepage, privacy policy and terms"
    if ($LASTEXITCODE -ne 0) { Fail "git commit" }
}
if ($exists) {
    if ($changes) { git -C $work push -q; if ($LASTEXITCODE -ne 0) { Fail "git push" } }
    else { Write-Host "Site already up to date." }
} else {
    gh repo create $repo --public --source $work --remote origin --push --description "Homepage and privacy policy for the $AppName app"
    if ($LASTEXITCODE -ne 0) { Fail "could not create $repo" }
}
# User sites usually publish automatically; make sure Pages is on (an error here just means it already is).
gh api -X POST "repos/$repo/pages" -f "source[branch]=main" -f "source[path]=/" *> $null

Write-Host ""
Write-Host "Site: https://$siteHost/  (live within 1-2 minutes)" -ForegroundColor Green
Write-Host "Google Cloud > Google Auth Platform > Branding:"
Write-Host "  App name:                 $AppName"
Write-Host "  Application home page:    https://$siteHost/"
Write-Host "  Application privacy policy: https://$siteHost/privacy.html"
Write-Host "  Application terms of service: https://$siteHost/terms.html"
Write-Host "  Authorised domain:        $siteHost"
if ($GoogleVerification) { Write-Host "Search Console code is on the homepage: click Verify in Search Console." }
else { Write-Host "No Search Console code yet: re-run with -GoogleVerification <code> after getting it." }
