# Toolchain and Project Setup

**Verified** on this machine, 2026-08-01 — every version below was read from real
build output, not from documentation.

---

## 1. What is being used

| Layer | Tool | Version | Location |
|---|---|---|---|
| SDK | **nRF Connect SDK (NCS)** | **v3.1.1** | `C:\ncs\v3.1.1` |
| RTOS | Zephyr | 4.1.99 (`ncs-v3.1.1`) | `C:\ncs\v3.1.1\zephyr` |
| Toolchain bundle | Zephyr SDK | **0.17.0** | `C:\ncs\toolchains\c1a76fddb2` |
| Compiler | `arm-zephyr-eabi-gcc` (GNU) | 12.2.0 | bundle → `opt\zephyr-sdk` |
| Linker | GNU `ld.bfd` | 2.38 | bundle |
| C library | **picolibc** | — | Zephyr default |
| Build system | CMake | 3.21.0 | bundle → `opt\bin` |
| Build driver | Ninja | — | bundle → `opt\bin` |
| Meta-tool | `west` | 1.4.0 | bundle → `opt\bin\Scripts` |
| Devicetree compiler | `dtc` | 1.4.7 | bundle → `opt\bin` |
| Build Python | CPython | 3.12.4 | bundle → `opt\bin` |
| Host/GUI Python | CPython | 3.13.14 | system (Microsoft Store) |
| Board target | — | **`xiao_ble/nrf52840/sense`** | upstream Zephyr |
| Flashing | UF2 mass storage | bootloader 0.6.1 | on-device |

Other NCS versions (`v3.0.1`, `v3.1.0`) are installed but unused. Nothing in this
project needs a hardware debugger — the board flashes over plain USB.

### The bundle ↔ SDK mapping

`C:\ncs\toolchains\` holds three bundles with opaque hash names. The mapping lives in
`C:\ncs\toolchains\toolchains.json`:

| Bundle | NCS version |
|---|---|
| `c1a76fddb2` | **v3.1.1** ← in use |
| `b8b84efebd` | v3.1.0 |
| `0b393f9e1b` | v3.0.1 |

If you ever change `$NCS_VERSION` in `tools/build.ps1`, **change `$BUNDLE_ID` with it.**

---

## 2. Why `tools/build.ps1` exists

**`cmake`, `ninja`, `dtc` and `arm-zephyr-eabi-gcc` are not on PATH on this machine.**
They only exist inside the toolchain bundle. A bare `west build` in a fresh shell fails.

`tools/build.ps1` sets the environment every time, so a build never depends on a shell
somebody configured by hand:

```powershell
$env:ZEPHYR_BASE              = "C:\ncs\v3.1.1\zephyr"
$env:ZEPHYR_TOOLCHAIN_VARIANT = "zephyr"
$env:ZEPHYR_SDK_INSTALL_DIR   = "C:\ncs\toolchains\c1a76fddb2\opt\zephyr-sdk"
$env:PATH = "…\opt\bin\Scripts;…\opt\bin;$env:PATH"
```

Two non-obvious details it handles:

- **west resolves its workspace from the current directory.** This project is not a west
  workspace, so the script `Push-Location`s into `C:\ncs\v3.1.1` and passes the app by
  absolute path. That is why apps can live outside the SDK tree.
- **NCS enables sysbuild by default**, which nests output one level deeper than plain
  Zephyr (`build\<app>\<app>\zephyr\`). The script *searches* for `zephyr.uf2` rather
  than assuming a path.

---

## 3. Project layout

```
falldetect-gkl/
├── PROJECT_OUTLINE.md      system design, phases, risks
├── ACCEL_PLAN.md           accelerometer / data-collection plan
├── MIC_PLAN.md             PDM microphone recorder plan
├── SETUP.md                this file
├── apps/
│   ├── blink/              Phase 0 bring-up
│   │   ├── CMakeLists.txt
│   │   ├── prj.conf
│   │   └── src/main.c
│   └── mic_record/
│       ├── CMakeLists.txt
│       ├── prj.conf
│       ├── boards/
│       │   └── xiao_ble_nrf52840_sense.overlay
│       └── src/main.c
├── tools/
│   ├── build.ps1           build + flash driver
│   ├── mic_gui.py          recorder GUI
│   └── mic_gui.ps1         GUI launcher
├── build/                  generated, disposable
└── recordings/             captured .wav files
```

---

## 4. Adding a new app

Three files minimum, plus an overlay only if you need to change devicetree.

**`apps/<name>/CMakeLists.txt`**
```cmake
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(<name>)
target_sources(app PRIVATE src/main.c)
```

**`apps/<name>/prj.conf`** — Kconfig options, e.g. `CONFIG_GPIO=y`

**`apps/<name>/src/main.c`** — with `int main(void)`

**`apps/<name>/boards/xiao_ble_nrf52840_sense.overlay`** *(optional)* — devicetree
changes. The filename must be the board target with `/` replaced by `_`; west picks it
up automatically. This is how `mic_record` enables `pdm0` and the mic power rail.

Then:
```powershell
.\tools\build.ps1 <name> -Pristine
```

---

## 5. Build and flash workflow

```powershell
.\tools\build.ps1 blink               # incremental build
.\tools\build.ps1 blink -Pristine     # wipe first — REQUIRED after editing
                                      #   prj.conf, CMakeLists.txt or an overlay
