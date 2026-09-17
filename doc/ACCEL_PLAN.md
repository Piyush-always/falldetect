# Accelerometer Work Plan

**Status:** v0.1 — plan, not yet implemented.
**Date:** 2026-08-01
**Context:** closes Phase 0 and builds the Phase 1 data-collection rig from
[PROJECT_OUTLINE.md](PROJECT_OUTLINE.md).

---

## 1. Goal of this phase

Get **trustworthy, labelled wrist accelerometer + gyroscope data off the board and
onto disk.** Nothing here detects a fall. This phase exists so that Phase 2 has
something to tune against.

Restating why this is the critical path: at the wrist the detection problem is
dominated by data, not by algorithm cleverness. A beautiful detector tuned on
20 minutes of data will fire every time the user leans on a walking frame.

**Done when:** we can record an hour of wrist motion at a known sample rate, with no
dropped samples, in a file whose units and axis orientation are unambiguous.

---

## 2. What the SDK actually gives us (Verified against NCS v3.1.1 source)

The board's devicetree binds the LSM6DS3TR-C to Zephyr's **`st,lsm6dsl`** driver
(`xiao_ble_nrf52840_sense.dts:41`). The two parts share a register map, which is why
this works. What that driver does and does not do:

| Capability | Zephyr `lsm6dsl` driver | Needed for | Verdict |
|---|---|---|---|
| Read accel + gyro | ✅ sensor API | everything | **use it** |
| Runtime ODR / full-scale | ✅ `SENSOR_ATTR_SAMPLING_FREQUENCY` / `_FULL_SCALE` | tuning | **use it** |
| Data-ready trigger on INT1 | ✅ `SENSOR_TRIG_DATA_READY` | clean streaming | **use it** |
| WHO_AM_I validation | ✅ in `lsm6dsl_init_chip()`, fails bind on mismatch | bring-up | **use it** |
| **Free-fall interrupt** | ❌ `__ASSERT_NO_MSG(trig->type == SENSOR_TRIG_DATA_READY)` | power architecture | own code |
| **Wake-up / activity / inactivity** | ❌ | post-impact stillness | own code |
| **FIFO (4 KB, pre-trigger capture)** | ❌ | pre-impact data | own code |
| **Low-power modes** | ❌ | battery | own code |

The driver's private header `lsm6dsl.h` *does* define every register we would need
later — `FREE_FALL` 0x5D, `WAKE_UP_THS` 0x5B, `WAKE_UP_DUR` 0x5C, `MD1_CFG` 0x5E,
`TAP_CFG` 0x58, `FIFO_CTRL1–5` 0x06–0x0A, `FIFO_STATUS1–4` 0x3A–0x3D,
`FIFO_DATA_OUT_L/H` 0x3E/0x3F, `WAKE_UP_SRC` 0x1B (with the `FF_IA` bit).

That is a **private driver header**, not public API. When we need those registers we
define our own header with datasheet citations rather than reaching into the driver's
internals.

Also confirmed: `zephyr,console = &board_cdc_acm_uart` — **console is already on USB
CDC**, no overlay needed. `printk` reaches a COM port out of the box.

---

## 3. Architecture decision: staged, not all at once

The power architecture in PROJECT_OUTLINE §5.1/§7 (hardware free-fall interrupt, FIFO
pre-trigger, gyro-on-demand) needs raw register access the Zephyr driver will not give
us. That is real, and eventually we write a small purpose-built IMU layer.

**We do not write it now.** Two reasons:

1. **Phase 1 does not need it.** During data collection the device is tethered or
   charged daily. Power is irrelevant. What matters is getting clean data fast.
2. **Writing it now would be speculative.** We do not yet know the production ODR, the
   free-fall threshold, the FIFO depth, or the candidate-event rate — those are Phase 2
   outputs. Building a power-optimised driver against guessed parameters means building
   it twice.

So: **Zephyr sensor driver for Stages A–D. Own register layer at Stage E**, once the
data tells us what to build.

---

## 4. Sensor configuration — and why

These three choices decide whether the dataset is usable. Getting them wrong is not
recoverable by reprocessing; it means re-collecting.

### 4.1 Full-scale range: **±16 g** (accel), **±2000 dps** (gyro)

**This is the one that silently ruins datasets.** A wrist impact — especially landing
on the instrumented arm — comfortably exceeds ±8 g. When the sensor clips, the peak is
flattened, and *the clipped sample looks like perfectly valid data*. You destroy the
exact feature you are trying to detect and there is no way to tell after the fact.

At ±16 g with 16-bit output, resolution is ~0.488 mg/LSB — far finer than anything
fall detection needs. **There is no meaningful cost to the widest range**, and an
unrecoverable cost to being too narrow. Same argument for gyro: wrist rotation during a
fall can exceed 1000 dps, so ±2000 dps.

### 4.2 Output data rate: **208 Hz for collection**

Principle: **you can always downsample offline; you can never upsample.** Fall impacts
are sharp transients — at 52 Hz you risk aliasing the peak and under-measuring impact
magnitude.

208 Hz costs nothing during tethered collection. The *production* ODR (likely 52–104 Hz
for battery) gets chosen in Phase 2 by decimating the collected data and measuring where
detection performance actually degrades. That is a measurement, not a guess.

