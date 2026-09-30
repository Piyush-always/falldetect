<#
.SYNOPSIS
    Package FD Studio as a single shareable Windows exe.

.DESCRIPTION
    Produces release\FD Studio.exe - the ONE exe, tracked in git - no Python
    needed on the target PC, only Windows 10/11 with Bluetooth. release\ is
    the folder you zip and send (see release\START HERE.txt). The exe keeps
    data\ and firmware\ in the folder it is run from.

    It contains NO bot token: the repo is public. To ship it with phone alerts
    set up, put alerts.bundle.json (git-ignored) in release\ next to it:

        { "telegram_bot_token": "123:ABC...", "ntfy_topic": "fd-sos-...",
          "wearer_name": "Grandma" }

    On first run the exe sets up phone alerts from that file. Send the file
    privately: whoever has it can use the bot and read the alert topic.

    Builds in its own venv (build\exe-venv) rather than the system py -3.13:
    that interpreter carries the obsolete 'enum34' backport, which PyInstaller
    refuses to run with, and a clean venv keeps unrelated packages out of the
    exe. The venv is created on first run and reused after.

.EXAMPLE
    .\tools\build_exe.ps1
#>
$ErrorActionPreference = 'Stop'

$repo  = Split-Path $PSScriptRoot -Parent
$entry = Join-Path $PSScriptRoot 'fd_studio.py'
$venv  = Join-Path $repo 'build\exe-venv'
$py    = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host "Creating build venv: $venv" -ForegroundColor Cyan
    & py -3.13 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed ($LASTEXITCODE)" }
}
& $py -m pip install --quiet --disable-pip-version-check `
    pyinstaller PySide6 numpy pyserial bleak smpclient segno
if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }

# bleak picks its WinRT backend at runtime and winrt is a namespace package,
# so PyInstaller's static import scan misses both - collect them explicitly.
# No --add-data: nothing secret goes inside the exe.
& $py -m PyInstaller $entry `
    --name 'FD Studio' `
    --onefile --windowed --noconfirm --clean `
    --paths $PSScriptRoot `
    --collect-submodules bleak `
    --collect-submodules winrt `
    --collect-submodules smpclient `
    --distpath (Join-Path $repo 'release') `
    --workpath (Join-Path $repo 'build\pyinstaller') `
    --specpath (Join-Path $repo 'build\pyinstaller')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

Write-Host "Built: $(Join-Path $repo 'release\FD Studio.exe')" -ForegroundColor Green
if (-not (Test-Path (Join-Path $repo 'release\alerts.bundle.json'))) {
    Write-Host "release\alerts.bundle.json is missing: the shipped exe will have no phone alerts." -ForegroundColor Yellow
}
Write-Host "To ship: zip the release folder and send it privately." -ForegroundColor Cyan
