<#
.SYNOPSIS
    Launch the microphone recorder GUI.

.DESCRIPTION
    Prefers the system Python. The nRF Connect SDK's bundled Python is NOT
    usable here: `import tkinter` appears to succeed but `tkinter.ttk` fails with
    "unknown location", because that interpreter ships a partial Tk install.

    Requirements: tkinter (stdlib) and pyserial. Both were already present on
    this machine's Python 3.13; if pyserial is missing elsewhere:
        python -m pip install pyserial
#>
$ErrorActionPreference = 'Stop'

$gui = Join-Path $PSScriptRoot 'mic_gui.py'
if (-not (Test-Path $gui)) { throw "Not found: $gui" }

$candidates = @()
$sys = Get-Command python -ErrorAction SilentlyContinue
if ($sys) { $candidates += $sys.Source }
$candidates += 'C:\ncs\toolchains\c1a76fddb2\opt\bin\python.exe'

$py = $null
foreach ($c in $candidates) {
    if (-not (Test-Path $c)) { continue }
    # Verify the interpreter can actually build a GUI before handing it the app.
    & $c -c "import tkinter.ttk, serial" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}

if (-not $py) {
    throw 'No Python with both tkinter.ttk and pyserial. Try: python -m pip install pyserial'
}

Write-Host "Launching GUI with $py" -ForegroundColor Cyan
& $py $gui
