<#
.SYNOPSIS
    Build (and optionally flash) a falldetect-gkl Zephyr application.

.DESCRIPTION
    cmake, ninja, dtc and the ARM compiler are not on PATH on this machine —
    they live inside the nRF Connect SDK toolchain bundle. This script sets up
    that environment and drives west, so builds are reproducible without
    relying on a shell that happens to have been set up by hand.

    Toolchain bundle c1a76fddb2 maps to NCS v3.1.1 per
    C:\ncs\toolchains\toolchains.json. If you switch SDK version, update BOTH
    $NCS_VERSION and $BUNDLE_ID together.

.EXAMPLE
    .\tools\build.ps1                     # the product firmware at the repo root
    .\tools\build.ps1 blink -Pristine     # a bench app under apps/
    .\tools\build.ps1 mic_record -Flash
#>
[CmdletBinding()]
param(
    # 'falldetect' (the default) builds the product firmware at the repository
    # root. Any other name builds apps/<name>.
    [Parameter(Position = 0)]
    [string]$App = 'falldetect',

    # Wipe the build directory first. Use after changing prj.conf, devicetree
    # overlays or CMakeLists.txt — Zephyr does not always pick those up.
    [switch]$Pristine,

    # Copy the resulting .uf2 to the board's bootloader drive.
    [switch]$Flash
)

$ErrorActionPreference = 'Stop'

$NCS_VERSION = 'v3.1.1'
$BUNDLE_ID   = 'c1a76fddb2'
$BOARD       = 'xiao_ble/nrf52840/sense'

$NCS = "C:\ncs\$NCS_VERSION"
$TC  = "C:\ncs\toolchains\$BUNDLE_ID"

foreach ($p in @($NCS, $TC)) {
    if (-not (Test-Path $p)) { throw "Not found: $p" }
}

$repo = Split-Path -Parent $PSScriptRoot

# The product firmware lives at the repo root (CMakeLists.txt + prj.conf + src/).
# Everything under apps/ is a self-contained bench application.
if ($App -in @('falldetect', 'main', '.', '')) {
    $App    = 'falldetect'
    $srcDir = $repo
} else {
    $srcDir = Join-Path $repo "apps\$App"
}
$bldDir = Join-Path $repo "build\$App"

if (-not (Test-Path (Join-Path $srcDir 'CMakeLists.txt'))) {
    throw "No Zephyr application at: $srcDir"
}

# --- toolchain environment -------------------------------------------------
$env:ZEPHYR_BASE              = "$NCS\zephyr"
$env:ZEPHYR_TOOLCHAIN_VARIANT = 'zephyr'
$env:ZEPHYR_SDK_INSTALL_DIR   = "$TC\opt\zephyr-sdk"
$env:PATH                     = "$TC\opt\bin\Scripts;$TC\opt\bin;$env:PATH"

Write-Host "app   : $App"     -ForegroundColor Cyan
Write-Host "board : $BOARD"   -ForegroundColor Cyan
Write-Host "sdk   : $NCS_VERSION ($BUNDLE_ID)" -ForegroundColor Cyan
Write-Host ''


# --- VERSION drift guard ---------------------------------------------------
# CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION derives from the app's VERSION file, but
# it is a *Kconfig* value: an incremental build prints "No change to
# configuration" and signs the image with the PREVIOUS version. You then push
# an update and the device truthfully reports the old version number, which is
# worse than having no version at all - it makes "what is installed?"
# actively misleading.
#
# So: if the VERSION file disagrees with the last generated .config, force a
# pristine build rather than leaving it to be remembered.
$verFile = Join-Path $srcDir 'VERSION'
if ((Test-Path $verFile) -and (-not $Pristine) -and (Test-Path $bldDir)) {
    $v = @{}
    foreach ($line in (Get-Content $verFile)) {
        if ($line -match '^\s*([A-Z_]+)\s*=\s*(\d+)') { $v[$Matches[1]] = $Matches[2] }
    }
    if ($v.ContainsKey('VERSION_MAJOR')) {
        $want = "$($v['VERSION_MAJOR']).$($v['VERSION_MINOR']).$($v['PATCHLEVEL'])"
        $cfg = Get-ChildItem $bldDir -Filter '.config' -Recurse -File -ErrorAction SilentlyContinue |
               Where-Object { $_.FullName -notmatch 'mcuboot' } |
               Where-Object { Select-String -Path $_.FullName -Pattern 'MCUBOOT_IMGTOOL_SIGN_VERSION' -Quiet } |
               Select-Object -First 1
        if ($cfg) {
            $line = Select-String -Path $cfg.FullName -Pattern 'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="([^"]+)"'
            if ($line) {
                $have = ($line.Matches[0].Groups[1].Value -split '\+')[0]
                if ($have -ne $want) {
                    Write-Host "VERSION changed ($have -> $want) - forcing a pristine build so the" -ForegroundColor Yellow
                    Write-Host "signed image actually carries the new version." -ForegroundColor Yellow
                    Write-Host ''
                    $Pristine = $true
                }
            }
        }
    }
}

