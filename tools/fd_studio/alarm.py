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

Windows only, by design: winsound is in the standard library, so this adds no
dependency, and the project targets Windows. Everywhere else it degrades to
silence rather than failing - a missing sound must never take the GUI down
during a demo.
"""

from __future__ import annotations

import sys
import threading

try:
    import winsound  # noqa: F401  (Windows only)
    _HAVE_SOUND = sys.platform == "win32"
except ImportError:  # pragma: no cover - non-Windows
    _HAVE_SOUND = False


#: Two-tone pattern, repeated. A steady beep reads as an appliance; an
#: alternating pair reads as an alarm and carries better through a room.
_PATTERN = ((880, 350), (660, 350))

#: Rough cap on how long the alarm sounds if nothing stops it. The cancel
#: window is 30 s, so this outlasts it without beeping forever in a room full
#: of people if something goes wrong.
_MAX_CYCLES = 30


class Alarm:
    """Start/stop an audible alarm. Safe to call from the GUI thread."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return _HAVE_SOUND

    @property
    def sounding(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Begin sounding. Does nothing if already sounding."""
        if not _HAVE_SOUND or self.sounding:
            return
        self._stop.clear()
        # Daemon so a forgotten alarm cannot keep the process alive, and on a
        # worker thread because winsound.Beep BLOCKS for its full duration -
        # calling it on the GUI thread would freeze the window mid-alarm,
        # which is the worst possible moment for the UI to stop responding.
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import winsound

        for _ in range(_MAX_CYCLES):
            for freq, ms in _PATTERN:
                if self._stop.is_set():
                    return
                try:
                    winsound.Beep(freq, ms)
                except RuntimeError:
                    # Some audio configurations refuse Beep. Silence is an
                    # acceptable degradation; a crash during an alarm is not.
                    return
