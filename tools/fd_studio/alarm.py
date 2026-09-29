"""
Audible alarm on the laptop.

WHY THIS EXISTS: the device's LED cannot be the alarm. A pendant sits on the
wearer's chest, and someone lying on the floor cannot see it.
PROJECT_OUTLINE.md section 6 requires an alarm "loud enough to wake someone who
has fallen and is dazed" - that needs a buzzer on the device, which does not
exist yet.

Until it does, the laptop makes the noise. That is honest for a demo (the
sound is real, the escalation it stands for is real) as long as nobody claims
the device itself is sounding. Say so out loud when showing it.

TWO PHASES
----------
  1. Cancel window (first 30 s): a two-tone beep. Insistent, not frantic -
     most of these are false alarms the wearer is about to cancel.
  2. Escalated (window expired, or SOS): a looping siren until someone
     silences or dismisses it. The situation got worse, so the sound does.

The siren is a WAV played through the Windows mixer (PlaySound), not
winsound.Beep: fuller, louder at the same volume setting, and it loops without
a thread blocking on every tone. The WAV is synthesised on first use into the
temp folder - no binary in git, nothing extra for PyInstaller to bundle. If
PlaySound fails for any reason, phase 2 falls back to the fast beep: an alarm
that goes quiet because a sound file would not play is the failure this
module exists to prevent.

Windows only, by design: winsound is in the standard library, so this adds no
dependency, and the project targets Windows. Everywhere else it degrades to
silence rather than failing - a missing sound must never take the GUI down
during a demo.
"""

from __future__ import annotations

import math
import struct
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

try:
    import winsound  # noqa: F401  (Windows only)
    _HAVE_SOUND = sys.platform == "win32"
except ImportError:  # pragma: no cover - non-Windows
    _HAVE_SOUND = False


#: Two-tone pattern, repeated. A steady beep reads as an appliance; an
#: alternating pair reads as an alarm and carries better through a room.
_PATTERN = ((880, 350), (660, 350))

#: Phase 2 fallback if the siren WAV cannot play: faster and higher.
_URGENT_PATTERN = ((1100, 180), (880, 180))
#: Phase 2 starts this long after start() even without escalate(), so a
#: rehearsal ("Test alarm") also reaches the siren at the end of its window.
_URGENT_AFTER_S = 30.0

#: Sounds until stop(). This is only a backstop so a forgotten alarm cannot
#: sound forever. The old 30-cycle cap was 21 s, which went silent BEFORE the
#: 30 s window ended - the alarm stopped exactly when nobody had responded.
_MAX_SECONDS = 600.0

# Siren: a "wail" sweeping LOW -> HIGH -> LOW every SWEEP_S. Upper harmonics
# make it harsher and louder-sounding than a pure sine at the same level,
# which is the point. 22.05 kHz mono 16-bit: ~350 KB for the 8 s loop.
SIREN_RATE = 22050
SIREN_LOW_HZ = 650.0
SIREN_HIGH_HZ = 1500.0
SIREN_SWEEP_S = 1.0
SIREN_SWEEPS = 8
SIREN_LEVEL = 0.9           # peak, as a fraction of full scale
SIREN_FADE_S = 0.01         # at the loop seam, so the repeat does not click
SIREN_FILE = "fd_studio_siren_v1.wav"


