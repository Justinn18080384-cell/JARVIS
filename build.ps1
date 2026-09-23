# Baut JARVIS komplett: Icon -> JARVIS.exe (PyInstaller) -> JARVIS-Setup-x.y.z.exe (Inno Setup) -> SHA-256
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"

$version = (& $py -c "import jarvis; print(jarvis.VERSION)").Trim()
Write-Host "== JARVIS $version wird gebaut ==" -ForegroundColor Cyan

# Versionsinfo der .exe aktualisieren
$parts = ($version.Split(".") + @("0", "0", "0"))[0..3] -join ", "
(Get-Content build\version_info.txt -Raw -Encoding utf8) `
    -replace "filevers=\([^)]*\)", "filevers=($parts)" -replace "prodvers=\([^)]*\)", "prodvers=($parts)" `
    -replace "'FileVersion', '[^']*'", "'FileVersion', '$version'" -replace "'ProductVersion', '[^']*'", "'ProductVersion', '$version'" |
    Set-Content build\version_info.txt -Encoding utf8

& $py tools\make_icon.py
& .\.venv\Scripts\pyinstaller.exe build\jarvis.spec --noconfirm --clean --distpath dist --workpath build\work --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fehlgeschlagen" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 nicht gefunden (winget install JRSoftware.InnoSetup)" }
& $iscc /Qp "/DAppVersion=$version" build\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup fehlgeschlagen" }

$setup = "dist\JARVIS-Setup-$version.exe"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
Set-Content "$setup.sha256" "$hash  JARVIS-Setup-$version.exe" -Encoding ascii
Write-Host "`nFertig: $setup" -ForegroundColor Green
Write-Host "SHA-256: $hash"