> Note: with `CONFIG_LSM6DSL_ACCEL_ODR=0` (the default) the rate is runtime-selected,
> so the app **must** set it explicitly at startup or the sensor may stay powered down.
> Verify samples actually change before trusting anything.

### 4.3 Units and log format

Zephyr's API returns m/s² and rad/s as `struct sensor_value`. For the log we convert to
compact integers with the scale fixed and documented at the boundary:

| Field | Type | Unit | Range |
|---|---|---|---|
| `seq` | `uint32` | sample counter | ~239 days at 208 Hz |
| `ax, ay, az` | `int16` | **milli-g** | ±16000 |
| `gx, gy, gz` | `int16` | **deci-dps** | ±20000 |

Both fit `int16` at the chosen full scales with headroom. No floating point in the
logging path.

`seq` is not decoration — it is how we **detect dropped samples**. A gap in the counter
turns a silent data-quality failure into a visible one. Every capture gets checked for
gaps before it is allowed into the dataset.

### 4.4 Axis convention — do this before collecting anything

Define, once, which physical direction each of `+x/+y/+z` points when the device is worn,
and record it in the dataset README with a photo.

Skipping this is the classic own-goal: six months later nobody can say whether `+z` was
towards the back of the hand or the palm, and the entire corpus becomes uninterpretable.
Stage C exists solely to nail this down.

---

## 5. Staged delivery

Each stage has a verify step. No stage starts before the previous one passes.

### Stage A — `apps/imu_probe` (small)
Console up, IMU bound, one sample printed.

- `device_is_ready()` on the `lsm6ds3tr_c` node — this *is* the WHO_AM_I check
- Set ODR and full-scale explicitly, read back one accel + gyro sample
- Print identity and the sample

**Verify:** board enumerates a COM port; at rest, total acceleration magnitude reads
≈ 1000 mg, concentrated on whichever axis faces down. Gyro ≈ 0 when still.

*This also proves the P1.08 power rail and the I²C bus — if the rail were dead the
driver would not bind.*

### Stage B — `apps/imu_stream`
Continuous CSV over USB CDC at 208 Hz.

- `SENSOR_TRIG_DATA_READY` on INT1 (P0.11), using **`CONFIG_LSM6DSL_TRIGGER_OWN_THREAD`**
  so our callback never blocks the system workqueue
- Trigger callback pushes into a **ring buffer**; a separate thread drains it to the
  console. The callback must not print — `printk` over CDC blocks when the host is not
  draining, and blocking in the callback drops samples silently
- Every line carries `seq`

**Verify:** sustained 208 lines/s for 60 s with **zero counter gaps**; tilting the board
moves gravity smoothly between axes.

### Stage C — orientation + sanity check
Establish the axis convention (§4.4).

- Six-position check: each axis pointed down in turn should read ≈ +1000 mg on that axis
  and ≈ 0 on the others
- Record the wrist-worn orientation with a photo into the dataset README

**Verify:** all six positions within a few percent of ±1000 mg. Large deviation means a
mounting or scaling error — find it now, not after 30 hours of collection.

### Stage D — `apps/imu_logger` (Phase 1 proper)
Untethered logging to the 2 MB QSPI flash + host-side dump tool.

Needed because 30 hours of ADL data cannot be collected on a USB cable. Capacity maths,
wear, and session framing get worked out when we start this stage — it is the largest
piece here and deserves its own design pass.

### Stage E — free-fall interrupt, FIFO, gyro-on-demand
The power architecture. **Deferred until Phase 2 tells us the parameters.** This is where
the custom register layer gets written.

---

## 6. Host-side tooling

A capture script (`tools/capture.ps1`) that opens the COM port, records CSV to a
timestamped file, and **verifies `seq` continuity on the way in**, refusing to silently
accept a gapped capture.

Labelling matters as much as capture: each recording needs an activity label
(`walk`, `stairs_down`, `sit_heavy`, `walking_frame`, `fall_forward`, …). Simplest
workable scheme is one file per activity per session, with the label in the filename.

---

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Clipping at too-low full scale** | Dataset silently unusable | ±16 g / ±2000 dps (§4.1) |
| **Dropped samples via blocking console** | Silent gaps in data | ring buffer + `seq` gap check (§5 Stage B) |
| Aliased impact peaks | Under-measured impacts | 208 Hz collection ODR |
| Unknown axis convention | Corpus uninterpretable later | Stage C before any collection |
| ODR left runtime-unset | Sensor stays powered down | Set explicitly, verify samples change |
| QSPI capacity / wear | Short sessions, lost data | Sized properly at Stage D |

---

## 8. Open questions

1. **Wrist mounting for collection** — strap, tape, or a printed holder? It must be
   repeatable, or Stage C's convention drifts between sessions.
2. **Who performs simulated falls?** Crash mat + healthy volunteers only; never elderly
   participants (PROJECT_OUTLINE §5.5).
3. **Both wrists, both landing sides?** Recommended — the instrumented-arm vs opposite-arm
   cases look completely different.

None of these block Stage A.

---

## 9. Immediate next step

Build **Stage A** (`apps/imu_probe`). It is small, and it independently proves the I²C
bus, the P1.08 sensor rail, the driver binding, the WHO_AM_I, and the USB CDC console —
five things that everything downstream depends on.
