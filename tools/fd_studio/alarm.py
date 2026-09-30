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

THE SOUND
---------
Modelled on patient-monitor alarms (the IEC 60601-1-8 style - not certified
to it): short, buzzy, harmonic-rich pulses at one flat pitch. That reads as
"medical alarm", where a sweeping siren or a two-note ding-dong read as a toy
(user feedback, 2026-09-30). Two levels:

  1. Cancel window (first 30 s): 3 pulses, then a pause, repeating.
     Insistent, not frantic - most of these are false alarms the wearer is
     about to cancel.
  2. Escalated (window expired, or SOS - at once): the high-priority burst,
     3 + 2 + 3 + 2 pulses, higher and louder, repeating until someone
     silences or dismisses it.

Both are WAVs played through the Windows mixer (PlaySound, looping), not
winsound.Beep: fuller, and louder at the same volume setting. They are
synthesised on first use into the temp folder - no binary in git, nothing for
PyInstaller to bundle. If PlaySound fails, the same rhythm is played with
winsound.Beep instead: an alarm that goes quiet because a sound file would not
play is the failure this module exists to prevent.

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


#: Phase 2 starts this long after start() even without escalate(), so a
#: rehearsal ("Test alarm") also reaches the escalated sound.
_URGENT_AFTER_S = 30.0

#: Once escalated (window expired, or SOS) the alarm sounds until a person
#: silences or dismisses it - no time cap. A 10-minute backstop used to stop
#: it while the alert was still unanswered and no phone may have been
#: reached (PR #1 review). The old 30-cycle cap was worse: 21 s, before the
#: window even ended.

# ── the pulses ───────────────────────────────────────────────────────────────
RATE = 22050
PULSE_S = 0.15          # one pulse
PULSE_GAP_S = 0.075     # between pulses inside a group
ATTACK_S = 0.015        # ramps: no click, but still a hard edge
RELEASE_S = 0.03
#: Relative level of harmonics 1..5. A pure sine is easy to ignore and hard
#: to locate; the upper harmonics make it buzzy and cut through a room.
HARMONICS = (1.0, 0.7, 0.5, 0.35, 0.25)

#: (file, fundamental Hz, peak level, pulse groups, gaps after each group).
#: The last gap is the pause before the pattern repeats.
MEDIUM = ("fd_studio_alarm_medium_v2.wav", 587.0, 0.7, (3,), (1.6,))
HIGH = ("fd_studio_alarm_high_v2.wav", 740.0, 0.9, (3, 2, 3, 2),
        (0.35, 0.7, 0.35, 0.8))


def timeline(groups, gaps) -> list[tuple[bool, float]]:
    """Pulse groups -> [(is_pulse, seconds), ...], starting on a pulse."""
    out: list[tuple[bool, float]] = []
    for n, gap in zip(groups, gaps):
        for i in range(n):
            out.append((True, PULSE_S))
            if i < n - 1:
                out.append((False, PULSE_GAP_S))
        out.append((False, gap))
    return out


def make_wav(path: Path, level_spec) -> Path:
    """Write one looping alarm pattern (MEDIUM or HIGH) to `path`."""
    _name, f0, level, groups, gaps = level_spec
    attack, release = int(RATE * ATTACK_S), int(RATE * RELEASE_S)
    wav: list[float] = []
    for is_pulse, secs in timeline(groups, gaps):
        n = int(RATE * secs)
        if not is_pulse:
            wav.extend([0.0] * n)
            continue
        for i in range(n):
            env = min(1.0, i / attack, (n - 1 - i) / release)
            t = i / RATE
            wav.append(env * sum(a * math.sin(2 * math.pi * f0 * (k + 1) * t)
                                 for k, a in enumerate(HARMONICS)))
    # Normalise to the real peak - the harmonics never all line up, so
    # scaling by their summed amplitude comes out far too quiet (measured).
    scale = 32767 * level / max(abs(v) for v in wav)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(v * scale)) for v in wav))
    return path


