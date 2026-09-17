# Fall Detection Wearable — Project Outline

**Status:** v0.2 — architecture decisions resolved (§3).
**Date:** 2026-08-01

---

## 1. Purpose

A body-worn device for an independently-living elderly person that **detects a fall
and gets a human to respond**, without the wearer having to do anything.

The product is not "a fall detector." It is an **escalation system** whose sensor
happens to be an accelerometer. The firmware is maybe 30% of the risk; the other 70%
is: does a caregiver actually get told, and do they believe it when they do.

### Success criteria (v1)

| # | Criterion | Target | How measured |
|---|---|---|---|
| S1 | Detect a hard fall | ≥ 90% sensitivity on wrist-mounted corpus | Offline replay against labelled dataset |
| S2 | Don't cry wolf | ≤ 1 false alarm / week / device | 14-day ADL wear trial, real user |
| S3 | Time to caregiver notification | ≤ 60 s from impact | End-to-end timestamped trace |
| S4 | Battery life | ≥ 7 days per charge (stretch: 30) | Measured, not calculated |
| S5 | Wearer can cancel a false alarm | 30 s window, one button | Usability test with target user |
| S6 | Device-offline is itself an alert | Detected within 15 min | Cloud-side heartbeat watchdog |

**S2 is the one that kills products.** An elderly user who gets woken at 3am by a
false alarm takes the device off and never puts it back on. Sensitivity is easy;
specificity is the engineering problem. Budget accordingly.

> **S2 at the wrist is materially harder than at the waist** (§3 D2). Plan on the
> learned model (§5 stage 2) being *required* to hit it, not optional. If the trial
> lands at 2–3 false alarms/week, the honest responses are: more ADL training data,
> or revisit mount point — not quietly relaxing the target.

**S6 is the one people forget.** A dead device and a fine person look identical from
the cloud. If you don't monitor liveness, you have a system that silently stops
working and nobody notices until it matters.

---

## 2. Project Profile (hardware — fixed)

| Field | Value | Confidence |
|---|---|---|
| Board | Seeed Studio XIAO nRF52840 **Sense** | **Verified** — bootloader `Board-ID: Seeed_XIAO_nRF52840_Sense` |
| MCU | Nordic nRF52840, Cortex-M4**F** @ 64 MHz | Verified (vendor spec) |
| FPU | **Present**, single-precision only | Verified |
| RAM / Flash | 256 KB / 1 MB | Verified |
| External flash | 2 MB QSPI onboard | Verified |
| IMU | ST **LSM6DS3TR-C** (3-axis accel + 3-axis gyro) | Verified |
| IMU interrupt | **INT1 → P0.11** | **Verified** — SDK devicetree `irq-gpios = <&gpio0 11 ...>` |
| IMU power enable | **P1.08** (GPIO-gated rail) | **Verified** — SDK devicetree `enable-gpios = <&gpio1 8 ...>` |
| PDM mic enable | **P1.10** (GPIO-gated rail) | **Verified** — SDK devicetree |
| Microphone | PDM digital mic (MSM261D3526H1CPM) | Verified |
| Radio | BLE 5.0 / 802.15.4 — **no WAN radio** | Verified |
| Charging | Onboard Li-Po charger (BQ25101), 50/100 mA select | Verified |
| Board standby | < 5 µA claimed | Assumed — vendor figure, must measure |

### Board identity (read from the device, 2026-08-01)

```
UF2 Bootloader 0.6.1
Model:       Seeed XIAO nRF52840
Board-ID:    Seeed_XIAO_nRF52840_Sense
SoftDevice:  S140 version 7.3.0
Date:        Nov 12 2021
```

The Sense question is **closed** — the IMU and PDM mic are present. The S140 v7.3.0
SoftDevice explains the `nrf52840_partition_uf2_sdv7` layout and why applications link
at **0x27000**; Zephyr does not use the SoftDevice (it brings its own BLE host), it just
respects the reserved region so the UF2 bootloader stays intact.

### Toolchain (established and working)

