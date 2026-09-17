<#
.SYNOPSIS
    Launch FD Studio with an interpreter that actually has its dependencies.

.DESCRIPTION
    This machine has several Pythons and `python` does not resolve to the one
    carrying PySide6:

        WindowsApps\python.exe          3.13  <- PySide6 lives here
        AppData\Local\Python\bin        3.14  <- what `python` resolves to
        C:\ncs\toolchains\...           3.12  <- SDK build interpreter

    So rather than depend on PATH order, probe every interpreter we can find and
    use the first that can import PySide6, numpy and pyserial together.

.EXAMPLE
    .\tools\fd_studio.ps1
#>
$ErrorActionPreference = 'Stop'

$entry = Join-Path $PSScriptRoot 'fd_studio.py'
if (-not (Test-Path $entry)) { throw "Not found: $entry" }

# Candidates, best-known first, then anything the py launcher and PATH offer.
$candidates = New-Object System.Collections.Generic.List[string]
$candidates.Add('C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe')

try {
    foreach ($line in (& py -0p 2>$null)) {
        if ($line -match '([A-Za-z]:\\[^\s].*python\.exe)') {
            $candidates.Add($Matches[1])
        }
    }
} catch { }

foreach ($p in (where.exe python 2>$null)) { $candidates.Add($p) }

$py = $null
$seen = @{}
foreach ($c in $candidates) {
    if (-not $c -or $seen.ContainsKey($c) -or -not (Test-Path $c)) { continue }
    $seen[$c] = $true
    & $c -c "import PySide6, numpy, serial" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}

if (-not $py) {
    Write-Host 'No interpreter has all of PySide6, numpy and pyserial.' -ForegroundColor Red
    Write-Host 'Install them into one, for example:' -ForegroundColor Yellow
    Write-Host '    py -3.13 -m pip install PySide6 numpy pyserial'
    exit 1
}

$ver = & $py -c "import sys;print(sys.version.split()[0])"
# ASCII only: the console codepage here mangles non-ASCII separators.
Write-Host "FD Studio | Python $ver | $py" -ForegroundColor Cyan
& $py $entry
