<#
.SYNOPSIS
    Launch the labelled activity recorder.

.DESCRIPTION
    Prefers the system Python. The nRF Connect SDK's bundled Python cannot run
    this: `import tkinter` appears to succeed but `tkinter.ttk` fails, because
    that interpreter ships a partial Tk install.

    Requires tkinter (stdlib) and pyserial:
        python -m pip install pyserial
#>
$ErrorActionPreference = 'Stop'

$gui = Join-Path $PSScriptRoot 'datalog_gui.py'
if (-not (Test-Path $gui)) { throw "Not found: $gui" }

$candidates = @()
$sys = Get-Command python -ErrorAction SilentlyContinue
if ($sys) { $candidates += $sys.Source }
$candidates += 'C:\ncs\toolchains\c1a76fddb2\opt\bin\python.exe'

$py = $null
foreach ($c in $candidates) {
    if (-not (Test-Path $c)) { continue }
    & $c -c "import tkinter.ttk, serial" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}
if (-not $py) {
    throw 'No Python with both tkinter.ttk and pyserial. Try: python -m pip install pyserial'
}

Write-Host "Launching recorder with $py" -ForegroundColor Cyan
& $py $gui