| Item | Value |
|---|---|
| SDK | nRF Connect SDK **v3.1.1** (`C:\ncs\v3.1.1`), Zephyr 4.1.99 |
| Toolchain bundle | `c1a76fddb2` (Zephyr SDK 0.17.0) — mapped in `C:\ncs\toolchains\toolchains.json` |
| Board target | **`xiao_ble/nrf52840/sense`** |
| Build | `.\tools\build.ps1 <app> [-Pristine] [-Flash]` |
| Flash | UF2 drag-and-drop to the `XIAO-SENSE` drive |

`cmake`/`ninja`/`dtc`/`arm-zephyr-eabi-gcc` are **not** on PATH — they live in the
toolchain bundle. `tools/build.ps1` sets that environment up, so builds do not depend on
a hand-configured shell.

### The single most important hardware fact

The LSM6DS3TR-C is not a dumb accelerometer. It has an on-chip event engine —
**free-fall, wake-up, activity/inactivity, 6D orientation, single/double-tap,
significant-motion, tilt** — plus a **4 KB FIFO**, and every one of those events can be
routed to INT1/INT2. (Ref: ST **AN5130**.)

That means the nRF52840 can sleep at single-digit µA and be woken **by the fall itself**.
Do not poll the IMU from the MCU. The entire power architecture (§7) hangs off this.

---

## 3. Decisions (resolved)

### D1. Connectivity → **Fixed home hub (v1)** ✅

BLE wearable → Raspberry Pi / ESP32 hub in the home → WiFi → cloud.

**Consequence, state it plainly in any product description:** v1 is an **in-home
system**. A fall in the garden, the street or a shop will not be reported. That is a
deliberate, bounded v1 scope, not a bug — but it must be disclosed to the wearer and
their family, not discovered by them.

Firmware rule that keeps the v2 door open: **the wearable never knows what the gateway
is.** It advertises, connects, emits events, retries until acked. Adding a phone as a
second path later is then a gateway/cloud change with **zero firmware impact**.

Practical hub notes: site it centrally, verify BLE coverage in the bathroom and bedroom
(the two rooms where falls actually happen and where walls are worst), and give the hub
a UPS or battery backup — a power cut currently takes the whole safety system down.

### D2. Mount point → **Wrist** ✅

Chosen for compliance. This is a defensible trade — a worn device with a weaker
algorithm beats an accurate device in a drawer — but it moves risk from the product
into the firmware, and §5 is rewritten accordingly.

**What you gain:** highest wear rate; the cancel button (§6) is always within reach on
the other hand, which is the single best thing you can do for S5; charging is a
familiar habit (people already charge watches).

**What you lose:** the arm moves independently of the body. The classic 4-stage cascade
leans on post-impact *body* orientation and stillness — at the wrist both are noisy.
Two stages of the four degrade. Plus the wrist sees far more high-g daily activity than
the trunk ever does.

**Wrist-specific complication worth designing around:** the fall may land on the arm
wearing the device (huge impact spike, arm pinned under the body) or on the other side
(muted signal, arm free). These look completely different. Your dataset must contain
**both**, and the detector must not be tuned to only one.

Mitigations are in §5 and §7 — the short version is: **use the gyroscope, and lean on
the learned model from the start.**

### D3. Toolchain → **Zephyr / nRF Connect SDK** ✅

Board `xiao_ble/nrf52840/sense` is upstream; IMU and PDM are already in devicetree;
`west flash -r uf2` works over USB with no debugger; MCUboot gives OTA updates, which a
fielded safety device needs.

---

## 4. System architecture

```
┌──────────────────────────┐
│  WEARABLE (XIAO Sense)   │  worn on wrist
│                          │
│  LSM6DS3TR-C ──INT1──┐   │   HW free-fall / wake-up IRQ
│   accel always-on    │   │   gyro powered on demand
│   (FIFO batching)    │   │
│                      ▼   │
│  nRF52840  ── fall cascade + classifier
│      │       ── cancel button + buzzer/LED
│      │       ── event store (QSPI, survives reconnect)
│      └── BLE GATT ───────┼──┐
└──────────────────────────┘  │  encrypted + bonded
                              │
                    ┌─────────▼─────────┐
                    │  HOME HUB         │  Pi / ESP32
                    │  BLE central      │  (+ phone app in v2)
                    │  store & forward  │
                    └─────────┬─────────┘
                              │  MQTT/TLS or HTTPS
                    ┌─────────▼─────────┐
                    │  CLOUD            │
                    │  event ingest     │
                    │  dedup + ack      │
                    │  liveness watchdog│
                    │  escalation ladder│
                    └─────────┬─────────┘
                              │
                    push / SMS / voice call
                              ▼
                    Caregiver 1 → 2 → emergency contact
```

