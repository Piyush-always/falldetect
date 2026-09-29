# tools/

Host-side software. Nothing here runs on the device — firmware lives in
`src/` (the product) and `apps/` (bench instruments).

```
tools/
├── fd_studio.ps1     ← launch the tool
├── fd_studio.py         entry point (fd_studio.ps1 finds the right Python for you)
├── fd_studio/           THE TOOL — GUI package
├── build.ps1         ← build + flash firmware
├── scripts/             command-line utilities
└── bench/               older single-purpose GUIs, superseded by fd_studio
```

---

## FD Studio — the tool

```powershell
.\tools\fd_studio.ps1
```

Connects to the device over **Bluetooth** and shows three tabs:

| Tab | For | Shows |
|---|---|---|
| **User** | the wearer, family, a demo audience | One plain-language state — "All good", "Walking", "Possible fall" — plus the 30 s cancel window. No numbers. |
| **Debug** | tuning the detector | Live magnitude/tilt/variance, the fall cascade stage by stage, editable thresholds, session recording, offline corpus replay. |
| **Update** | shipping new firmware | Push a build to the device wirelessly. |

Both the User and Debug tabs read the **same** detector (`fd_studio/engine.py`).
There is no second algorithm and no smoothing that Debug cannot see — if the
two tabs disagree, that is a bug.

Detection is the rule-based cascade only. No machine learning yet: a model
needs a measured cascade baseline to beat and a labelled corpus to train on,
and neither exists in useful quantity. See `doc/PROJECT_OUTLINE.md` §5.3.

### Package layout

| File | Role |
|---|---|
| `app.py` | Window, tabs, wiring. |
| `engine.py` | The detector: posture, activity, fall cascade. Pure Python, no Qt — so it can be replayed and unit-tested headless. |
| `link.py` | `LinkBase` (parsing, gap detection, recording) + USB serial transport. |
| `ble_link.py` | BLE/NUS transport. What the app actually uses. |
| `ota.py` | Firmware update over BLE. Shared with `scripts/ota_flash.py`. |
| `replay.py` | Replays recorded sessions through the engine; sensitivity/specificity. |
| `notify.py` | Phone alerts: Telegram message + Telegram voice call (CallMeBot). No Qt. |
| `user_view.py`, `ota_view.py`, `widgets.py` | UI. |
| `tokens.py` | Every colour and spacing value. Nothing else hardcodes either. |

### Phone alerts

When a possible fall gets no response for 30 s, FD Studio sends a Telegram
message to the family and places a Telegram voice call that reads the alert
aloud. Dismissing the alert afterwards sends an "all clear" message (no call).

Setup, once (User tab):

1. **Phone alert settings** opens `%APPDATA%\FD Studio\alerts.json` in Notepad.
   It lives there, not in the repo or next to the exe, so the bot token can
   neither be committed nor shared with the exe.
2. In Telegram, message **@BotFather** → `/newbot` → paste the token into
   `telegram_bot_token`. Set `wearer_name`.
3. From each phone, send any message to the new bot — or add the bot to a
   family group and post there.
4. For the voice call: each person sends `/start` to **@CallMeBot_txtbot**,
   then add their `@username` to `callmebot_users`.
5. **Send test to phones.** It fills in the chat ids by itself, then sends a
   test message and call. The result shows under the button and in the Log.

Limits — say these out loud to whoever relies on it:

- Sent **from the laptop**. Laptop asleep, offline, or FD Studio closed means
  nothing is sent. The alert screen says "alert NOT sent" when a send fails.
- Both services are free. The Telegram Bot API is official; CallMeBot is a
  third-party service for personal use with no guarantee, and on iPhone the
  call rings but its audio may not play (their documented Telegram bug).
- "Reached" means the service accepted it — not that anyone read or answered.

---

## build.ps1 — build and flash firmware

```powershell
.\tools\build.ps1 datalog              # build
.\tools\build.ps1 datalog -Pristine    # wipe first (REQUIRED after prj.conf/overlay edits)
.\tools\build.ps1 datalog -Flash       # build, then flash over USB (double-tap RST)
```

Apps: `falldetect` (the product, at the repo root), `datalog`, `blink`,
`mic_record`. `cmake`/`ninja`/`gcc` are not on PATH on this machine — this
script sets that environment up, which is why builds always go through it.
See `doc/SETUP.md`.

**USB flashing is for bootstrapping and recovery.** Routine updates go over
the air — the Update tab, or `scripts/ota_flash.py`.

---

## scripts/ — command-line utilities

| Script | Does |
|---|---|
| `ota_flash.py` | Push firmware over BLE. Same implementation as the Update tab, so the two cannot drift. |
| `ble_dfu_scan.py` | Scan for a BLE DFU advertisement. Written to answer one bring-up question; kept because it is the fastest way to check whether a board is advertising at all. |

```powershell
py -3.13 tools\scripts\ota_flash.py datalog
```

---

## bench/ — older single-purpose GUIs

Superseded by FD Studio, kept because they still run and are occasionally
the quickest way to look at one thing.

| Tool | Was for | Now |
|---|---|---|
| `datalog_gui.py` | recording labelled sessions from `apps/datalog` | FD Studio's Debug tab |
| `ff_gui.py` | the free-fall candidate-rate probe (`src/main.c`) | FD Studio reads its `$A` format too |
| `mic_gui.py` | `apps/mic_record` — record and play back audio | not superseded; voice is deferred (`doc/PROJECT_OUTLINE.md` §9) |

---

## Python on this machine

Several interpreters are installed and `python` does not resolve to the one
with the GUI dependencies. `fd_studio.ps1` probes for a working one; for
scripts, call `py -3.13` explicitly. The SDK's own Python
(`C:\ncs\toolchains\...`) cannot run the GUIs — its Tk install is partial.
