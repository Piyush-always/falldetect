"""
Custom-painted components.

Everything here exists because QSS cannot express it. Each one draws structure
rather than restating a number the user could have read from a label — the
cascade stepper in particular teaches the whole detection architecture at a
glance, which no amount of log text does.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Property, QPointF, QRectF, Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import tokens as T


def _col(name: str) -> QColor:
    return T.rgba_to_qcolor(T.c(name))


def _font(spec, mono: bool = False) -> QFont:
    size, weight, tracking = spec
    f = QFont("Cascadia Code" if mono else "Segoe UI Variable Text")
    if mono:
        f.setStyleHint(QFont.Monospace)
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(min(900, max(100, weight))))
    if tracking:
        f.setLetterSpacing(QFont.PercentageSpacing, 100 + tracking * 100)
    return f


class StatePlate(QWidget):
    """The one thing a user across the room must be able to read."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(112)
        self._text = "NOT CONNECTED"
        self._sub = "connect a device to begin"
        self._tone = "text_3"
        self._pulse = 0.0

        self._anim = QPropertyAnimation(self, b"pulse", self)
        self._anim.setDuration(1200)
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

    def set_state(self, text: str, sub: str, tone: str, alive: bool) -> None:
        self._text, self._sub, self._tone = text, sub, tone
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
        p.setPen(QPen(QColor(tone.red(), tone.green(), tone.blue(),
                             int(90 + 60 * self._pulse)), 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, T.R_CARD, T.R_CARD)

        p.setPen(tone)
        p.setFont(_font(T.T_DISPLAY))
        p.drawText(r.adjusted(T.S4, T.S3, -T.S4, 0), Qt.AlignLeft | Qt.AlignTop, self._text)

        p.setPen(_col("text_2"))
        p.setFont(_font(T.T_CAPTION))
        p.drawText(r.adjusted(T.S4, 0, -T.S4, -T.S3),
                   Qt.AlignLeft | Qt.AlignBottom, self._sub)
        p.end()


class CascadeStepper(QWidget):
    """The four detection stages as a rail.

    This is the component that makes the architecture legible: a viewer can see
    that free-fall fired but stillness did not, which is the difference between
    'it missed' and 'it correctly rejected'.
    """

    STAGES = ["FREE-FALL", "IMPACT", "ORIENTATION", "STILLNESS"]
    KEYS = ["freefall", "impact", "orientation", "stillness"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(64)
        self._flags = {k: False for k in self.KEYS}
        self._armed = False
        self._failed = False

    def set_flags(self, flags: dict, armed: bool, failed: bool) -> None:
        self._flags = dict(flags)
        self._armed = armed
        self._failed = failed
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        n = len(self.STAGES)
        cy = h * 0.40
        pad = 46
        span = max(1, w - pad * 2)
        xs = [pad + span * i / (n - 1) for i in range(n)]

        rail = _col("hairline")
        p.setPen(QPen(rail, 2))
        p.drawLine(QPointF(xs[0], cy), QPointF(xs[-1], cy))

        done_col = _col("danger") if self._failed else _col("accent")
        for i in range(n - 1):
            if self._flags[self.KEYS[i]] and self._flags[self.KEYS[i + 1]]:
                p.setPen(QPen(done_col, 2))
                p.drawLine(QPointF(xs[i], cy), QPointF(xs[i + 1], cy))

        for i, x in enumerate(xs):
            on = self._flags[self.KEYS[i]]
            col = done_col if on else _col("text_3")
            p.setPen(QPen(col, 1.6))
            p.setBrush(col if on else _col("surface_2"))
            p.drawEllipse(QPointF(x, cy), 7, 7)
            if on:
                p.setPen(QPen(QColor("#FFFFFF"), 1.6))
                p.drawLine(QPointF(x - 3, cy), QPointF(x - 0.5, cy + 2.5))
                p.drawLine(QPointF(x - 0.5, cy + 2.5), QPointF(x + 3.2, cy - 2.6))

            p.setPen(_col("text_1") if on else _col("text_3"))
            p.setFont(_font(T.T_MICRO))
            p.drawText(QRectF(x - 56, cy + 14, 112, 16),
                       Qt.AlignHCenter | Qt.AlignTop, self.STAGES[i])
        p.end()


class TracePlot(QWidget):
    """Acceleration magnitude over time, with the decision bands drawn.

    Bands rather than gridlines: the question a viewer asks is never 'what value
    is this' but 'is it in the region that triggers something'.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self._trace: list = []
        self._ff = 500
        self._impact = 2500
        self._top = 4000.0

    def set_data(self, trace, ff_mg: int, impact_mg: int) -> None:
        self._trace = trace
        self._ff, self._impact = ff_mg, impact_mg
        peak = max((m for _, m, _ in trace[-2000:]), default=1000)
        self._top = max(4000.0, math.ceil(peak / 1000.0) * 1000.0)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        left, top, bot = 40, 10, 22
        plot_h = h - top - bot

        def y(mg):
            return top + plot_h - min(mg, self._top) / self._top * plot_h

        p.setPen(Qt.NoPen)
        ff = _col("warning"); ff.setAlphaF(0.10)
        p.setBrush(ff)
        p.drawRect(QRectF(left, y(self._ff), w - left, top + plot_h - y(self._ff)))
        im = _col("danger"); im.setAlphaF(0.10)
        p.setBrush(im)
        p.drawRect(QRectF(left, top, w - left, y(self._impact) - top))

        step = 1000 if self._top <= 6000 else 2000
        p.setFont(_font(T.T_MICRO, mono=True))
        for mg in range(0, int(self._top) + 1, step):
            yy = y(mg)
            p.setPen(QPen(_col("hairline"), 1))
            p.drawLine(QPointF(left, yy), QPointF(w, yy))
            p.setPen(_col("text_3"))
            p.drawText(QRectF(0, yy - 7, left - 6, 14),
                       Qt.AlignRight | Qt.AlignVCenter, str(mg))

        p.setPen(QPen(_col("text_3"), 1, Qt.DashLine))
        p.drawLine(QPointF(left, y(1000)), QPointF(w, y(1000)))

        if len(self._trace) < 2:
            p.setPen(_col("text_3"))
            p.setFont(_font(T.T_CAPTION))
            p.drawText(self.rect(), Qt.AlignCenter, "waiting for samples")
            p.end()
            return

        pts = self._trace[-int(12 * 208):]
        n = len(pts)
        dx = (w - left) / float(max(1, n - 1))
        path = QPainterPath()
        for i, (_, mg, _t) in enumerate(pts):
            xx, yy = left + i * dx, y(mg)
            if i == 0:
                path.moveTo(xx, yy)
            else:
                path.lineTo(xx, yy)
        p.setPen(QPen(_col("accent"), 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        p.setPen(_col("text_3"))
        p.setFont(_font(T.T_MICRO))
        p.drawText(QRectF(left, h - bot + 4, 200, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, "LAST 12 s   ·   MILLI-G")
        p.end()


def _box(hx: float, hy: float, hz: float):
    v = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
         (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    # (indices, is_top)
    f = [((0, 1, 2, 3), False), ((4, 5, 6, 7), True), ((0, 1, 5, 4), False),
         ((2, 3, 7, 6), False), ((1, 2, 6, 5), False), ((3, 0, 4, 7), False)]
    return v, f


def _view_matrix(elev_deg: float, azim_deg: float):
    import numpy as np
    e, a = math.radians(elev_deg), math.radians(azim_deg)
    ry = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0],
                   [-math.sin(a), 0, math.cos(a)]])
    rx = np.array([[1, 0, 0], [0, math.cos(e), -math.sin(e)],
                   [0, math.sin(e), math.cos(e)]])
    return rx @ ry


class OrientationView(QWidget):
    """The device's orientation, live.

    Shows orientation RELATIVE TO CALIBRATION, not absolute: at the moment you
    press Calibrate the object sits square, and everything after that is the
    measured change. That is exactly what the sensor can know.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 260)
        self._R = None
        self._tilt = 0.0
        self._upright, self._lying = 40, 65
        self._shape = "slab"
        self._heading_age = 0.0
        self._V = _view_matrix(22.0, 34.0)

    def set_shape(self, shape: str) -> None:
        self._shape = shape
        self.update()

    def set_pose(self, R, tilt: float, upright: int, lying: int,
                 heading_age: float) -> None:
        self._R, self._tilt = R, tilt
        self._upright, self._lying = upright, lying
        self._heading_age = heading_age
        self.update()

    def _project(self, pts, cx, cy, scale):
        import numpy as np
        out = []
        for p in pts:
            q = self._V @ np.asarray(p, dtype=float)
            out.append((cx + q[0] * scale, cy - q[1] * scale, float(q[2])))
        return out

    def paintEvent(self, _):
        import numpy as np

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0 - 8
        scale = min(w, h) * 0.30

        if self._R is None:
            p.setPen(_col("text_3"))
            p.setFont(_font(T.T_CAPTION))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "not calibrated\nstand still and press Calibrate upright")
            p.end()
            return

        R = np.asarray(self._R, dtype=float)

        # world gravity: straight down, independent of the object
        gpts = self._project([(0, 0, 0), (0, 0, -1.75)], cx, cy, scale)
        p.setPen(QPen(_col("text_3"), 1.4, Qt.DashLine))
        p.drawLine(QPointF(gpts[0][0], gpts[0][1]), QPointF(gpts[1][0], gpts[1][1]))
        p.setFont(_font(T.T_MICRO))
        p.drawText(QRectF(gpts[1][0] - 20, gpts[1][1], 40, 14),
                   Qt.AlignHCenter | Qt.AlignTop, "GRAVITY")

        # Device axes, labelled. Without these the object's orientation is
        # ambiguous - you can see it move but not say which way is which,
        # which makes "is it mounted the right way round?" unanswerable.
        # After Calibrate these sit in the canonical pose: X right, Y front,
        # Z up.
        axes = ((1.45, 0.0, 0.0, "X", "danger"),
                (0.0, 1.45, 0.0, "Y", "success"),
                (0.0, 0.0, 1.45, "Z", "accent"))
        for ax, ay, az, name, tone in axes:
            tip = tuple(R @ np.asarray((ax, ay, az), dtype=float))
            seg = self._project([(0.0, 0.0, 0.0), tip], cx, cy, scale)
            p.setPen(QPen(_col(tone), 1.6))
            p.drawLine(QPointF(seg[0][0], seg[0][1]),
                       QPointF(seg[1][0], seg[1][1]))
            p.setFont(_font(T.T_MICRO))
            p.drawText(QRectF(seg[1][0] - 8, seg[1][1] - 8, 16, 14),
                       Qt.AlignCenter, name)

        verts, faces = (_box(1.0, 0.62, 0.11) if self._shape == "slab"
                        else _box(0.72, 0.72, 0.72))
        rot = [tuple(R @ np.asarray(v, dtype=float)) for v in verts]
        proj = self._project(rot, cx, cy, scale)

        order = sorted(range(len(faces)),
                       key=lambda i: sum(proj[k][2] for k in faces[i][0]) / 4.0)

        for fi in order:
            idx, is_top = faces[fi]
            a = np.asarray(rot[idx[1]]) - np.asarray(rot[idx[0]])
            b = np.asarray(rot[idx[3]]) - np.asarray(rot[idx[0]])
            n = np.cross(a, b)
            ln = np.linalg.norm(n)
            lit = 0.45 if ln < 1e-9 else 0.35 + 0.65 * abs(float(
                np.dot(n / ln, self._V.T @ np.array([0.3, 0.5, 1.0]))))
            lit = max(0.18, min(1.0, lit))

            base = _col("accent") if is_top else _col("text_2")
            col = QColor(int(base.red() * lit), int(base.green() * lit),
                         int(base.blue() * lit))
            col.setAlphaF(0.92 if is_top else 0.80)

            path = QPainterPath()
            path.moveTo(proj[idx[0]][0], proj[idx[0]][1])
            for k in idx[1:]:
                path.lineTo(proj[k][0], proj[k][1])
            path.closeSubpath()
            p.setBrush(col)
            p.setPen(QPen(_col("hairline_st"), 1))
            p.drawPath(path)

        # body axes
        axes = [((1.55, 0, 0), "X", "danger"),
                ((0, 1.30, 0), "Y", "success"),
                ((0, 0, 1.30), "Z", "accent")]
        for vec, name, tone in axes:
            tip = tuple(R @ np.asarray(vec, dtype=float))
            a0, a1 = self._project([(0, 0, 0), tip], cx, cy, scale)
            p.setPen(QPen(_col(tone), 2))
            p.drawLine(QPointF(a0[0], a0[1]), QPointF(a1[0], a1[1]))
            p.setPen(_col(tone))
            p.setFont(_font(T.T_MICRO))
            p.drawText(QRectF(a1[0] - 8, a1[1] - 14, 16, 12), Qt.AlignCenter, name)

        # tilt band strip
        bar_y, bar_h, m = h - 22, 6, 14
        bw = w - m * 2

        def seg(a_deg, b_deg, tone, alpha):
            col = _col(tone)
            col.setAlphaF(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRect(QRectF(m + bw * a_deg / 180.0, bar_y,
                              bw * (b_deg - a_deg) / 180.0, bar_h))

        seg(0, self._upright, "success", 0.55)
        seg(self._upright, self._lying, "warning", 0.45)
        seg(self._lying, 180, "danger", 0.45)

        x = m + bw * max(0.0, min(180.0, self._tilt)) / 180.0
        p.setPen(QPen(_col("text_1"), 2))
        p.drawLine(QPointF(x, bar_y - 4), QPointF(x, bar_y + bar_h + 4))

        p.setPen(_col("text_1"))
        p.setFont(_font(T.T_TITLE, mono=True))
        p.drawText(QRectF(0, h - 62, w - m, 26), Qt.AlignRight | Qt.AlignVCenter,
                   f"{self._tilt:.0f}°")
        p.setPen(_col("text_3"))
        p.setFont(_font(T.T_MICRO))
        p.drawText(QRectF(m, h - 62, 200, 26), Qt.AlignLeft | Qt.AlignVCenter,
                   "TILT FROM CALIBRATED UPRIGHT")
        if self._heading_age > 30:
            p.setPen(_col("warning"))
            p.drawText(QRectF(m, h - 44, 260, 14), Qt.AlignLeft | Qt.AlignVCenter,
                       f"HEADING DRIFTING — {int(self._heading_age)}s SINCE RESET")
        p.end()


class BubbleLevel(QWidget):
    """Lean direction and magnitude, as a spirit level."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(132)
        self._x = self._y = 0.0
        self._upright, self._lying = 40, 65
        self._ok = False

    def set_lean(self, dx: float, dy: float, upright: int, lying: int,
                 ok: bool) -> None:
        self._x, self._y = dx, dy
        self._upright, self._lying = upright, lying
        self._ok = ok
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0 - 4
        rad = min(w, h) * 0.36
        full = 90.0

        for frac, tone in ((self._upright / full, "success"),
                           (self._lying / full, "warning"),
                           (1.0, "danger")):
            r = rad * min(1.0, frac)
            col = _col(tone)
            col.setAlphaF(0.5)
            p.setPen(QPen(col, 1))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r, r)

        p.setPen(QPen(_col("hairline"), 1))
        p.drawLine(QPointF(cx - rad, cy), QPointF(cx + rad, cy))
        p.drawLine(QPointF(cx, cy - rad), QPointF(cx, cy + rad))

        if self._ok:
            bx = cx + max(-1.0, min(1.0, self._x / full)) * rad
            by = cy - max(-1.0, min(1.0, self._y / full)) * rad
            p.setPen(Qt.NoPen)
            p.setBrush(_col("accent"))
            p.drawEllipse(QPointF(bx, by), 6, 6)

        p.setPen(_col("text_3"))
        p.setFont(_font(T.T_MICRO))
        p.drawText(QRectF(0, h - 14, w, 12), Qt.AlignHCenter | Qt.AlignTop,
                   "LEAN DIRECTION")
        p.end()


class AxisBars(QWidget):
    """All six axes, signed, with values. Accel and gyro scaled separately."""

    NAMES = ["ax", "ay", "az", "gx", "gy", "gz"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6 * 22 + 16)
        self._v = [0] * 6
        self._acc_fs = 2000
        self._gyr_fs = 5000

    def set_values(self, vals) -> None:
        self._v = list(vals)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w = self.width()
        x0, bw = 26, w - 26 - 66
        mid = x0 + bw / 2.0

        for i, name in enumerate(self.NAMES):
            y = 14 + i * 22
            val = self._v[i]
            fs = self._acc_fs if i < 3 else self._gyr_fs
            frac = max(-1.0, min(1.0, val / float(fs)))

            p.setPen(_col("text_3"))
            p.setFont(_font(T.T_MICRO, mono=True))
            p.drawText(QRectF(0, y - 8, x0 - 6, 16), Qt.AlignRight | Qt.AlignVCenter,
                       name)

            p.setPen(QPen(_col("hairline"), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(x0, y - 7, bw, 14))
            p.drawLine(QPointF(mid, y - 8), QPointF(mid, y + 8))

            col = _col("accent") if i < 3 else _col("success")
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            if frac >= 0:
                p.drawRect(QRectF(mid, y - 5, frac * bw / 2.0, 10))
            else:
                p.drawRect(QRectF(mid + frac * bw / 2.0, y - 5, -frac * bw / 2.0, 10))

            p.setPen(_col("text_1"))
            p.setFont(_font(T.T_CAPTION, mono=True))
            p.drawText(QRectF(w - 62, y - 8, 60, 16),
                       Qt.AlignRight | Qt.AlignVCenter, f"{val:d}")
        p.end()


class TiltDial(QWidget):
    """Tilt from the calibrated upright reference, as an arc."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(120)
        self._tilt = 0.0
        self._upright = 40
        self._lying = 65
        self._ok = False

    def set_tilt(self, tilt: float, upright: int, lying: int, ok: bool) -> None:
        self._tilt, self._upright, self._lying, self._ok = tilt, upright, lying, ok
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        cx, cy, rad = w / 2.0, h - 14, min(w / 2.0 - 12, h - 30)
        box = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)

        p.setPen(QPen(_col("hairline"), 8, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(box, 0, 180 * 16)

        if self._ok:
            up = _col("success"); up.setAlphaF(0.55)
            p.setPen(QPen(up, 8, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(box, 180 * 16, -int(self._upright / 180 * 180 * 16))
            ly = _col("warning"); ly.setAlphaF(0.55)
            p.setPen(QPen(ly, 8, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(box, int((180 - self._lying) / 180 * 180 * 16),
                      -int((180 - self._lying) / 180 * 180 * 16))

            ang = math.radians(180 - max(0.0, min(180.0, self._tilt)))
            p.setPen(QPen(_col("text_1"), 2))
            p.drawLine(QPointF(cx, cy),
                       QPointF(cx + rad * 0.82 * math.cos(ang),
                               cy - rad * 0.82 * math.sin(ang)))

        p.setPen(_col("text_1") if self._ok else _col("text_3"))
        p.setFont(_font(T.T_TITLE, mono=True))
        p.drawText(QRectF(0, cy - 34, w, 24), Qt.AlignHCenter | Qt.AlignVCenter,
                   f"{self._tilt:.0f}°" if self._ok else "—")
        p.setPen(_col("text_3"))
        p.setFont(_font(T.T_MICRO))
        p.drawText(QRectF(0, cy - 12, w, 14), Qt.AlignHCenter | Qt.AlignVCenter,
                   "TILT FROM UPRIGHT")
        p.end()