---

## 5. Fall detection at the wrist

### 5.1 The gyro-on-demand trick

This is the core architectural idea for a wrist device, and it resolves what looks like
a hard conflict between accuracy and battery life.

At the wrist, angular velocity carries much of the fall signature — the arm rotates
characteristically during a fall in a way it doesn't during most daily activity. So you
want the gyro. But the gyro draws on the order of **milliamps** versus tens of
**microamps** for the accelerometer. Running it continuously destroys S4 outright.

**Resolution: never run the gyro continuously.**

```
  accel always on, low power, HW free-fall + wake-up IRQ armed   ← µA
        │
        │  INT1 fires (free-fall or high-g candidate)
        ▼
  MCU wakes, powers up gyro                                      ← mA, briefly
        │
        │  capture accel + gyro for a 3–5 s window
        │  (impact + immediate aftermath)
        ▼
  gyro off, classify, decide, sleep                              ← back to µA
```

The IMU's FIFO has already been batching accelerometer data, so you get the **pre-trigger**
samples too — the free-fall phase that happened *before* the MCU woke. That's the part
you'd otherwise lose.

Net cost: gyro active only for a few seconds per candidate event, a handful of times a
day. **Derived:** negligible against the daily budget. Verify once the real candidate-event
rate is known from Phase 1 — if the wrist throws hundreds of candidates a day, revisit.

### 5.2 Stage 1 — cascade (build first, as a baseline)

| Stage | Condition | Starting threshold | Wrist reliability |
|---|---|---|---|
| 1. Free-fall | \|a\| below threshold | < 0.5 g for 100–200 ms | **Good** — HW interrupt, nearly free |
| 2. Impact | \|a\| peak after free-fall | > 2.5–3.0 g within 500 ms | **Good** |
| 2b. Angular signature | gyro peak + rotation during fall | > 200–300 °/s | **Good** — wrist-specific, use it |
| 3. Orientation change | gravity vector, pre vs post | > 45–60° | **Weak** — arm moves freely |
| 4. Post-impact inactivity | low variance after impact | < 0.1 g RMS, 5–10 s | **Weak** — but not useless, see below |

> All thresholds are **Assumed** — literature starting points, not tuned values. Every
> one gets re-tuned against *your* wrist data in Phase 2.

Stages 3 and 4 are demoted at the wrist but **not deleted**. Use them as *soft evidence
feeding the classifier*, not as hard gates. A wrist that is completely still for 20 s
after a 3 g impact is still meaningful — just not conclusive on its own. Extend the
inactivity window longer than you would at the waist (15–20 s rather than 5–10 s); the
arm settles more slowly and more erratically than the trunk.

### 5.3 Stage 2 — learned classifier (expected, not optional)

At the wrist, plan for this in v1. A small model over a ~2–3 s window of **accel + gyro**
anchored at the impact. Edge Impulse targets the nRF52840 directly and is the pragmatic
route.

**Still build the cascade first.** Not as ceremony — as the baseline the model has to
beat. A learned model with no measured baseline is unfalsifiable: when it misses, you
can't tell a data problem from a label problem from an architecture problem. Get a real
ROC curve from the cascade, then require the model to improve on it.

Sensible division of labour: **cascade as the cheap gate** (runs always, catches the
99% of nothing-happened), **model as the arbiter** (runs only on candidates the cascade
lets through). That also keeps the model's power cost bounded to the same rare events
as the gyro.

### 5.4 The confounders — wrist edition

These must be in the ADL corpus in volume. The wrist list is much longer than the waist
list, which is exactly why S2 is harder here:

**Generic:** sitting down heavily · lying down on a bed · stairs, especially descending ·
getting into a car · picking something off the floor

