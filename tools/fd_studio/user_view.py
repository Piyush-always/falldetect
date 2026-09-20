"""
The User tab: what a wearer, a family member, or a professor sees.

Deliberately NOT the Debug tab with fewer labels. Different audience, so
different rules:

  - One state at a time, in plain language. No milli-g, no degrees, no
    variance, no threshold names.
  - Colour carries the message. Green / amber / red is readable across a room
    without reading the words, which is the point for an alert device.
  - The fall screen is the product, not a debug view of one: it mirrors
    PROJECT_OUTLINE.md section 6's alert flow, including the 30 s cancel
    window that makes a false alarm survivable ("annoying beep the wearer
    silences" instead of "caregiver panic at 3am").

Everything here reads the SAME Engine output the Debug tab does. There is no
second detector and no smoothing that Debug cannot see - if the two disagree,
that is a bug, not a feature.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Property, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from . import tokens as T
from .engine import Activity, Posture, Stage

# Seconds the wearer has to cancel before this would escalate to a caregiver.
# PROJECT_OUTLINE.md section 6. Nothing is actually sent anywhere yet - there
# is no cloud in v1 scope - so this is an honest preview of the real flow, not
# a simulation pretending to alert someone.
CANCEL_WINDOW_S = 30


def _col(name: str) -> QColor:
    return T.rgba_to_qcolor(T.c(name))


def _font(size: int, weight: int = 400, mono: bool = False) -> QFont:
    f = QFont("Cascadia Code" if mono else "Segoe UI Variable Text")
    if mono:
        f.setStyleHint(QFont.Monospace)
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(min(900, max(100, weight))))
    return f


class BigStatus(QWidget):
    """The single most important element: one word, one colour, one line."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self._title = "Not connected"
        self._sub = "Connect the device to begin"
        self._tone = "text_3"
        self._alive = False
        self._pulse = 0.0

        self._anim = QPropertyAnimation(self, b"pulse", self)
        self._anim.setDuration(1600)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.setLoopCount(-1)

    def get_pulse(self) -> float:
        return self._pulse

    def set_pulse(self, v: float) -> None:
        self._pulse = v
        self.update()

    pulse = Property(float, get_pulse, set_pulse)

    def set_state(self, title: str, sub: str, tone: str, alive: bool) -> None:
        if (title, sub, tone, alive) == (self._title, self._sub, self._tone, self._alive):
            return
        self._title, self._sub, self._tone, self._alive = title, sub, tone, alive
        if alive and T.MOTION_ENABLED:
            if self._anim.state() != QPropertyAnimation.Running:
                self._anim.start()
        else:
            self._anim.stop()
            self._pulse = 0.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        tone = _col(self._tone)
        bg = QColor(tone)
        bg.setAlphaF(0.10 + 0.05 * self._pulse)
        p.setBrush(bg)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(r, T.R_PANEL, T.R_PANEL)

        # Status dot, large enough to read as the signal rather than a bullet.
        cx = r.center().x()
        dot_r = 9.0 + 2.0 * self._pulse
        p.setBrush(tone)
        p.drawEllipse(QRectF(cx - dot_r, r.top() + 34 - dot_r, dot_r * 2, dot_r * 2))

        p.setPen(QPen(tone))
        # Shrink to fit rather than clip. "Set up needed" and "Help is being
        # called" overflowed at 34px and were cut off mid-word at both edges -
        # on the one element whose whole job is to be readable instantly.
        title_rect = QRectF(r.left() + 12, r.top() + 58, r.width() - 24, 46)
        size = 34
        while size > 18:
            p.setFont(_font(size, 650))
            if p.fontMetrics().horizontalAdvance(self._title) <= title_rect.width():
                break
            size -= 2
        p.drawText(title_rect, Qt.AlignHCenter | Qt.AlignVCenter, self._title)

        p.setPen(QPen(_col("text_2")))
        p.setFont(_font(14, 400))
        sub_rect = QRectF(r.left() + 24, r.top() + 106, r.width() - 48, 52)
        p.drawText(sub_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self._sub)


