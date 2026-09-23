# Veröffentlicht eine neue JARVIS-Version komplett automatisch:
#   bauen -> Code committen & pushen -> GitHub-Release mit Installer + SHA-256 -> prüfen
# Die Versionsnummer kommt aus jarvis/__init__.py (VERSION).
# Aufruf:  .\release.ps1                      (Beschreibung aus den Commits seit dem letzten Release)
#          .\release.ps1 -Notes "Neu: …"      (eigene Beschreibung)
param([string]$Notes = "", [switch]$NoBuild)
# Nicht "Stop": Windows PowerShell 5.1 wertet harmlose stderr-Hinweise von git/gh sonst als Fehler.
# Stattdessen wird nach jedem Schritt $LASTEXITCODE geprüft.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$git = "C:\Program Files\Git\cmd\git.exe"
$env:PATH = "C:\Program Files\Git\cmd;$env:PATH"   # gh ruft intern „git“ auf
$gh = @("C:\Program Files\GitHub CLI\gh.exe", "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $gh) { throw "GitHub CLI nicht gefunden (winget install GitHub.cli)" }
& $gh auth status *> $null
if ($LASTEXITCODE -ne 0) { throw "GitHub CLI ist nicht angemeldet (gh auth login --web)" }

$version = (& .\.venv\Scripts\python.exe -c "import jarvis; print(jarvis.VERSION)").Trim()
$tag = "v$version"
& $gh release view $tag *> $null
if ($LASTEXITCODE -eq 0) { throw "Release $tag gibt es schon – zuerst VERSION in jarvis/__init__.py erhöhen." }

# 1) Bauen (-NoBuild: vorhandenen Installer dieser Version verwenden)
if (-not $NoBuild) {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
    if ($LASTEXITCODE -ne 0) { throw "Build fehlgeschlagen" }
}
$setup = "dist\JARVIS-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "$setup fehlt" }

# 2) Code sichern und hochladen
& $git add -A
& $git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { & $git commit -q -m "Version $version" }
& $git push -q
if ($LASTEXITCODE -ne 0) { throw "git push fehlgeschlagen" }

# 3) Beschreibung: eigene oder die Commits seit dem letzten Release
if (-not $Notes) {
    & $git fetch -q --tags
    $prev = (& $gh release view --json tagName --jq .tagName 2>$null)
    $range = if ($prev) { "$prev..HEAD" } else { "HEAD~10..HEAD" }
    $lines = & $git log $range --pretty="- %s" | Where-Object { $_ -notmatch "^- Version \d" }
    $Notes = if ($lines) { "Änderungen:`n" + ($lines -join "`n") } else { "JARVIS $version" }
}

# 4) Release veröffentlichen (Tag wird auf GitHub angelegt)
$notesFile = Join-Path $env:TEMP "jarvis-release-notes.md"
Set-Content $notesFile $Notes -Encoding utf8
& $gh release create $tag $setup "$setup.sha256" --title "JARVIS $version" --notes-file $notesFile --target main --latest
if ($LASTEXITCODE -ne 0) { throw "Release konnte nicht erstellt werden" }
Remove-Item $notesFile -ErrorAction SilentlyContinue

# 5) Prüfen – genau so, wie JARVIS selbst nach Updates sucht
$repo = (& $gh repo view --json nameWithOwner --jq .nameWithOwner).Trim()
$rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{ Accept = "application/vnd.github+json" }
$shaAsset = $rel.assets | Where-Object name -like "*.sha256"
$online = (Invoke-RestMethod $shaAsset.browser_download_url).Split(" ")[0].Trim()
$local = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
if ($rel.tag_name -ne $tag -or $online -ne $local) { throw "Prüfung fehlgeschlagen: latest=$($rel.tag_name), SHA online=$online lokal=$local" }
Write-Host "`nVeröffentlicht: $($rel.html_url)" -ForegroundColor Green
Write-Host "JARVIS findet das Update jetzt über Einstellungen -> Updates oder „Lade dir dein Update runter“."