**Wrist-specific — the ones that will actually bite you:**
clapping · waving · gesturing while talking · brushing teeth · washing hands ·
putting a hand down hard on a table or armrest · pushing up out of a chair by the
armrests · throwing or catching · taking the watch off and dropping it on a surface ·
**using a walking stick or frame** — repeated impact loading straight through the wrist,
many times a day, in exactly the demographic you're targeting · leaning weight on the arm ·
reaching up to a high shelf · a dog pulling on a lead

The walking-aid case deserves specific attention: it's high-frequency, high-g, and
correlated with the users most likely to fall. If your detector can't separate a walking
frame from a fall, S2 is unreachable.

**And the fall that doesn't look like one:** the **slow slump** — sliding down a wall or
out of a chair. Little or no free-fall phase, so stage 1 never fires. Document as a known
v1 gap, or add a separate low-and-still detector. Don't pretend the cascade covers it.

### 5.5 Data — the actual bottleneck

You cannot tune this without labelled data, and it is the phase most likely to be skipped
and most certain to cause pain later.

- **Never collect real falls from elderly participants.** Simulated falls onto crash
  mats, young healthy volunteers, consent and an ethics-shaped protocol.
- **ADL data matters more than fall data**, and doubly so at the wrist. Falls are the
  easy half of the corpus.
- **Collect on both wrists**, and include falls landing on the instrumented arm *and* the
  opposite arm (§3 D2).
- **Public datasets — pick wrist-inclusive ones:** UMAFall, UP-Fall and FallAllD include
  wrist placements. **SisFall (waist) and MobiAct (pocket/thigh) transfer poorly here** —
  useful for algorithm shape, not for wrist thresholds. Verify exact placements before
  relying on any of them.
- Log **raw accel + gyro** at full rate to the 2 MB QSPI during collection. You'll re-run
  the detector offline hundreds of times; if you only log the decision, every experiment
  has to be redone on hardware.

---

## 6. Alert & escalation flow

```
   IMPACT
     │
     ├─► t+0s    device: buzzer + LED + (vibration if added)
     │           ── 30 s cancel countdown starts
     │
     ├─► t+0s    BLE ──► "PRE_ALERT" ──► cloud       ◄── SEND IMMEDIATELY
     │
     ├─► user presses button ──► "CANCELLED" ──► cloud ──► stand down
     │
     ├─► t+30s   no cancel ──► "FALL_CONFIRMED" ──► cloud
     │
     ├─► cloud   ──► caregiver 1 (push + SMS)
     ├─► t+3min  no ack ──► caregiver 2
     └─► t+6min  no ack ──► emergency contact / call centre
```

Two things here are not optional:

**Send PRE_ALERT before the countdown resolves.** If the device is smashed, the battery
is knocked loose, or the wearer crawls out of BLE range, the cloud has still heard
something. The cloud rule is: **`PRE_ALERT` followed by silence is a fall**, not a
non-event. Waiting for confirmation before transmitting means the worst falls are the
ones you fail to report.

**The 30 s cancel window is what makes S2 survivable.** It converts a false positive
from "caregiver panic at 3am" into "annoying beep the wearer silences." It doesn't
reduce the false-positive *rate*, but it collapses the *cost*, which is what actually
determines whether the device stays worn.

The wrist choice pays off here: the button is always reachable with the opposite hand,
no fumbling for a pendant. Make it large, tactile, and unmistakable — and make the alarm
loud enough to wake someone who has fallen and is dazed. Given a wrist device with a
higher expected false-alarm rate, **this cancel path is load-bearing**; treat it as a
primary feature, not an afterthought. It is added hardware — the XIAO's RST button is
not it.

**Cloud-side liveness (S6):** device heartbeats every N minutes with battery %. Missing
heartbeats → "device offline" alert to caregiver. Distinct from a fall alert, lower
urgency, but it must exist or the system fails silently. Low battery gets its own warning
well before shutdown.

### Event schema (starting point)

```json
{
  "device_id":  "xiao-0001",
  "event_id":   "uuid-or-monotonic-counter",
  "type":       "PRE_ALERT | FALL_CONFIRMED | CANCELLED | HEARTBEAT | LOW_BATTERY",
  "ts_device":  1754049600,
  "confidence": 0.0,
  "signature":  {
    "freefall_ms": 180, "impact_g": 3.4, "gyro_peak_dps": 280,
    "tilt_deg": 72, "still_s": 8, "model_score": 0.91
  },
  "battery_pct": 87
}
```

