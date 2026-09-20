"""
Offline replay of recorded sessions through Engine.

This is ACCEL_PLAN.md Phase 2a: "Cascade in Python, replay against corpus,
tune thresholds. ROC curve." Deliberately no Qt import, same reasoning as
engine.py — must be usable headless (a script, a notebook, CI) and not only
from inside a window.

Every recorded CSV's parent folder name is a label (see app.py's
start_recording: data/<mount>/<label>/<stamp>_<subject>_<label>.csv), so the
ground truth for scoring comes from the folder structure the recordings were
already saved into — nothing extra to track.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import sisfall as sisfall_mod
from .engine import Engine, FallEvent, Thresholds

CSV_HEADER_PREFIX = "seq,"



def calibrate_from_quiet(engine: Engine,
                         samples: list[tuple[int, int, int, int, int, int]],
                         search_s: float = 5.0) -> bool:
    """Set the engine's 'upright' reference from the stillest early window.

    WHY THIS EXISTS: Engine.tilt is measured against a calibrated gravity
    reference (g_ref). Live, the user stands still and presses Calibrate.
    A recording has no such moment, and without g_ref the engine reports
    posture UNKNOWN and leaves tilt at 0 forever - so the cascade's
    orientation-change stage can NEVER fire and every replay silently scores a
    crippled detector. Earlier replays in this project did exactly that.

    Picks the lowest-variance half-second in the first `search_s`, which for
    these corpora is someone standing before they start moving. Refuses if the
    window is not close to 1 g: a reference captured mid-stride is worse than
    none, because it looks calibrated while being wrong.
    """
    if not samples:
        return False

    win = max(8, engine.odr // 2)
    horizon = min(len(samples), int(search_s * engine.odr))
    if horizon < win:
        win, horizon = len(samples), len(samples)

    acc = np.array([s[:3] for s in samples[:horizon]], dtype=float)
    if len(acc) < win:
        return False

    mag = np.linalg.norm(acc, axis=1)
    # Variance of magnitude over each candidate window, via a rolling mean.
    csum = np.cumsum(np.insert(mag, 0, 0.0))
    csum2 = np.cumsum(np.insert(mag * mag, 0, 0.0))
    n = len(mag) - win + 1
    means = (csum[win:] - csum[:-win]) / win
    means2 = (csum2[win:] - csum2[:-win]) / win
    var = means2 - means * means

    best = int(np.argmin(var[:n]))
    ref = acc[best:best + win].mean(axis=0)
    norm = float(np.linalg.norm(ref))
    if norm < 700.0 or norm > 1300.0:
        return False  # not ~1 g: never still in the search window

    engine.g_ref = ref / norm
    return True


def run_samples(samples: list[tuple[int, int, int, int, int, int]],
                thresholds: Thresholds | None = None,
                odr_hz: int = 208) -> tuple[Engine, bool]:
    """Replay samples through a fresh Engine at nominal ODR spacing.

    Calibration happens BEFORE any sample is pushed, because the cascade's
    orientation stage compares tilt-before against tilt-after and both are
    meaningless without a gravity reference.
    """
    engine = Engine(odr_hz=odr_hz, thresholds=thresholds)
    calibrated = calibrate_from_quiet(engine, samples)

    t = 0.0
    dt = 1.0 / odr_hz
    for ax, ay, az, gx, gy, gz in samples:
        t += dt
        engine.push_sample(ax, ay, az, gx, gy, gz, t)
    return engine, calibrated


@dataclass
class SessionResult:
    path: Path
    mount: str
    label: str
    samples: int
    events: list[FallEvent]

    @property
    def confirmed(self) -> int:
        return sum(1 for e in self.events if e.confirmed)

    @property
    def is_fall_label(self) -> bool:
        return self.label.startswith("fall")

    @property
    def outcome(self) -> str:
        hit = self.confirmed > 0
        if self.is_fall_label:
            return "TP" if hit else "FN"
        return "FP" if hit else "TN"


def replay_csv(path: Path, thresholds: Thresholds | None = None,
                odr_hz: int = 208) -> SessionResult:
    """Feed one recorded CSV through a fresh Engine at nominal ODR spacing.

    link.py's writer records seq but not a wall-clock timestamp, so replay
    assumes constant 1/odr spacing between rows — the same spacing the device
    itself samples at. A capture with real dropped-sample gaps (see the
    header's "gaps=" footer) replays slightly compressed at each gap rather
    than with the original stall; that is a replay-fidelity limitation, not a
    detector bug, and is exactly why apps/datalog's seq counter exists — to
    make those gaps visible rather than silently absorbed here too.
    """
    samples: list[tuple[int, int, int, int, int, int]] = []
    label = "unlabelled"
    mount = "unknown"

    with path.open("r", encoding="ascii", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith("# label="):
                    label = line.split("=", 1)[1].strip()
                elif line.startswith("# mount="):
                    mount = line.split("=", 1)[1].strip()
                continue
            if line.startswith(CSV_HEADER_PREFIX):
                continue
            parts = line.split(",")
            if len(parts) != 7:
                continue
            try:
                values = [int(x) for x in parts]
            except ValueError:
                continue  # a truncated last line is expected on power loss
            _, ax, ay, az, gx, gy, gz = values
            samples.append((ax, ay, az, gx, gy, gz))

    engine, _cal = run_samples(samples, thresholds=thresholds, odr_hz=odr_hz)
    return SessionResult(path=path, mount=mount, label=label,
                         samples=len(samples), events=list(engine.events))


def replay_corpus(root: Path, thresholds: Thresholds | None = None,
                  odr_hz: int = 208) -> list[SessionResult]:
    """Replay every recorded CSV under root (data/<mount>/<label>/*.csv).

    A session that fails to parse (wrong format, foreign dataset) is skipped
    rather than aborting the whole corpus — one bad file should not hide the
    results for everything else.
    """
    results = []
    for csv_path in sorted(root.rglob("*.csv")):
        try:
            results.append(replay_csv(csv_path, thresholds=thresholds, odr_hz=odr_hz))
        except OSError:
            continue
    return results


def summarize(results: list[SessionResult]) -> dict:
    """Corpus-wide confusion counts and the two numbers PROJECT_OUTLINE.md
    names as the actual success criteria: sensitivity (S1) and the inverse of
    the false-alarm rate (S2, here as specificity over non-fall sessions).
    """
    counts = {"TP": 0, "FN": 0, "FP": 0, "TN": 0}
    for r in results:
        counts[r.outcome] += 1
    tp, fn, fp, tn = counts["TP"], counts["FN"], counts["FP"], counts["TN"]
    return {
        "counts": counts,
        "n": len(results),
        "sensitivity": (tp / (tp + fn)) if (tp + fn) else None,
        "specificity": (tn / (tn + fp)) if (tn + fp) else None,
    }


# ── SisFall public dataset ───────────────────────────────────────────────────

def replay_sisfall_trial(trial, thresholds: Thresholds | None = None) -> SessionResult:
    """Score one SisFall trial. Ground truth comes from its filename code."""
    engine, _cal = run_samples(trial.samples, thresholds=thresholds,
                               odr_hz=sisfall_mod.SISFALL_HZ)
    return SessionResult(path=trial.path, mount="waist", label=trial.label,
                         samples=len(trial.samples), events=list(engine.events))


def replay_sisfall(root: Path, thresholds: Thresholds | None = None,
                   limit: int | None = None,
                   subjects: set[str] | None = None,
                   progress=None) -> list[SessionResult]:
    """Replay SisFall trials under `root`. `limit` samples evenly across codes."""
    paths = sisfall_mod.find_trials(root, limit=limit, subjects=subjects)
    out: list[SessionResult] = []
    for i, path in enumerate(paths):
        trial = sisfall_mod.load(path)
        if trial is None:
            continue
        out.append(replay_sisfall_trial(trial, thresholds=thresholds))
        if progress is not None:
            progress(i + 1, len(paths))
    return out
