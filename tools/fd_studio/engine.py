"""
Detection engine: posture, activity, and the fall cascade.

Deliberately pure Python + numpy with no Qt import, so it can be unit-tested and
replayed against recorded CSV without a GUI. That matters: the same code must
later be ported to firmware and proven to agree, and you cannot diff a detector
that only exists inside a window.

WHY DETECTION LIVES HERE, FOR NOW
---------------------------------
The shipping device must detect on-board — the wearable cannot depend on a PC.
But thresholds tuned by guesswork are worthless, and every firmware iteration
costs a reflash and a double-tap. So the detector is prototyped here against the
live stream, tuned until it behaves, and only then ported. See ACCEL_PLAN.md.

CO-ORDINATE FRAME
-----------------
The device can be mounted at any rotation, so no axis has a fixed meaning. Every
posture decision is made against a CALIBRATED reference: the user stands upright
and still, and the current gravity vector is recorded as "up". Tilt is then the
angle between live gravity and that reference. Without calibration the engine
reports UNKNOWN rather than guessing — a pendant that has rotated on its cord
would otherwise silently report the wearer as lying down.

UNITS
-----
Acceleration milli-g (1000 = 1 g), angular rate deci-dps, time seconds.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, asdict
from enum import Enum

import numpy as np

G_MG = 1000.0


class Posture(str, Enum):
    UNKNOWN = "UNKNOWN"
    UPRIGHT = "UPRIGHT"
    RECLINED = "RECLINED"
    LYING = "LYING"


class Activity(str, Enum):
    UNKNOWN = "UNKNOWN"
    STILL = "STILL"
    WALKING = "WALKING"
    ACTIVE = "ACTIVE"


class Stage(str, Enum):
    IDLE = "IDLE"
    FREEFALL = "FREE-FALL"
    IMPACT = "IMPACT"
    ASSESS = "ASSESSING"
    CONFIRMED = "FALL"
    REJECTED = "REJECTED"


@dataclass
class Thresholds:
    """Literature starting points, NOT tuned values.

    Every one of these is provisional until measured against a real corpus for
    the actual mount point. They are runtime-editable for exactly that reason.
    """

    # stage 1 — free-fall
    freefall_mg: int = 500
    freefall_min_ms: int = 80
    # stage 2 — impact
    impact_mg: int = 2500
    impact_window_ms: int = 800
    # a fall with no clean free-fall phase still counts if the impact is hard
    hard_impact_mg: int = 3500
    # stage 3 — orientation change
    orientation_deg: int = 45
    # stage 4 — post-impact stillness
    settle_ms: int = 500
    still_window_ms: int = 3000
    still_std_mg: int = 150
    # posture
    upright_max_deg: int = 40
    lying_min_deg: int = 65
    # activity
    motion_std_mg: int = 90
    walk_steps_per_s: float = 0.6
    # do not re-trigger on the aftermath of one fall
    refractory_s: float = 8.0


@dataclass
class FallEvent:
    t: float
    confirmed: bool
    freefall: bool
    impact: bool
    orientation: bool
    stillness: bool
    peak_mg: int
    min_mg: int
    tilt_before: float
    tilt_after: float
    still_std: float
    trace: list = field(default_factory=list)  # (t, mag, tilt) around the event

    def reason(self) -> str:
        if self.confirmed:
            return "confirmed"
        missing = []
        if not self.impact:
            missing.append("no impact")
        if not self.orientation and not self.stillness:
            missing.append("no orientation change or stillness")
        return "rejected — " + (", ".join(missing) or "incomplete")


class Engine:
    def __init__(self, odr_hz: int = 208, thresholds: Thresholds | None = None):
        self.odr = odr_hz
        self.th = thresholds or Thresholds()

        # Gravity estimate: a ~0.5 s low pass. Fast enough to follow a posture
        # change, slow enough that walking bounce does not move it.
        self._alpha = math.exp(-(1.0 / odr_hz) / 0.5)
        self._g = np.array([0.0, 0.0, G_MG])
        self.g_ref: np.ndarray | None = None

        self.mag = 0.0
        self.tilt = 0.0
        self.std = 0.0
        self.last_raw = (0, 0, 0, 0, 0, 0)
        self.posture = Posture.UNKNOWN
        self.activity = Activity.UNKNOWN
        self.stage = Stage.IDLE

        self._mag_win = deque(maxlen=odr_hz)          # 1 s for motion variance
        self.trace = deque(maxlen=odr_hz * 12)        # 12 s for the plot
        # RAW accel vectors, for calibration. Deliberately not the smoothed
        # _g: that is an EMA with a 0.5 s time constant, so it is quiet by
        # construction. Judging steadiness from it measures the filter rather
        # than the person - a slow lean, which is exactly what ruins a
        # calibration, is the part the filter removes best.
        self._recent_g = deque(maxlen=odr_hz)         # 1 s for calibration

        self.steps = 0
        self._step_hist = deque(maxlen=8)             # (t, steps)
        self.step_rate = 0.0

        # posture dwell times, seconds
        self.dwell = {p: 0.0 for p in Posture}
        self._last_t: float | None = None

        # Gyro-integrated heading. Honest caveat: this drifts, because it is a
        # pure integration with nothing to correct it — there is no
        # magnetometer on this part. Useful for making the view feel real,
        # never for a decision. heading_age says how stale it is.
        self.heading = 0.0
        self.heading_age = 0.0

        # Peak hold, for "what did it actually see" after a throw.
        self.peak_mag = 0.0
        self.peak_tilt = 0.0

        self.events: list[FallEvent] = []
        self._reset_candidate()
        self._last_fall_t = -1e9

        # live cascade flags, for the stepper display
        self.flags = {"freefall": False, "impact": False,
                      "orientation": False, "stillness": False}

    # ── calibration ──────────────────────────────────────────────────────────
    @property
    def calibrated(self) -> bool:
        return self.g_ref is not None

    #: How far the raw accel vector may wander over 1 s, in milli-g, and still
    #: count as "standing still".
    #:
    #: Measured from data/neck (2026-09-20): a device at rest sits at 5-10 mg,
    #: genuine walking runs 150-250 mg. 35 mg is several times resting noise
    #: and several times below walking, so nothing delicate rests on the exact
    #: value. Synthetic leans put a +/-4 deg sway at ~19 mg (accepted) and
    #: +/-12 deg at ~56 mg (rejected), which is roughly the line we want:
    #: a person can breathe and shift their weight, but not sway.
    CAL_MAX_SPREAD_MG = 35.0

    def calibration_check(self) -> tuple[bool, str, float]:
        """Could we calibrate right now, why not, and how steady are we?

        Returns (ready, reason, steadiness 0..1). Split out from
        calibrate_upright so the UI can show this LIVE, before the user
        commits - "hold still, nearly there" is useful, "calibration failed"
        after the fact is not.
        """
        if len(self._recent_g) < self.odr // 2:
            return False, "Waiting for data from the device...", 0.0

        arr = np.array(self._recent_g)
        mean = arr.mean(axis=0)
        norm = float(np.linalg.norm(mean))

        if norm < 700.0 or norm > 1300.0:
            # Mean magnitude far from 1 g means real acceleration on top of
            # gravity - they are moving, not standing.
            return False, "Too much movement — stand still and upright", 0.0

        # THE CHECK THAT WAS MISSING. Mean magnitude alone passes happily
        # while someone sways: the vector wanders but its average length stays
        # near 1 g, so a direction averaged over the sway gets locked in and
        # every later tilt reading is measured from a reference that was never
        # true. Spread catches that; magnitude cannot.
        spread = float(np.linalg.norm(arr - mean, axis=1).mean())
        steadiness = max(0.0, min(1.0, 1.0 - spread / self.CAL_MAX_SPREAD_MG))

        if spread > self.CAL_MAX_SPREAD_MG:
            return False, "Hold still — you are swaying", steadiness

        return True, "Ready", steadiness

    def calibrate_upright(self) -> bool:
        """Capture the current gravity direction as 'upright'.

        Refuses unless calibration_check() passes, so a reference is never
        taken mid-stride or mid-sway.
        """
        ready, _reason, _steady = self.calibration_check()
        if not ready:
            return False

        arr = np.array(self._recent_g)
        mean = arr.mean(axis=0)
        self.g_ref = mean / float(np.linalg.norm(mean))
        return True

    def clear_calibration(self) -> None:
        self.g_ref = None
        self.posture = Posture.UNKNOWN

    # ── orientation ──────────────────────────────────────────────────────────
    def rotation(self) -> np.ndarray | None:
        """3x3 rotation carrying the calibrated 'upright' orientation to the
        current one.

        Derived from gravity alone, so only two of three degrees of freedom are
        observable: rotating about the vertical axis does not move the gravity
        vector, and this part has no magnetometer to recover heading from. The
        returned rotation therefore has zero yaw by construction — the device
        will appear not to turn when you spin on the spot, which is honest
        rather than broken. Posture does not depend on heading.
        """
        if self.g_ref is None:
            return None
        n = np.linalg.norm(self._g)
        if n < 1.0:
            return None
        b = self._g / n
        a = self.g_ref

        v = np.cross(a, b)
        c = float(np.dot(a, b))
        s = float(np.linalg.norm(v))

        if s < 1e-8:
            # Parallel, or exactly inverted. Inverted needs a 180 deg turn about
            # any perpendicular axis; picking one arbitrarily is fine because
            # yaw is unobservable anyway.
            if c > 0:
                return np.eye(3)
            perp = np.array([1.0, 0.0, 0.0])
            if abs(a[0]) > 0.9:
                perp = np.array([0.0, 1.0, 0.0])
            axis = np.cross(a, perp)
            axis /= np.linalg.norm(axis)
            K = np.array([[0, -axis[2], axis[1]],
                          [axis[2], 0, -axis[0]],
                          [-axis[1], axis[0], 0]])
            return np.eye(3) + 2.0 * (K @ K)

        K = np.array([[0, -v[2], v[1]],
                      [v[2], 0, -v[0]],
                      [-v[1], v[0], 0]])
        r_tilt = np.eye(3) + K + K @ K * ((1.0 - c) / (s * s))
        return self._apply_heading(r_tilt)

    def _apply_heading(self, r_tilt: np.ndarray) -> np.ndarray:
        """Compose the measured tilt with the integrated heading.

        Heading is a rotation about the reference vertical, applied after tilt,
        because that is what turning on the spot does in the real world.
        """
        if abs(self.heading) < 1e-6 or self.g_ref is None:
            return r_tilt
        ax = self.g_ref
        th = math.radians(self.heading)
        K = np.array([[0, -ax[2], ax[1]],
                      [ax[2], 0, -ax[0]],
                      [-ax[1], ax[0], 0]])
        r_yaw = np.eye(3) + math.sin(th) * K + (1.0 - math.cos(th)) * (K @ K)
        return r_yaw @ r_tilt

    def tilt_components(self) -> tuple[float, float]:
        """Tilt split into two orthogonal lean angles, in the DEVICE frame.

        Which one is 'forward' depends on how the device is mounted — there is
        no way to know that without a second calibration gesture — so these are
        labelled by device axis rather than by body direction.
        """
        if self.g_ref is None:
            return 0.0, 0.0
        n = np.linalg.norm(self._g)
        if n < 1.0:
            return 0.0, 0.0
        b = self._g / n
        d = b - self.g_ref * float(np.dot(b, self.g_ref))
        return math.degrees(math.asin(max(-1.0, min(1.0, d[0])))), \
            math.degrees(math.asin(max(-1.0, min(1.0, d[1]))))

    # ── ingest ───────────────────────────────────────────────────────────────
    def push_steps(self, steps: int, t: float) -> None:
        self.steps = steps
        self._step_hist.append((t, steps))
        if len(self._step_hist) >= 2:
            t0, s0 = self._step_hist[0]
            dt = t - t0
            self.step_rate = ((steps - s0) / dt) if dt > 0.5 else self.step_rate

    def push_sample(self, ax: int, ay: int, az: int,
                    gx: int, gy: int, gz: int, t: float) -> FallEvent | None:
        self.last_raw = (ax, ay, az, gx, gy, gz)
        a = np.array([float(ax), float(ay), float(az)])
        self.mag = float(np.linalg.norm(a))

        self._g = self._alpha * self._g + (1.0 - self._alpha) * a
        self._recent_g.append(a)

        if self.g_ref is not None:
            gn = np.linalg.norm(self._g)
            if gn > 1.0:
                cosang = float(np.dot(self._g / gn, self.g_ref))
                self.tilt = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))

        self._mag_win.append(self.mag)
        if len(self._mag_win) >= 16:
            self.std = float(np.std(self._mag_win))

        self.trace.append((t, self.mag, self.tilt))

        self.peak_mag = max(self.peak_mag, self.mag)
        self.peak_tilt = max(self.peak_tilt, self.tilt)

        dt = (t - self._last_t) if self._last_t is not None else 0.0
        if 0.0 < dt < 1.0:
            self._integrate_heading(gx, gy, gz, dt)
            self._classify(dt)
        self._last_t = t

        return self._cascade(t)

    def _integrate_heading(self, gx: int, gy: int, gz: int, dt: float) -> None:
        """Rotation rate about the gravity axis, integrated.

        Only the component along gravity contributes to heading; the rest is
        tilt, which we already get from the accelerometer far more reliably.
        Gyro is deci-dps, so /10 for dps.
        """
        if self.g_ref is None:
            return
        n = np.linalg.norm(self._g)
        if n < 1.0:
            return
        w = np.array([gx, gy, gz], dtype=float) / 10.0
        self.heading += float(np.dot(w, self._g / n)) * dt
        self.heading = (self.heading + 180.0) % 360.0 - 180.0
        self.heading_age += dt

    def reset_heading(self) -> None:
        self.heading = 0.0
        self.heading_age = 0.0

    def reset_peaks(self) -> None:
        self.peak_mag = self.mag
        self.peak_tilt = self.tilt

    # ── posture / activity ───────────────────────────────────────────────────
    def _classify(self, dt: float) -> None:
        if self.g_ref is None:
            self.posture = Posture.UNKNOWN
            self.activity = Activity.UNKNOWN
            return

        if self.tilt < self.th.upright_max_deg:
            self.posture = Posture.UPRIGHT
        elif self.tilt >= self.th.lying_min_deg:
            self.posture = Posture.LYING
        else:
            self.posture = Posture.RECLINED

        # Walking is taken from the IMU's own step counter rather than inferred
        # from the waveform: the pedometer is a hardware block that already
        # solved this, and it does not need training data.
        if self.step_rate >= self.th.walk_steps_per_s and self.posture != Posture.LYING:
            self.activity = Activity.WALKING
        elif self.std < self.th.motion_std_mg:
            self.activity = Activity.STILL
        else:
            self.activity = Activity.ACTIVE

        if 0 < dt < 1.0:
            self.dwell[self.posture] += dt

    # ── fall cascade ─────────────────────────────────────────────────────────
    def _reset_candidate(self) -> None:
        self._ff_run = 0
        self._t_ff = 0.0
        self._t_impact = 0.0
        self._tilt_before = 0.0
        self._peak = 0.0
        self._min = 1e9
        self._still_samples: list[float] = []
        self._cand_ff = False

    def _set_flags(self, **kw) -> None:
        self.flags.update(kw)

    def _cascade(self, t: float) -> FallEvent | None:
        th = self.th
        ff_needed = max(1, int(th.freefall_min_ms * self.odr / 1000))

        if self.stage in (Stage.IDLE, Stage.CONFIRMED, Stage.REJECTED):
            if t - self._last_fall_t < th.refractory_s:
                return None
            if self.stage in (Stage.CONFIRMED, Stage.REJECTED):
                self.stage = Stage.IDLE
                self._set_flags(freefall=False, impact=False,
                                orientation=False, stillness=False)

            # stage 1: sustained low magnitude
            if self.mag < th.freefall_mg:
                self._ff_run += 1
                if self._ff_run >= ff_needed:
                    self._reset_candidate()
                    self._cand_ff = True
                    self._t_ff = t
                    self._tilt_before = self.tilt
                    self._min = self.mag
                    self.stage = Stage.FREEFALL
                    self._set_flags(freefall=True)
            else:
                self._ff_run = 0
                # A hard impact with no free-fall phase still deserves
                # assessment: not every fall produces a clean weightless window,
                # and requiring one caps sensitivity at whatever stage 1 sees.
                if self.mag > th.hard_impact_mg:
                    self._reset_candidate()
                    self._cand_ff = False
                    self._tilt_before = self.tilt
                    self._peak = self.mag
                    self._t_impact = t
                    self.stage = Stage.ASSESS
                    self._set_flags(freefall=False, impact=True)
            return None

        if self.stage is Stage.FREEFALL:
            self._min = min(self._min, self.mag)
            if self.mag > th.impact_mg:
                self._peak = self.mag
                self._t_impact = t
                self.stage = Stage.ASSESS
                self._set_flags(impact=True)
            elif (t - self._t_ff) * 1000.0 > th.impact_window_ms:
                self.stage = Stage.REJECTED
                self._last_fall_t = t - th.refractory_s + 1.0
                return self._finish(t, confirmed=False)
            return None

        if self.stage is Stage.ASSESS:
            self._peak = max(self._peak, self.mag)
            since = (t - self._t_impact) * 1000.0
            if since >= th.settle_ms:
                self._still_samples.append(self.mag)
            if since >= th.still_window_ms:
                return self._finish(t, confirmed=None)
            return None

        return None

    def _finish(self, t: float, confirmed) -> FallEvent:
        th = self.th
        still_std = float(np.std(self._still_samples)) if self._still_samples else 999.0
        orientation = abs(self.tilt - self._tilt_before) >= th.orientation_deg
        stillness = still_std < th.still_std_mg
        impact = self._peak >= th.impact_mg

        if confirmed is None:
            # Impact is mandatory. Beyond that, either a sustained orientation
            # change or post-impact stillness is enough — requiring both misses
            # falls where the wearer keeps moving, or lands upright-ish.
            confirmed = impact and (orientation or stillness)

        # Never confirm without a gravity reference. Uncalibrated, tilt is
        # permanently 0, so the orientation stage cannot fire and the verdict
        # collapses to "impact, then stillness" - which is what putting the
        # device down on a table looks like. That produced a sounding alarm on
        # a screen that was simultaneously saying "Set up needed", with no
        # visible way to silence it.
        #
        # The candidate is still recorded, so the Debug log shows what was seen
        # and why it was not acted on.
        if confirmed and not self.calibrated:
            confirmed = False

        if confirmed:
            self._set_flags(orientation=orientation, stillness=stillness)
            self.stage = Stage.CONFIRMED
            self._last_fall_t = t
        else:
            self._set_flags(orientation=orientation, stillness=stillness)
            self.stage = Stage.REJECTED
            self._last_fall_t = t - th.refractory_s + 2.0

        ev = FallEvent(
            t=t,
            confirmed=bool(confirmed),
            freefall=self._cand_ff,
            impact=impact,
            orientation=orientation,
            stillness=stillness,
            peak_mg=int(self._peak),
            min_mg=int(self._min if self._min < 1e8 else 0),
            tilt_before=self._tilt_before,
            tilt_after=self.tilt,
            still_std=still_std,
            trace=list(self.trace),
        )
        self.events.append(ev)
        self._reset_candidate()
        return ev

    # ── output ───────────────────────────────────────────────────────────────
    def state_text(self) -> str:
        if self.stage is Stage.CONFIRMED:
            return "FALL DETECTED"
        if not self.calibrated:
            return "NOT CALIBRATED"
        if self.posture is Posture.LYING:
            return "LYING" if self.activity is Activity.STILL else "LYING / MOVING"
        if self.activity is Activity.WALKING:
            return "WALKING"
        if self.posture is Posture.RECLINED:
            return "RECLINED"
        return "UPRIGHT / STILL" if self.activity is Activity.STILL else "UPRIGHT / ACTIVE"

    def snapshot(self) -> dict:
        return {
            "mag": int(self.mag),
            "tilt": round(self.tilt, 1),
            "std": int(self.std),
            "posture": self.posture.value,
            "activity": self.activity.value,
            "stage": self.stage.value,
            "steps": self.steps,
            "step_rate": round(self.step_rate, 2),
            "calibrated": self.calibrated,
            "dwell": {k.value: round(v, 1) for k, v in self.dwell.items()},
            "thresholds": asdict(self.th),
        }
