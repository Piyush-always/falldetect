# falldetect-gkl — Project Profile

Wearable fall detection for an independently-living elderly person. Full system
design is in [doc/PROJECT_OUTLINE.md](doc/PROJECT_OUTLINE.md); this file is the
fixed technical profile the governing firmware rules require, plus the things
that are easy to get wrong here.

---

## Hardware (fixed — never introduce anything not listed here)

| Field | Value | Confidence |
|---|---|---|
| Board | Seeed Studio XIAO nRF52840 **Sense** | Verified — bootloader reports `Board-ID: Seeed_XIAO_nRF52840_Sense` |
| MCU | Nordic nRF52840, Cortex-M4**F** @ 64 MHz | Verified |
| FPU | **Present**, single-precision only | Verified |
| RAM / Flash | 256 KB / 1 MB | Verified |
| External flash | 2 MB QSPI onboard | Verified |
| IMU | ST **LSM6DS3TR-C** (accel + gyro), I²C | Verified — driver binds |
| IMU driver | Zephyr **`st,lsm6dsl`** (shared register map) | Verified |
| IMU INT1 | **P0.11** | Verified — devicetree `irq-gpios` |
| IMU power rail | **P1.08**, `regulator-boot-on` | Verified |
| Microphone | PDM MSM261D3526H1CPM, rail **P1.10** (NOT boot-on) | Verified |
| Radio | BLE 5.0 — **no WAN radio** | Verified |
| Bootloader | Adafruit UF2 0.6.1 + S140 SoftDevice v7.3.0 reserved below `0x27000` | Verified |

**FPU note:** single-precision float is acceptable off the hot path. Double
still triggers soft-float. Prefer fixed-point in the sample path and state the
Q-format. Today all firmware maths is integer — keep it that way unless there
is a measured reason not to.

## Toolchain (fixed)

| Item | Value |
|---|---|
| SDK | nRF Connect SDK **v3.1.1** (`C:\ncs\v3.1.1`), Zephyr 4.1.99 |
| Toolchain bundle | `c1a76fddb2` (Zephyr SDK 0.17.0) |
| Board target | **`xiao_ble/nrf52840/sense`** |
| Build | `.\tools\build.ps1 <app> [-Pristine] [-Flash]` |
| Host Python | `py -3.13` (system). The SDK's Python cannot run the GUIs. |

`cmake`/`ninja`/`dtc`/`gcc` are **not on PATH**. Always build via
`tools/build.ps1`, which sets that environment up.

---

## Firmware applications

| Name | Path | Purpose |
|---|---|---|
| `falldetect` | `src/` (repo **root**) | The product. Currently the free-fall candidate-rate probe. Emits `$A` at 10 Hz. |
| `datalog` | `apps/datalog/` | Data-collection rig. 208 Hz accel+gyro+pedometer over USB CDC **and** BLE. **This is what FD Studio needs.** |
| `blink` | `apps/blink/` | Phase 0 bring-up. Where BLE OTA was proved out. |
| `mic_record` | `apps/mic_record/` | PDM recorder. Voice is deferred (OUTLINE §9). |

The product builds from the **repo root**, not from `apps/`. Everything under
`apps/` is a self-contained bench instrument and is not part of the product
build.

## Host tooling

See [tools/README.md](tools/README.md). The tool is **FD Studio**
(`.\tools\fd_studio.ps1`) — three tabs: User (consumer view), Debug (tuning),
Update (OTA). It connects over **BLE**, not serial.

---

## Wire protocol (device → host)

```
$I,<odr>,<accel_fs_g>,<gyro_fs_dps>,<pedo_ok>   identity, once on connect
$D,<seq>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>          sample, 208 Hz
$P,<seq>,<steps>                                 pedometer, 1 Hz
$X,<message>                                     device error
$A,<ax>,<ay>,<az>,<gx>,<gy>,<gz>                 root firmware only, 10 Hz
```

**Units: accel milli-g, gyro deci-dps.** Integer only, no floating point in the
sample path. The same format goes out over USB CDC and BLE/NUS, so all host
parsing is shared.

`seq` is not decoration — it is how dropped samples become visible. A corpus
with silent holes produces a model with silent holes. Always check the gap
count before trusting a recording.

---

## Flash layout — do not change without re-reading this

The board ships with a UF2 bootloader whose regions must survive. MCUboot and
the two OTA image slots are carved **only** out of the stock application span.

```
0x00000–0x27000  SoftDevice reserved   (Adafruit bootloader — untouched)
0x27000–0x33000  mcuboot                48 KB
0x33000–0x8F800  slot0 (running app)   370 KB
0x8F800–0xEC000  slot1 (OTA staging)   370 KB
0xEC000–0xF4000  settings storage      (untouched)
0xF4000–0x100000 UF2 bootloader code   (untouched)
```