# --- build -----------------------------------------------------------------
# west resolves its workspace from the working directory, so run it from the
# SDK tree and point at our out-of-tree app by absolute path.
$westArgs = @('build', '-b', $BOARD, '-d', $bldDir)
if ($Pristine) { $westArgs += @('-p', 'always') }
$westArgs += $srcDir

Push-Location $NCS
try {
    & west @westArgs
    if ($LASTEXITCODE -ne 0) { throw "Build failed (exit code $LASTEXITCODE)" }
}
finally {
    Pop-Location
}

# When MCUboot is a child image (see sysbuild.conf), sysbuild produces a
# merged.hex spanning both mcuboot and the app but NO merged .uf2 - only
# separate per-image ones. Flashing just one of those over UF2 leaves the
# other image's flash region exactly as it was (the Adafruit bootloader only
# writes the addresses a UF2 file actually contains), so a plain
# "first zephyr.uf2 found" search can silently flash the app while leaving a
# stale or blank MCUboot in place. Prefer converting merged.hex when present;
# it is the only artifact that reliably boots the board from a blank slate.
$mergedHex = Join-Path $bldDir 'merged.hex'
$uf2 = $null

if (Test-Path $mergedHex) {
    # Verified against this board's board.cmake ("--board-id=Seeed_XIAO..."):
    # the per-image UF2s this same build already produced carry family ID
    # 0xada52840 (NRF52840) - read back with uf2conv.py -i rather than
    # assumed, since board.cmake's --board-id string isn't itself a uf2conv
    # argument in this Zephyr version.
    $uf2conv = "$NCS\zephyr\scripts\build\uf2conv.py"
    $mergedUf2 = Join-Path $bldDir 'merged.uf2'
    & python $uf2conv -c -f 0xada52840 -o $mergedUf2 $mergedHex
    if ($LASTEXITCODE -ne 0) { throw 'merged.hex -> uf2 conversion failed' }
    $uf2 = Get-Item $mergedUf2
} else {
    # No child bootloader image (e.g. mic_record, datalog) - one uf2 exists.
    $uf2 = Get-ChildItem $bldDir -Filter 'zephyr.uf2' -Recurse -File -ErrorAction SilentlyContinue |
           Select-Object -First 1
}

Write-Host ''
Write-Host 'Build OK' -ForegroundColor Green
if ($uf2) {
    Write-Host ("  {0}  ({1:N1} KB)" -f $uf2.FullName, ($uf2.Length / 1KB))
}

# --- flash -----------------------------------------------------------------
if (-not $Flash) { return }

if (-not $uf2) { throw 'No zephyr.uf2 produced — cannot flash.' }

# The XIAO exposes a mass-storage bootloader after a double-tap of RST.
# INFO_UF2.TXT identifies the board and confirms which variant this is.
# Wait for the bootloader rather than checking once, so the double-tap can
# happen after the build finishes instead of having to be timed before it.
Write-Host ''
Write-Host 'Double-tap RST on the XIAO to enter the bootloader (waiting up to 60s)...' -ForegroundColor Yellow

$drive = $null
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    $drive = Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
             Where-Object { $_.DriveType -eq 2 } | Select-Object -First 1
    if ($drive) { break }
    Start-Sleep -Milliseconds 500
}

if (-not $drive) {
    Write-Warning 'No UF2 bootloader drive appeared. Re-run with -Flash and double-tap RST.'
    return
}

Write-Host ''
Write-Host "Bootloader drive: $($drive.DeviceID) ($($drive.VolumeName))" -ForegroundColor Cyan
Get-Content (Join-Path $drive.DeviceID '\INFO_UF2.TXT') | ForEach-Object { "  $_" }

# Windows assigns the drive letter before the volume will accept writes. Copying
# immediately fails with "A device which does not exist was specified" and the
# board silently stays in the bootloader, so confirm it answers first.
for ($i = 0; $i -lt 20; $i++) {
    if (Test-Path (Join-Path $drive.DeviceID '\INFO_UF2.TXT')) { break }
    Start-Sleep -Milliseconds 500
}

try {
    Copy-Item $uf2.FullName -Destination "$($drive.DeviceID)\NEW.UF2" -Force -ErrorAction Stop
} catch {
    # The board reboots the instant the last block lands, so the volume can
    # disappear mid-copy. That is expected; the check below decides the outcome.
    Write-Host "  (volume detached during copy: $($_.Exception.Message))" -ForegroundColor DarkGray
}

Start-Sleep -Seconds 5
if (Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 2 }) {
    Write-Warning 'Board is STILL in the bootloader - the flash did not take. Re-run with -Flash.'
} else {
    Write-Host ''
    Write-Host 'Flashed. Board rebooted into the new image.' -ForegroundColor Green
}
