"""
SisFall public dataset -> our sample format.

WHY: the detector needs to be shown working on real falls before anyone throws
themselves at a mattress. SisFall gives 4,505 labelled trials from 38 subjects
(23 adults, 15 elderly) - far more than we will ever record ourselves, and the
ground truth is in the filename, so scoring needs no manual labelling.

MOUNTING CAVEAT - READ THIS BEFORE TRUSTING ANY NUMBER FROM IT
--------------------------------------------------------------
SisFall was recorded at the WAIST. The product is worn at the NECK. Both sit on
the trunk, so gravity direction, orientation change and post-impact stillness
all behave similarly - far closer than a wrist would be, which is why
PROJECT_OUTLINE.md warns SisFall transfers poorly to wrist placements.

It is still a proxy. A neck pendant swings on a cord; a belt clip does not. Use
SisFall to answer "does the cascade detect falls at all, and does it survive
ordinary activity" - not to fix final thresholds. Those get confirmed on our own
neck recordings.

UNITS (Verified against data/sisfall/Readme.txt, not assumed)
-------------------------------------------------------------
Nine columns, 200 Hz, all values in raw ADC bits:

    1-3  ADXL345   accelerometer  13-bit, +/-16 g
    4-6  ITG3200   gyroscope      16-bit, +/-2000 deg/s
    7-9  MMA8451Q  accelerometer  14-bit, +/-8 g

The Readme's conversion is  value = [(2*Range) / 2^Resolution] * raw.

We use the ADXL345, not the MMA8451Q: at +/-8 g the MMA clips on hard impacts,
and our LSM6DS3TR-C runs at +/-16 g precisely so it does not. Scoring against a
sensor that saturates where ours would not would flatter the detector on exactly
the events that matter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SISFALL_HZ = 200

# (2 * 16 g) / 2^13, expressed in milli-g.
ADXL345_MG_PER_LSB = (2.0 * 16.0) / (2 ** 13) * 1000.0
# (2 * 2000 deg/s) / 2^16, expressed in deci-dps.
ITG3200_DDPS_PER_LSB = (2.0 * 2000.0) / (2 ** 16) * 10.0

#: Activity code -> human label, from the Readme's tables.
ADL_CODES = {
    "D01": "walking_slow", "D02": "walking_quick",
    "D03": "jogging_slow", "D04": "jogging_quick",
    "D05": "stairs_slow", "D06": "stairs_quick",
    "D07": "sit_chair_slow", "D08": "sit_chair_quick",
    "D09": "sit_low_chair_slow", "D10": "sit_low_chair_quick",
    "D11": "collapse_into_chair", "D12": "lying_slow",
    "D13": "lying_quick", "D14": "roll_over_in_bed",
    "D15": "bend_knees", "D16": "bend_no_knees",
    "D17": "car_in_out", "D18": "stumble_while_walking",
    "D19": "jump_reach_object",
}

FALL_CODES = {
    "F01": "fall_forward_slip", "F02": "fall_backward_slip",
    "F03": "fall_lateral_slip", "F04": "fall_forward_trip",
    "F05": "fall_forward_jogging_trip", "F06": "fall_vertical_faint",
    "F07": "fall_damped_by_hands", "F08": "fall_forward_getting_up",
    "F09": "fall_lateral_getting_up", "F10": "fall_forward_sitting_down",
    "F11": "fall_backward_sitting_down", "F12": "fall_lateral_sitting_down",
    "F13": "fall_forward_while_seated", "F14": "fall_backward_while_seated",
    "F15": "fall_lateral_while_seated",
}

#: The ADLs most likely to be mistaken for a fall. A detector that scores well
#: overall but fails these is not usable - they are common daily movements, so
#: each false positive here is one the wearer would actually experience.
HARD_ADLS = {"D11", "D13", "D18", "D19", "D08", "D10"}


@dataclass(frozen=True)
class Trial:
    """One SisFall recording, already converted to our units."""

    path: Path
    code: str          # "D01" / "F03"
    subject: str       # "SA01" / "SE06"
    trial: str         # "R01"
    label: str         # human-readable activity
    is_fall: bool
    elderly: bool
    samples: list[tuple[int, int, int, int, int, int]]

    @property
    def is_hard_adl(self) -> bool:
        return self.code in HARD_ADLS

    @property
    def duration_s(self) -> float:
        return len(self.samples) / SISFALL_HZ


def parse_name(path: Path) -> tuple[str, str, str] | None:
    """('F05', 'SA01', 'R04') from 'F05_SA01_R04.txt', or None if unrecognised."""
    parts = path.stem.split("_")
    if len(parts) != 3:
        return None
    code, subject, trial = parts
    if code not in ADL_CODES and code not in FALL_CODES:
        return None
    return code, subject, trial


def load(path: Path) -> Trial | None:
    """Read one SisFall file. Returns None if it is not a usable trial.

    Malformed rows are skipped rather than raising: a few files in the
    distribution have a truncated final line, and losing one corpus file to a
    parse error would be a worse outcome than dropping one sample.
    """
    named = parse_name(path)
    if named is None:
        return None
    code, subject, trial = named

    samples: list[tuple[int, int, int, int, int, int]] = []
    with path.open("r", encoding="ascii", errors="replace") as fh:
        for raw in fh:
            line = raw.strip().rstrip(";")
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 6:
                continue
            try:
                ax, ay, az = (int(parts[i]) for i in (0, 1, 2))
                gx, gy, gz = (int(parts[i]) for i in (3, 4, 5))
            except ValueError:
                continue
            samples.append((
                int(round(ax * ADXL345_MG_PER_LSB)),
                int(round(ay * ADXL345_MG_PER_LSB)),
                int(round(az * ADXL345_MG_PER_LSB)),
                int(round(gx * ITG3200_DDPS_PER_LSB)),
                int(round(gy * ITG3200_DDPS_PER_LSB)),
                int(round(gz * ITG3200_DDPS_PER_LSB)),
            ))

    if not samples:
        return None

    is_fall = code in FALL_CODES
    return Trial(
        path=path, code=code, subject=subject, trial=trial,
        label=(FALL_CODES if is_fall else ADL_CODES)[code],
        is_fall=is_fall,
        elderly=subject.startswith("SE"),
        samples=samples,
    )


def find_trials(root: Path, limit: int | None = None,
                subjects: set[str] | None = None) -> list[Path]:
    """SisFall .txt trial files under `root`, sorted for reproducibility.

    `limit` takes an evenly-spread sample rather than the first N, so a quick
    run still covers every activity code and subject instead of just SA01's
    walking trials.
    """
    files = sorted(p for p in root.rglob("*.txt")
                   if parse_name(p) is not None
                   and (subjects is None or p.stem.split("_")[1] in subjects))
    if limit is not None and len(files) > limit:
        step = len(files) / limit
        files = [files[int(i * step)] for i in range(limit)]
    return files