`event_id` gives the cloud **idempotent dedup** — the device retries until acked, so
duplicates are expected and must be harmless. Include the raw `signature` on every alert:
when you're triaging a false positive in the field, it's the difference between diagnosing
it and guessing. At the wrist you *will* be triaging false positives, so this field earns
its keep.

---

## 7. Power strategy

The architecture *is* the power strategy:

1. **Accel always on, MCU asleep.** LSM6DS3TR-C low-power accel-only, free-fall + wake-up
   interrupts armed, batching to FIFO. nRF52840 System ON idle, RAM retained.
2. **Gyro off by default**, powered only for a 3–5 s window after a candidate event
   (§5.1). This is the difference between a 7-day device and a 1-day device.
3. **Wake only on INT1.** MCU wakes, drains FIFO, classifies, sleeps. Typical wake is
   milliseconds.
4. **BLE connection interval as long as tolerable.** Long while idle; request a short one
   only while an alert is in flight. Never hold a fast interval "just in case."
5. **PDM mic powered down entirely** until Phase 7. It's the largest consumer on the board
   when active — not a background service.
6. **Gate the IMU rail (P1.08)** for shipping/storage mode. It must stay powered at runtime
   for always-on detection — this is not a runtime duty-cycling knob.

### Budget (all **Derived** — every figure must be replaced by measurement)

| Item | Est. avg current |
|---|---|
| nRF52840 System ON idle, RAM retained | ~2 µA |
| LSM6DS3TR-C low-power accel, ~52 Hz | ~40 µA |
| Gyro, duty-cycled on candidate events | ~1–5 µA equivalent |
| BLE advertising / long-interval connection | ~10–30 µA |
| **Subtotal** | **~55–80 µA** |
| Onboard regulator + charge IC quiescent | **? — measure this** |

With a 250 mAh Li-Po, ~70 µA implies months on paper. **Do not believe this number.**
The XIAO's onboard power path (LDO + BQ25101) has quiescent draw that community reports
put in the tens of µA, and it can easily dominate everything above. **Measuring actual
board sleep current is a Phase 0 task, not a Phase 6 task** — if it comes in at 200 µA
you need to know before designing an enclosure around a battery size.

The gyro line above assumes a low candidate-event rate. **That assumption is only valid
if the cascade's stage-1 gate is tight** — a wrist throwing 500 candidates/day would
change the picture. Measure the real rate in Phase 1 and revisit.

Nothing about timing, current, RF range or real sensor behaviour here is verifiable
without hardware. Everything above is spec-derived; the numbers that matter get measured
on-device.

---

## 8. Phased plan

Each phase has an explicit verify step. Don't advance without it.

| Phase | Work | Verify | Est. |
|---|---|---|---|
| **0. Bring-up** | Zephyr toolchain, `xiao_ble/nrf52840/sense` builds and flashes via UF2. Read IMU over I²C. Confirm INT1/P1.08 on schematic. **Measure sleep current.** | IMU streams; sleep current on a meter | 1 wk |
| **1. Data collection rig** | Log raw accel+gyro at full rate to QSPI; USB dump tool. Wrist-mounted collection: falls (crash mat, both arms, both landing sides) + many hours ADL incl. walking-aid use. | ≥ 50 falls + ≥ 30 h wrist ADL, labelled; candidate-event rate known | 2–3 wk |
| **2a. Offline cascade** | Cascade in Python, replay against corpus, tune thresholds. ROC curve. | Baseline sensitivity/specificity measured and recorded | 2 wk |
| **2b. Learned model** | Edge Impulse / small NN over accel+gyro window. Must beat 2a. | S1 + S2 met **offline**; model beats cascade baseline | 2 wk |
| **3. On-device detection** | Port to firmware. HW free-fall IRQ → gyro power-up → FIFO → classify. Buzzer, LED, cancel button, 30 s countdown. | Bench falls trigger; cancel works; on-device decisions match offline | 2 wk |
| **4. BLE + hub** | GATT service, bonding/encryption, store-and-forward on QSPI, retry-until-ack. Pi/ESP32 hub forwards to cloud. **BLE coverage survey of the actual home.** | Alert survives hub reboot + out-of-range; bathroom/bedroom covered | 2 wk |
| **5. Cloud + escalation** | Ingest, dedup by `event_id`, escalation ladder, caregiver app/SMS, heartbeat watchdog. | S3 (≤60 s) end-to-end timestamped; S6 fires | 2 wk |
| **6. Power + enclosure + trial** | Optimise to S4. Wrist enclosure + strap, battery sizing. **14-day wear trial on a real user.** | S4 measured; S2 confirmed in the field | 3+ wk |
| **7. Voice (deferred)** | PDM mic, keyword spotting, or post-fall audio confirmation. | TBD | later |