class CancelCountdown(QWidget):
    """The 30 s cancel ring plus the button that stands the alert down."""

    def __init__(self, on_cancel, parent=None):
        super().__init__(parent)
        self._remaining = CANCEL_WINDOW_S
        self._total = CANCEL_WINDOW_S
        self.setVisible(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, T.S3, 0, 0)
        lay.setSpacing(T.S3)

        self._ring = _Ring()
        self._ring.setFixedHeight(120)
        lay.addWidget(self._ring)

        self.btn = QPushButton("I'm OK")
        self.btn.setObjectName("Danger")
        self.btn.setMinimumHeight(48)
        self.btn.clicked.connect(on_cancel)
        lay.addWidget(self.btn)

    def set_remaining(self, seconds: float) -> None:
        self._ring.set_fraction(max(0.0, seconds) / self._total)
        self._ring.set_label(str(int(math.ceil(max(0.0, seconds)))))


class _Ring(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._fraction = 1.0
        self._label = str(CANCEL_WINDOW_S)

    def set_fraction(self, f: float) -> None:
        self._fraction = max(0.0, min(1.0, f))
        self.update()

    def set_label(self, text: str) -> None:
        if text != self._label:
            self._label = text
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.TextAntialiasing)
        side = min(self.width(), self.height()) - 8
        r = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)

        track = _col("surface_3")
        p.setPen(QPen(track, 8, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r, 0, 360 * 16)

        p.setPen(QPen(_col("danger"), 8, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r, 90 * 16, -int(360 * 16 * self._fraction))

        p.setPen(QPen(_col("danger")))
        p.setFont(_font(34, 700, mono=True))
        p.drawText(r, Qt.AlignCenter, self._label)


class PositionPicker(QWidget):
    """Where the device is worn, as three large targets rather than a combo box."""

    POSITIONS = [("neck", "Neck"), ("wrist", "Wrist"), ("waist", "Waist")]

    def __init__(self, on_change, parent=None):
        super().__init__(parent)
        self._on_change = on_change
        self._value = "neck"
        self._buttons: dict[str, QPushButton] = {}

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(T.S2)

        for key, label in self.POSITIONS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setMinimumHeight(44)
            b.setObjectName("Segment")
            b.clicked.connect(lambda _=False, k=key: self.set_value(k))
            self._buttons[key] = b
            lay.addWidget(b)

        self._refresh()

    def value(self) -> str:
        return self._value

    def set_value(self, key: str) -> None:
        if key not in self._buttons:
            return
        self._value = key
        self._refresh()
        self._on_change(key)

    def _refresh(self) -> None:
        for key, b in self._buttons.items():
            b.setChecked(key == self._value)


class ActivityBar(QWidget):
    """Today's movement as a shape, not a table of numbers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(56)
        self._series: list[float] = []

    def set_series(self, values: list[float]) -> None:
        self._series = values
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        p.setBrush(_col("surface_2"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(r, T.R_CARD, T.R_CARD)

        if len(self._series) < 2:
            p.setPen(QPen(_col("text_3")))
            p.setFont(_font(12))
            p.drawText(r, Qt.AlignCenter, "Movement will appear here")
            return

        hi = max(self._series) or 1.0
        inner = r.adjusted(T.S3, T.S3, -T.S3, -T.S3)
        step = inner.width() / (len(self._series) - 1)

        path = QPainterPath()
        for i, v in enumerate(self._series):
            x = inner.left() + i * step
            y = inner.bottom() - (v / hi) * inner.height()
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        p.setPen(QPen(_col("accent"), 2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


class UserTab(QWidget):
    """Assembles the consumer view and translates Engine state into plain words."""

    def __init__(self, get_engine, get_link, on_alarm=None, parent=None):
        super().__init__(parent)
        # Called with True to sound, False to silence. Injected so this
        # view does not own the audio device.
        self._on_alarm = on_alarm
        # Lets the button label reflect reality rather than guess.
        self._is_sounding = None
        self._get_engine = get_engine
        self._get_link = get_link
        self._fall_started: float | None = None
        # Set by the Test alarm button. Runs the SAME countdown and
        # cancel path as a real fall - a rehearsal that takes a
        # different code path rehearses nothing.
        self._test_fall = False
        self._cancelled_until = 0.0
        self._activity: list[float] = []
        self._last_activity_push = 0.0

        # Set by MainWindow.notify(). Connection problems have to be readable
        # from THIS tab - the Debug log is a different tab, and a user sitting
        # here would otherwise just see a tool that never connects.
        self.notice = ""
        # Set by MainWindow once the device version has been read over BLE.
        self.firmware_version = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(T.S8, T.S6, T.S8, T.S6)
        outer.setSpacing(T.S4)

        centre = QWidget()
        centre.setMaximumWidth(560)
        lay = QVBoxLayout(centre)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(T.S4)

        self.status = BigStatus()
        lay.addWidget(self.status)

        self.countdown = CancelCountdown(self._cancel_alert)
        lay.addWidget(self.countdown)

        lay.addWidget(self._caption("MOVEMENT TODAY"))
        self.activity = ActivityBar()
        lay.addWidget(self.activity)

        lay.addWidget(self._caption("WORN ON"))
        self.position = PositionPicker(self._position_changed)
        lay.addWidget(self.position)

        # Recording state. The device button starts/stops a session and the
        # only feedback was a 3 s LED on the device itself - easy to think you
        # are recording when you are not, and lose the session.
        self.lbl_rec = QLabel("")
        self.lbl_rec.setObjectName("Caption")
        self.lbl_rec.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.lbl_rec)

        self.hint = QLabel("")
        self.hint.setObjectName("Caption")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.hint)

        # Rehearse the alarm without throwing yourself at a mattress. Anyone
        # demoing this needs to have seen it happen once before an audience
        # sees it.
        self.btn_test = QPushButton("Test alarm")
        self.btn_test.clicked.connect(self._test_alarm)
        lay.addWidget(self.btn_test)

        lay.addStretch(1)

        # Device footer: battery, charge state, firmware version. Quiet by
        # design - this is reassurance, not a dashboard. The raw millivolts
        # appear only when the reading is implausible, because the divider
        # values behind it are assumed rather than measured (see the overlay).
        foot = QHBoxLayout()
        self.lbl_batt = QLabel("—")
        self.lbl_batt.setObjectName("Caption")
        foot.addWidget(self.lbl_batt)
        foot.addStretch(1)
        self.lbl_fw = QLabel("")
        self.lbl_fw.setObjectName("Caption")
        foot.addWidget(self.lbl_fw)
        lay.addLayout(foot)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(centre)
        row.addStretch(1)
        outer.addLayout(row, 1)


    def _update_footer(self, link) -> None:
        """Battery and firmware version. Plain words, no jargon."""
        if link is None or getattr(link, "battery_pct", None) is None:
            self.lbl_batt.setText("—")
        else:
            pct = link.battery_pct
            mv = getattr(link, "battery_mv", None)
            if getattr(link, "charging", False):
                text = f"Charging · {pct}%"
            elif pct <= 15:
                text = f"Battery low · {pct}%"
            else:
                text = f"Battery {pct}%"
            # A Li-Po outside 3.0-4.3 V means the divider constants are wrong,
            # not that the cell is odd. Surface it rather than show a
            # confident-looking percentage derived from a bad ratio.
            if mv is not None and not (3000 <= mv <= 4300):
                text += f"  (reading {mv} mV - check divider)"
            self.lbl_batt.setText(text)

        ver = getattr(self, "firmware_version", "")
        self.lbl_fw.setText(f"Firmware {ver}" if ver else "")

    def _caption(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("Micro")
        return lab

    def _position_changed(self, key: str) -> None:
        # Recorded sessions are filed under the mount point, so the Debug
        # tab's recorder reads this rather than keeping a second selector.
        self.mount = key


    def _render_alert(self, now: float) -> None:
        """The alert screen. ONE implementation, shared by real falls and
        rehearsals - a drill that renders through different code rehearses
        nothing."""
        if self._fall_started is None:
            self._fall_started = now
        tag = " (test)" if self._test_fall else ""
        remaining = CANCEL_WINDOW_S - (now - self._fall_started)

        if remaining <= 0:
            self.status.set_state("Help is being called" + tag,
                                  "No response after 30 seconds.",
                                  "danger", True)
            self.countdown.setVisible(False)
            return

        self.status.set_state("Possible fall" + tag,
                              "Press I'm OK if this was not a fall.",
                              "danger", True)
        self.countdown.setVisible(True)
        self.countdown.set_remaining(remaining)

    def _test_alarm(self) -> None:
        """Start a rehearsal alert - or stop one already sounding.

        Doubles as a stop because an alarm with no visible way to silence it
        is a fault, not a feature. "I'm OK" only appears during the alert
        screen; this button is always on the page.
        """
        if self._test_fall or (self._is_sounding is not None
                               and self._is_sounding()):
            self._cancel_alert()
            return
        self._test_fall = True
        self._fall_started = time.time()
        self._cancelled_until = 0.0
        if self._on_alarm is not None:
            self._on_alarm(True)

    def _cancel_alert(self) -> None:
        self._fall_started = None
        self._test_fall = False
        if self._on_alarm is not None:
            self._on_alarm(False)
        # Suppress re-alerting on the tail of the same event. The engine's own
        # refractory window covers the detector; this covers the UI.
        self._cancelled_until = time.time() + 10.0
        self.countdown.setVisible(False)

    # ── refresh ──────────────────────────────────────────────────────────────
    def tick(self) -> None:
        engine = self._get_engine()
        link = self._get_link()
        now = time.time()

        self._update_footer(link)

        # Recording strip. Shown only while recording, so it reads as a state
        # rather than as decoration.
        sounding = self._is_sounding() if self._is_sounding else False
        self.btn_test.setText("Stop alarm" if (sounding or self._test_fall)
                              else "Test alarm")

        if link is not None and getattr(link, "recording", False):
            n = link.stats().get("rec_n", 0)
            self.lbl_rec.setText(f"● RECORDING — {n:,} samples")
            self.lbl_rec.setStyleSheet(f"color:{T.c('danger')};")
        else:
            self.lbl_rec.setText("")

        # A rehearsal must run with no device attached. Practising the demo
        # should not require hardware, and the connection branches below
        # return early - which silently made "Test alarm" do nothing.
        if self._test_fall:
            self._render_alert(now)
            return

        if link is None:
            if self.notice:
                # A real problem with a real fix - say it here, in amber, not
                # only in a log on another tab.
                self.status.set_state("Can't connect", self.notice,
                                      "warning", False)
            else:
                self.status.set_state("Not connected",
                                      "Press Scan, choose the device, then "
                                      "press Connect.", "text_3", False)
            self.countdown.setVisible(False)
            self.hint.setText("")
            return

        stats = link.stats()
        if stats["samples"] == 0:
            self.status.set_state("Connecting...",
                                  "Waiting for the device to start sending.",
                                  "accent", True)
            self.countdown.setVisible(False)
            return

        if stats["stale"] > 3.0:
            self.status.set_state("Signal lost",
                                  "Move closer to the device, or check it is "
                                  "still switched on.", "warning", True)
            self.countdown.setVisible(False)
            return

        if not engine.calibrated:
            self.status.set_state("Set up needed",
                                  "Stand upright and still, then press "
                                  "Calibrate at the top.", "warning", True)
            self.countdown.setVisible(False)
            self.hint.setText("This teaches the device which way is up for the "
                              "position you are wearing it.")
            return

        self.hint.setText("")

        # Fall takes priority over everything else on screen.
        if (engine.stage is Stage.CONFIRMED or self._test_fall)                 and now > self._cancelled_until:
            if self._fall_started is None:
                self._fall_started = now
        if self._fall_started is not None:
            self._render_alert(now)
            return

        self.countdown.setVisible(False)

        # Normal life.
        if engine.posture is Posture.LYING:
            if engine.activity is Activity.STILL:
                self.status.set_state("Lying down", "Resting, not moving.",
                                      "accent", False)
            else:
                self.status.set_state("Lying down", "Moving about.",
                                      "accent", False)
        elif engine.activity is Activity.WALKING:
            self.status.set_state("Walking", "Steady movement, nothing unusual.",
                                  "success", True)
        elif engine.activity is Activity.STILL:
            self.status.set_state("All good", "Upright and still.",
                                  "success", False)
        else:
            self.status.set_state("Active", "Moving around normally.",
                                  "success", True)

        # One point every 2 s keeps roughly 10 minutes of shape on screen
        # without turning this into a chart anyone has to read precisely.
        if now - self._last_activity_push >= 2.0:
            self._last_activity_push = now
            self._activity.append(float(engine.std))
            if len(self._activity) > 300:
                self._activity.pop(0)
            self.activity.set_series(self._activity)