Defined in `boards/xiao_ble_nrf52840_sense.overlay` **and** duplicated in
`sysbuild/mcuboot.overlay` — MCUboot builds as its own image with its own
devicetree pass, so both must be edited together. Same pair exists under
`apps/blink/` and `apps/datalog/`.

If the app outgrows 370 KB the linker fails the build outright. It is not
possible to silently ship an oversized image.

---

## Hard-won gotchas (each of these cost real time)

| Gotcha | Detail |
|---|---|
| **`CONFIG_NCS_SAMPLE_MCUMGR_BT_OTA_DFU` only *depends on* `BT_PERIPHERAL`** | It does not select it. Without `CONFIG_BT=y` + `CONFIG_BT_PERIPHERAL=y` it silently resolves to `n` and you get a non-BLE build that compiles clean. |
| **MCUboot inherits the board's forced `CONFIG_CONSOLE=y`** | Which pulls in USB CDC, which fails to link because MCUboot is `MULTITHREADING=n`. Disabled in `sysbuild/mcuboot.conf`. |
| **Enabling BLE Kconfig does not start BLE** | Nothing calls `bt_enable()` / `bt_le_adv_start()` for you. The app must do it. |
| **MCUboot reverts an unconfirmed image on the NEXT reset** | Firmware must call `boot_write_img_confirmed()` after its own startup checks pass. Never confirm from the host — that defeats the safety net. |
| **An unbounded DTR wait blocks self-confirm** | `datalog` once waited forever for a USB host, so it never reached the confirm call and every OTA silently reverted. All DTR waits are bounded to 5 s. |
| **Sysbuild produces `merged.hex` but no merged `.uf2`** | Flashing a per-image `.uf2` leaves the other region stale. `build.ps1` converts `merged.hex` itself. |
| **bleak's "Is Bluetooth turned on?" is a lie** | Reachable only *after* bleak confirms the radio is ON. It means the WinRT watcher went ABORTED — usually another app holding the scanner, or a wedged stack. |
| **Never `asyncio.run()` per BLE operation on Windows** | It tears down the WinRT COM apartment repeatedly and orphans advertisement watchers until Windows refuses new ones. One persistent loop — see `tools/fd_studio/ble_worker.py`. |
| **A VERSION bump needs a pristine build** | `CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION` derives from the VERSION file but is a *Kconfig* value; an incremental build prints "No change to configuration" and signs the OLD version. The device then reports a version it is not running. `build.ps1` now detects the drift and forces pristine. |
| **Default ATT MTU is 23 — smaller than one `$D` line** | `bt_nus_send()` fails outright above MTU-3, so every sample was rejected and only the 13-byte `$P` line got through. Windows never initiates MTU exchange; the peripheral must, which needs `CONFIG_BT_GATT_CLIENT=y`. |
| **Raising `BT_BUF_ACL_TX_COUNT` alone fails the build** | `BUILD_ASSERT(BT_BUF_EVT_RX_COUNT > BT_BUF_ACL_TX_COUNT)` — raise the event RX count with it. |
| **Draining the BLE buffer eagerly wastes the MTU** | Sending the instant a byte arrives gave 207 notifications/sec of ~30 bytes (6 KB/s, a third of samples lost). Batching to fill the 495-byte payload gives the same notification rate at full throughput. |
| **RST double-tap is timing-sensitive** | Two presses like a mouse double-click. Routine updates should use OTA, not USB. |
| **PowerShell 5.1 mangles native stderr** | Never redirect `west`'s stderr; it aborts the script. |

---

## Where the detector lives (and why)

The fall cascade is in **`tools/fd_studio/engine.py`**, on the PC — not on the
device. This is deliberate: thresholds tuned by guesswork are worthless, and
every firmware iteration costs a flash. Tune against live and recorded data
first, then port.

The engine is pure Python + numpy with **no Qt import**, so it can be replayed
headlessly against recorded CSV. Keep it that way — a detector that only runs
inside a window cannot be diffed against its firmware port.

**No machine learning yet, and that is a sequencing decision, not an oversight.**
A learned model needs (a) a measured cascade baseline to beat and (b) a labelled
corpus. Until both exist, a model is unfalsifiable. See OUTLINE §5.3.

---

## Current state

- OTA over BLE works end to end and is **verified on hardware** — pushed, booted,
  self-confirmed, checked via image state.
- `datalog` streams 208 Hz with **0 dropped samples** (measured).
- The device does **not** yet detect falls on its own. Detection is host-side.
- No cancel button or buzzer hardware exists yet (OUTLINE §6 calls it
  load-bearing for the false-alarm story).
- Sleep current has **never been measured** — outstanding since Phase 0.

## Non-goals right now

Cloud, hub, escalation ladder, voice/mic work, and the power trial are all
explicitly deferred by the user. Local import/export only. Do not build toward
them unless asked.