def make_siren(path: Path) -> Path:
    """Write the siren loop to `path` (overwriting) and return it.

    Phase-continuous: frequency is integrated, not set per sample, or every
    sweep would crackle.
    """
    n = int(SIREN_RATE * SIREN_SWEEP_S * SIREN_SWEEPS)
    fade = int(SIREN_RATE * SIREN_FADE_S)
    phase = 0.0
    wav = []
    for i in range(n):
        t = (i / SIREN_RATE) % SIREN_SWEEP_S / SIREN_SWEEP_S   # 0..1
        tri = 2 * t if t < 0.5 else 2 * (1 - t)                  # 0..1..0
        freq = SIREN_LOW_HZ + (SIREN_HIGH_HZ - SIREN_LOW_HZ) * tri
        phase += 2 * math.pi * freq / SIREN_RATE
        s = (math.sin(phase) + 0.35 * math.sin(3 * phase)
             + 0.15 * math.sin(5 * phase))
        gain = min(1.0, i / fade, (n - 1 - i) / fade) if fade else 1.0
        wav.append(gain * s)
    # Normalise to the real peak: the harmonics never line up, so scaling by
    # their summed amplitude left the loudest alarm at half volume (measured).
    scale = 32767 * SIREN_LEVEL / max(abs(v) for v in wav)
    frames = b"".join(struct.pack("<h", int(v * scale)) for v in wav)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SIREN_RATE)
        w.writeframes(bytes(frames))
    return path


def siren_path() -> Path:
    """The siren WAV, synthesised on first use."""
    path = Path(tempfile.gettempdir()) / SIREN_FILE
    if not path.exists() or path.stat().st_size < 1000:
        make_siren(path)
    return path


class Alarm:
    """Start/stop an audible alarm. Safe to call from the GUI thread."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._urgent = threading.Event()
        self._thread: threading.Thread | None = None
        #: Why phase 2 is beeping instead of the siren, if it is. For the log.
        self.siren_error = ""

    @property
    def available(self) -> bool:
        return _HAVE_SOUND

    @property
    def sounding(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, urgent: bool = False) -> None:
        """Begin sounding - phase 1, or straight to the siren if `urgent`.
        Does nothing if already sounding."""
        if not _HAVE_SOUND or self.sounding:
            return
        self._stop.clear()
        # Set BEFORE the thread exists: set after, the thread can get one
        # phase-1 beep out first (measured) - an SOS must open on the siren.
        if urgent:
            self._urgent.set()
        else:
            self._urgent.clear()
        # Daemon so a forgotten alarm cannot keep the process alive, and on a
        # worker thread because winsound.Beep BLOCKS for its full duration -
        # calling it on the GUI thread would freeze the window mid-alarm,
        # which is the worst possible moment for the UI to stop responding.
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def escalate(self) -> None:
        """Go to the siren now (window expired, or SOS). Starts the alarm if
        it is not already sounding."""
        if self.sounding:
            self._urgent.set()
        else:
            self.start(urgent=True)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import winsound

        t0 = time.monotonic()

        def left() -> float:
            return _MAX_SECONDS - (time.monotonic() - t0)

        # Phase 1: two-tone beep until escalated, timed out, or stopped.
        while left() > 0 and not self._stop.is_set():
            if self._urgent.is_set() or time.monotonic() - t0 >= _URGENT_AFTER_S:
                break
            for freq, ms in _PATTERN:
                if self._stop.is_set() or self._urgent.is_set():
                    break
                try:
                    winsound.Beep(freq, ms)
                except RuntimeError:
                    # Some audio configurations refuse Beep. Silence is an
                    # acceptable degradation; a crash during an alarm is not.
                    return

        if self._stop.is_set() or left() <= 0:
            return

        # Phase 2: the siren, looping asynchronously until stop().
        try:
            winsound.PlaySound(str(siren_path()), winsound.SND_FILENAME
                               | winsound.SND_ASYNC | winsound.SND_LOOP)
        except (RuntimeError, OSError) as exc:
            self.siren_error = f"{type(exc).__name__}: {exc}"
        else:
            self.siren_error = ""
            self._stop.wait(timeout=max(0.0, left()))
            try:
                winsound.PlaySound(None, 0)
            except RuntimeError:
                pass
            return

        # Siren would not play: fast beep instead, never silence.
        while left() > 0 and not self._stop.is_set():
            for freq, ms in _URGENT_PATTERN:
                if self._stop.is_set():
                    return
                try:
                    winsound.Beep(freq, ms)
                except RuntimeError:
                    return