def wav_path(level_spec) -> Path:
    """The pattern's WAV, synthesised on first use. The file name carries a
    version, so changing the sound never plays a stale file."""
    path = Path(tempfile.gettempdir()) / level_spec[0]
    if not path.exists() or path.stat().st_size < 1000:
        make_wav(path, level_spec)
    return path


class Alarm:
    """Start/stop an audible alarm. Safe to call from the GUI thread."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._urgent = threading.Event()
        self._thread: threading.Thread | None = None
        #: Why a phase is using Beep instead of its WAV, if it is. For the log.
        self.siren_error = ""

    @property
    def available(self) -> bool:
        return _HAVE_SOUND

    @property
    def sounding(self) -> bool:
        """True while a run is going and has not been told to stop. A run
        that is only winding down counts as NOT sounding, so a start() right
        after stop() is not mistaken for 'already on' (PR #1 review)."""
        return (self._thread is not None and self._thread.is_alive()
                and not self._stop.is_set())

    def start(self, urgent: bool = False) -> None:
        """Begin sounding - phase 1, or straight to phase 2 if `urgent`.
        Does nothing if already sounding."""
        if not _HAVE_SOUND or self.sounding:
            return
        old = self._thread
        if old is not None and old.is_alive():
            # A stopped run still winding down (at most one pulse). Let it
            # finish first: its final PlaySound(None) would otherwise cut off
            # the new run's sound, leaving a new SOS silent.
            old.join(timeout=1.0)
        # Fresh events per run: a stale run can never silence a new one.
        self._stop = threading.Event()
        self._urgent = threading.Event()
        # Set BEFORE the thread exists: set after, the thread can get a
        # phase-1 sound out first (measured) - an SOS must open escalated.
        if urgent:
            self._urgent.set()
        # Daemon so a forgotten alarm cannot keep the process alive, and on a
        # worker thread because winsound.Beep BLOCKS for its full duration -
        # calling it on the GUI thread would freeze the window mid-alarm,
        # which is the worst possible moment for the UI to stop responding.
        self._thread = threading.Thread(
            target=self._run, args=(self._stop, self._urgent), daemon=True)
        self._thread.start()

    def escalate(self) -> None:
        """Go to phase 2 now (window expired, or SOS). Starts the alarm if
        it is not already sounding."""
        if self.sounding:
            self._urgent.set()
        else:
            self.start(urgent=True)

    def stop(self) -> None:
        self._stop.set()

    def _run(self, stop: threading.Event, urgent: threading.Event) -> None:
        import winsound

        t0 = time.monotonic()

        def phase1_over() -> bool:
            return (stop.is_set() or urgent.is_set()
                    or time.monotonic() - t0 >= _URGENT_AFTER_S)

        def phase2_over() -> bool:
            return stop.is_set()

        try:
            if not urgent.is_set():
                self._play(winsound, MEDIUM, phase1_over, stop)
            if not stop.is_set():
                self._play(winsound, HIGH, phase2_over, stop)
        finally:
            try:
                winsound.PlaySound(None, 0)
            except RuntimeError:
                pass

    def _play(self, winsound, spec, over, stop: threading.Event) -> None:
        """Loop one pattern until over(). WAV first; Beep if that fails."""
        try:
            winsound.PlaySound(str(wav_path(spec)), winsound.SND_FILENAME
                               | winsound.SND_ASYNC | winsound.SND_LOOP)
        except (RuntimeError, OSError) as exc:
            self.siren_error = f"{type(exc).__name__}: {exc}"
        else:
            while not over():
                stop.wait(0.05)
            return

        # Same rhythm on the PC beeper - never silence.
        _name, f0, _level, groups, gaps = spec
        while not over():
            for is_pulse, secs in timeline(groups, gaps):
                if over():
                    return
                if is_pulse:
                    try:
                        winsound.Beep(int(f0), int(secs * 1000))
                    except RuntimeError:
                        return
                else:
                    stop.wait(secs)
