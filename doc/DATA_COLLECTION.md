# Data Collection & Calibration — the actual steps

Worn at the **neck**. Follow this in order; each step exists because skipping it
produces data you have to throw away.

---

## First, one thing to be clear about

There is **no machine learning here yet**, so nothing is "trained". What you are
doing is **tuning thresholds** — the detector already knows how to measure tilt,
stillness and impact, but the numbers that separate "sitting" from "lying", or
"sat down hard" from "fell", have to be set for *your* body and *your* mount
point.

So the loop is not *record → train → hope*. It is:

```
record labelled sessions  ->  see what the detector said  ->  adjust a number
        ^                                                            |
        +------------------------------------------------------------+
```

You will see the effect of a change in seconds, not after a retrain.

---

## Step 1 — Calibrate (once per session, every session)

**This is not optional.** The detector measures tilt against a reference
direction for "upright". With no reference it reports UNKNOWN and the
orientation stage can never fire.

1. Put the device on. Same position you will wear it in — a pendant that hangs
   differently gives a different reference.
2. Open FD Studio → **Scan** → pick `falldetect-datalog` → **Connect**.
3. **Stand up straight and still.** Not walking, not leaning.
4. Click **Calibrate upright**, then keep standing. It holds you there for
   **three seconds**, counting down on screen, and takes the reference only
   after the whole three seconds were steady. Wobble and the count restarts —
   you do not need to press anything again, just settle.
5. Confirm the Debug tab shows tilt near **0°** while you stand, and that it
   rises when you lean forward.

The hold is the point. A single-instant capture will happily lock in a
reference taken mid-sway: the gravity vector still averages to about 1 g, so
it looks valid, and every tilt reading afterwards is measured from a direction
you were never actually standing in. Measured on this device, standing still
reads 5–10 mg of wander and walking reads 150–250 mg; the gate sits at 35 mg,
so breathing and shifting your weight are fine and swaying is not.

The reported **steadiness** is how still you were, not how correct the result
is. 100% on a device lying on a desk is a perfect calibration of a desk.

Re-calibrate if you take the device off and put it back on. The cord twists.

---

## Step 2 — Record, using the button

Press the **button on the device** to start recording, press again to stop.
That exists so you do not have to reach the laptop mid-activity — reaching for
a keyboard while falling onto a mattress puts the reach in the recording, not
the fall.

Before each set, set the label in the Debug tab's inspector:

| Field | Set to |
|---|---|
| Subject | `s01` (you). Change per person. |
| Mount | `neck` |
| Activity | pick from the list — this becomes the folder name |

Files land in `data/neck/<activity>/<timestamp>_<subject>_<activity>.csv`, so
**the label is the folder**. Nothing extra to track.

### What to record — confounders FIRST

Falls are the easy half. What decides whether this product works is whether
ordinary movement sets it off, so record that first and in volume.

| Order | Activity | Reps | Each | Why |
|---|---|---|---|---|
| 1 | `standing` | 5 | 15 s | Posture reference |
| 2 | `sitting` | 5 | 15 s | Posture reference |
| 3 | `lying` | 5 | 15 s | Posture reference — the one a fall must be told apart from |
| 4 | `walking` | 10 | 20 s | Most of real life |
| 5 | `stand_to_sit` | 10 | — | Transitions look fall-shaped |
| 6 | `sit_to_stand` | 10 | — | |
| 7 | `sitting_down_heavily` | 10 | — | **SisFall's number one false alarm.** Do not skip. |
| 8 | `bending_picking_up` | 10 | — | Big tilt change, no fall |
| 9 | `stairs_down` | 5 | — | Also flagged false in SisFall |
| 10 | `fall_forward` | 10 | — | Onto the bed |
| 11 | `fall_backward` | 10 | — | |
| 12 | `fall_left` / `fall_right` | 5 each | — | |
| 13 | `fall_slow_slump` | 10 | — | Slide down a wall. The known weak case. |

Rows 5–9 are the ones that make or break it. A detector that catches every fall
and also fires when you sit down is worse than useless — it gets taken off.

### Sanity check each recording

After Stop, the status strip shows samples and **gaps**. Gaps should be `0`.
A recording with gaps has holes in it, and a corpus with holes produces a
detector with holes.

---

## Step 3 — Check what the detector saw

Debug tab → inspector → **Check my recordings**.

For every session it prints the label you gave it next to what the engine
actually reported:

```
sitting                -> RECLINED 88% / STILL 94%
lying                  -> LYING 96% / STILL 91%
walking                -> UPRIGHT 92% / WALKING 71%
sitting_down_heavily   -> UPRIGHT 78% / ACTIVE 64%  falls=1     <-- false alarm
```

Read it as: **left is what you did, right is what it thought.** Where they
disagree, that is the thing to fix.

---

## Step 4 — Adjust one number, check again

The thresholds are sliders in the same panel. Change **one at a time**, then
press *Check my recordings* again.

| Symptom | Adjust |
|---|---|
| Sitting reported as LYING | Raise **Lying above** (deg) |
| Sitting reported as UPRIGHT | Lower **Upright below** (deg) |
| Standing still reported as moving | Raise **Moving above** (mg) |
| Walking not detected as walking | Lower the step-rate expectation |
| `sitting_down_heavily` fires a fall | Raise **Impact above**, or raise **Hard impact** |
| Real falls missed | Lower **Impact above**, or widen **Impact within** |

**Change one thing, re-check, keep or revert.** Changing three at once means
you cannot tell which one helped.

---

## Step 5 — Score the falls

**Replay recorded corpus** gives the two numbers that actually matter:

- **Sensitivity** — what fraction of real falls were caught
- **Specificity** — what fraction of ordinary activity was correctly ignored

For reference, against the public SisFall dataset with untuned literature
defaults this detector scored **89.6% / 93.1%**. Your own neck data should beat
that once tuned, because it is your body and your mount point.

---

## The one trap to avoid

**Do not tune until the numbers are perfect.** If you keep adjusting until every
one of your own recordings is classified correctly, you have memorised your own
data, not learned to detect falls. It will then do noticeably worse on anyone
else — including in a demo.

Guard against it: record **3 extra reps of each activity and do not tune on
them**. Tune on the rest, then check those held-back ones. If they score about
the same, the thresholds are real. If they score much worse, you over-tuned —
back the changes off.

---

## Why not store on the device?

The board has 2 MB of QSPI flash, unused. It stays unused for this: you are
sitting next to the laptop, BLE carries the full 208 Hz with zero dropped
samples, and the PC has the disk and the tools. On-device logging earns its
complexity when the device is away from a host for hours — a 15-second labelled
trial is not that.