.\tools\build.ps1 blink -Flash        # build, then flash
```

Flashing is a two-step dance:

1. Run with `-Flash`. The script waits up to 60 s for the bootloader.
2. **Double-tap RST** (the tiny button beside the USB-C connector, two firm presses
   about a quarter-second apart). The `XIAO-SENSE` drive appears and the script copies
   `zephyr.uf2` to it; the board reboots into the new image automatically.

There is no debugger and none is needed.

### Host GUI

```powershell
.\tools\mic_gui.ps1
```

---

## 6. Gotchas already paid for

Each of these cost real time; they are recorded so they only cost it once.

| Gotcha | Detail |
|---|---|
| **Toolchain not on PATH** | `cmake`/`ninja`/`gcc` live only in the bundle. Always build via `tools/build.ps1`. |
| **UF2 volume race** | Windows assigns the drive letter *before* the volume accepts writes. Copying immediately fails and the board silently stays in the bootloader while reporting success. The script now waits for `INFO_UF2.TXT` to answer, then verifies the bootloader is gone. |
| **`-Pristine` after config changes** | Zephyr does not reliably pick up `prj.conf` / overlay edits on an incremental build. |
| **1200-baud touch ≠ UF2 mode** | It enters *serial-DFU-only* mode with no mass-storage drive. Only a physical double-tap gives the drag-and-drop drive. |
| **Zephyr has no 1200-baud touch** | The Arduino/CircuitPython firmware implemented it; Zephyr does not. Once Zephyr is flashed, **RST is the only way back to the bootloader.** |
| **SDK Python can't run the GUI** | `import tkinter` succeeds but `tkinter.ttk` fails with "unknown location" — partial Tk install. Use the system Python 3.13 (it already has tkinter and pyserial). |
| **SDK Python can't pip-install sdists** | Its isolated build env loses `_socket`. Use `--no-build-isolation`. |
| **PowerShell `2>&1` on native exes** | PS 5.1 wraps a native command's stderr as error records, so normal `west` progress output aborts the script. Never redirect west's stderr. |
| **USB VID/PID warnings** | `CONFIG_USB_DEVICE_VID/PID` are Zephyr test defaults (`0x2FE3:0x0100`). Harmless for development; must be set for a real product. |

---

## 7. Board identity (read from the device)

```
UF2 Bootloader 0.6.1
Model:       Seeed XIAO nRF52840
Board-ID:    Seeed_XIAO_nRF52840_Sense
SoftDevice:  S140 version 7.3.0
```

The S140 v7.3.0 SoftDevice is why applications link at **0x27000** — the
`nrf52840_partition_uf2_sdv7` layout reserves the region below it. Zephyr does not use
the SoftDevice (it brings its own BLE host); it just respects the reservation so the UF2
bootloader survives.
