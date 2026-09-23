<#
.SYNOPSIS
    Package FD Studio as a single shareable Windows exe.

.DESCRIPTION
    Produces dist\FD Studio.exe - no Python needed on the target PC, only
    Windows 10/11 with Bluetooth. The exe keeps data\ and firmware\ in the
    folder it is run from, so put it in its own folder before sharing.

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
    pyinstaller PySide6 numpy pyserial bleak smpclient
if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }

# bleak picks its WinRT backend at runtime and winrt is a namespace package,
# so PyInstaller's static import scan misses both - collect them explicitly.
& $py -m PyInstaller $entry `
    --name 'FD Studio' `
    --onefile --windowed --noconfirm --clean `
    --paths $PSScriptRoot `
    --collect-submodules bleak `
    --collect-submodules winrt `
    --collect-submodules smpclient `
    --distpath (Join-Path $repo 'dist') `
    --workpath (Join-Path $repo 'build\pyinstaller') `
    --specpath (Join-Path $repo 'build\pyinstaller')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

Write-Host "Built: $(Join-Path $repo 'dist\FD Studio.exe')" -ForegroundColor Green