**Phases 1–2 are the project.** Phase 3 onward is comparatively mechanical. The common
failure mode is six weeks of beautiful firmware and BLE plumbing, then discovering the
detector fires every time the user leans on a walking frame — with no dataset to diagnose
it against.

---

## 9. Voice detection (Phase 7 — explicitly deferred)

Confirming your own priority call: keep this out of v1. Two plausible roles later —

- **Post-fall confirmation:** after `FALL_CONFIRMED`, open the mic and listen for speech
  or distress, or let the caregiver listen in. Real value; real privacy obligations.
- **Keyword spotting ("help"):** an always-listening path to raise an alert without a
  fall. Attractive, but always-on audio is a large continuous power draw and a much bigger
  privacy conversation.

Note it partially compensates for the wrist's weaker detection — a wearer who can call
for help covers some of what the accelerometer misses (including the slow slump). Worth
revisiting once S2 is measured.

Either way: an always-on mic in an elderly person's home needs an explicit consent and
data-handling story before a line of code.

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Wrist ⇒ weaker stages 3–4, more confounders** | S2 at risk | Gyro-on-demand (§5.1); classifier in v1; heavy wrist ADL corpus; cancel window |
| **Walking stick/frame mimics impact** | Chronic false alarms in exactly the target user | Explicit corpus category; validate separately |
| **In-home only** | Fall outdoors = no alert | Disclosed scope. v2 phone path, firmware unchanged |
| Hub power cut / WiFi down | Whole system offline | UPS on hub; cloud liveness watchdog (S6) |
| BLE dead spots (bathroom, bedroom) | Alert not delivered where falls happen | Coverage survey in Phase 4; store-and-forward on QSPI |
| Board quiescent current dominates budget | Misses S4 | Measure in Phase 0, before enclosure design |
| Slow slumps produce no free-fall | Missed falls | Documented v1 gap, or low-and-still detector |
| Device dies / taken off ⇒ silent failure | System silently useless | S6 heartbeat watchdog |
| ~~Wrong board variant (non-Sense)~~ | — | **Closed** — Board-ID confirms Sense |
| **RST button inert on the dev board** | Cannot reach UF2 bootloader ⇒ cannot reflash | `CONFIG_GPIO_AS_PINRESET=y` repairs UICR at boot; needs on-device confirmation. Add a software reboot-to-bootloader path before relying on it |
| No labelled data ⇒ untunable | Project stalls at Phase 2 | Phase 1 is not optional |

### Safety & regulatory

This is a **safety-adjacent** device. It is not a certified medical device and must not
be presented as a replacement for a professionally monitored medical alert system. Any
real deployment needs: explicit consent from the wearer, a clear statement of coverage
limits (**in-home only, v1**), and a documented fallback when the system fails. If it
goes commercial, medical-device regulation and liability need proper advice well outside
the scope of this document.

---

## 11. Explicit non-goals for v1

GPS / outdoor location · two-way voice · heart rate or other vitals · medical
certification · multi-user or multi-tenant cloud · phone app as gateway (v2) ·
voice detection · gait or activity analytics

---

## 12. Immediate next steps

1. **Confirm the board is the Sense variant** (IMU + mic present next to the antenna).
2. **Phase 0 bring-up:** Zephyr toolchain + `west flash -r uf2`, IMU over I²C, INT1/P1.08
   confirmed against the schematic.
3. **Measure real sleep current** before anything else is designed around it.
4. Write the repo `CLAUDE.md` Project Profile from §2 and scaffold the Zephyr tree.
