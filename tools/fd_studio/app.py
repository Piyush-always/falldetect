"""
FD Studio — live posture, activity and fall detection for falldetect-gkl.

Layout is the standard instrument skeleton: toolbar, sidebar (what is happening
now), primary (the evidence), inspector (what you can change), status strip.

Deviation from the house style worth naming: this uses a native window frame
rather than a frameless one. A hand-rolled title bar means hand-rolled drag,
resize, snap and multi-monitor handling, and none of that serves a tool whose
job is to be trusted while someone throws themselves onto a mattress.
"""

from __future__ import annotations

import queue
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from . import tokens as T
from .engine import Engine, Posture, Stage, Thresholds
from .link import DeviceLink, list_ports
from .widgets import (AxisBars, BubbleLevel, CascadeStepper, OrientationView,
                      StatePlate, TracePlot)

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"

MOUNTS = ["neck", "wrist", "waist", "pocket"]
LABELS = [
    "walking", "standing", "sitting", "lying", "stairs_up", "stairs_down",
    "sit_to_stand", "stand_to_sit", "bending_picking_up",
    "sitting_down_heavily", "walking_stick", "clapping", "jumping",
    "device_dropped_on_table", "device_put_on_taken_off",
    "fall_forward", "fall_backward", "fall_left", "fall_right",
    "fall_from_chair", "fall_slow_slump",
]

TUNABLES = [
    ("freefall_mg", "Free-fall below", "mg", 100, 900),
    ("freefall_min_ms", "Free-fall for", "ms", 20, 400),
    ("impact_mg", "Impact above", "mg", 1200, 8000),
    ("impact_window_ms", "Impact within", "ms", 200, 2000),
    ("hard_impact_mg", "Hard impact (no FF)", "mg", 2000, 12000),
    ("orientation_deg", "Orientation change", "deg", 10, 90),
    ("still_window_ms", "Stillness window", "ms", 500, 10000),
    ("still_std_mg", "Stillness below", "mg", 20, 600),
    ("upright_max_deg", "Upright below", "deg", 10, 60),
    ("lying_min_deg", "Lying above", "deg", 40, 90),
    ("motion_std_mg", "Moving above", "mg", 20, 400),
]


def _label(text: str, obj: str = "") -> QLabel:
    lab = QLabel(text)
    if obj:
        lab.setObjectName(obj)
    return lab


def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFixedHeight(1)
    f.setStyleSheet(f"background:{T.c('hairline')}; border:none;")
    return f


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FD Studio — falldetect-gkl")
        self.resize(1320, 820)
        self.setMinimumSize(1080, 700)

        self.engine = Engine()
        self.link: DeviceLink | None = None
        self._fall_until = 0.0
        self._tick_broken = False
        self._spins: dict[str, QSpinBox] = {}

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._toolbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._sidebar())
        body.addWidget(self._primary(), 1)
        body.addWidget(self._inspector())
        outer.addLayout(body, 1)
        outer.addWidget(self._status())

        self.refresh_ports()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)  # ~30 Hz; sample rate is 208 Hz and is coalesced

    # ── chrome ───────────────────────────────────────────────────────────────
    def _toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(52)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(T.S4, T.S2, T.S4, T.S2)
        lay.setSpacing(T.S2)

        self.port_cb = QComboBox()
        self.port_cb.setMinimumWidth(280)
        lay.addWidget(self.port_cb)

        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_ports)
        lay.addWidget(b)

        self.btn_conn = QPushButton("Connect")
        self.btn_conn.setObjectName("Primary")
        self.btn_conn.clicked.connect(self.toggle_connection)
        lay.addWidget(self.btn_conn)

        lay.addSpacing(T.S4)

        self.btn_cal = QPushButton("Calibrate upright")
        self.btn_cal.setEnabled(False)
        self.btn_cal.clicked.connect(self.calibrate)
        lay.addWidget(self.btn_cal)

        lay.addStretch(1)
        self.lbl_ident = _label("", "MonoDim")
        lay.addWidget(self.lbl_ident)
        return bar

    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(268)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(T.S4, T.S4, T.S4, T.S4)
        lay.setSpacing(T.S3)

        lay.addWidget(_label("CURRENT STATE", "Micro"))
        self.plate = StatePlate()
        lay.addWidget(self.plate)

        self.bubble = BubbleLevel()
        lay.addWidget(self.bubble)

        lay.addWidget(_hline())
        lay.addWidget(_label("AXES   accel mg · gyro ddps", "Micro"))
        self.axes = AxisBars()
        lay.addWidget(self.axes)

        lay.addWidget(_hline())
        lay.addWidget(_label("LIVE", "Micro"))
        self.lbl_metrics = _label("—", "Mono")
        self.lbl_metrics.setTextFormat(Qt.PlainText)
        lay.addWidget(self.lbl_metrics)

        lay.addWidget(_hline())
        lay.addWidget(_label("TIME IN POSTURE", "Micro"))
        self.lbl_dwell = _label("—", "MonoDim")
        lay.addWidget(self.lbl_dwell)

        lay.addStretch(1)
        return side

    def _primary(self) -> QWidget:
        main = QWidget()
        lay = QVBoxLayout(main)
        lay.setContentsMargins(T.S6, T.S4, T.S6, T.S4)
        lay.setSpacing(T.S3)

        top = QHBoxLayout()
        top.setSpacing(T.S4)

        left = QVBoxLayout()
        left.setSpacing(T.S2)
        hdr = QHBoxLayout()
        hdr.addWidget(_label("ORIENTATION", "Micro"))
        hdr.addStretch(1)
        self.shape_cb = QComboBox()
        self.shape_cb.addItems(["slab", "cube"])
        self.shape_cb.setFixedWidth(84)
        self.shape_cb.currentTextChanged.connect(
            lambda s: self.cube.set_shape(s))
        hdr.addWidget(self.shape_cb)
        left.addLayout(hdr)

        self.cube = OrientationView()
        left.addWidget(self.cube, 1)

        btns = QHBoxLayout()
        b_head = QPushButton("Zero heading")
        b_head.clicked.connect(self.zero_heading)
        b_peak = QPushButton("Clear peaks")
        b_peak.clicked.connect(self.clear_peaks)
        btns.addWidget(b_head); btns.addWidget(b_peak)
        left.addLayout(btns)

        wrap = QWidget()
        wrap.setLayout(left)
        wrap.setFixedWidth(336)
        top.addWidget(wrap)

        right = QVBoxLayout()
        right.setSpacing(T.S2)
        right.addWidget(_label("ACCELERATION MAGNITUDE", "Micro"))
        self.plot = TracePlot()
        right.addWidget(self.plot, 1)
        self.lbl_peaks = _label("—", "MonoDim")
        right.addWidget(self.lbl_peaks)
        top.addLayout(right, 1)

        lay.addLayout(top, 1)

        lay.addWidget(_hline())
        lay.addWidget(_label("FALL CASCADE", "Micro"))
        self.stepper = CascadeStepper()
        lay.addWidget(self.stepper)

        lay.addWidget(_hline())
        lay.addWidget(_label("EVENTS", "Micro"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(150)
        self.log.setPlaceholderText(
            "No events yet.\n"
            "Calibrate while standing still, then move around. "
            "Every fall candidate is logged here with the reason it was "
            "accepted or rejected."
        )
        lay.addWidget(self.log)
        return main

    def _inspector(self) -> QWidget:
        wrap = QWidget()
        wrap.setObjectName("Inspector")
        wrap.setFixedWidth(324)
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        inner = QWidget()
        scroll.setWidget(inner)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(T.S4, T.S4, T.S4, T.S4)
        lay.setSpacing(T.S3)

        # recording
        lay.addWidget(_label("RECORD A LABELLED SESSION", "Micro"))
        form = QFormLayout()
        form.setSpacing(T.S2)
        self.in_subject = QLineEdit("s01")
        self.in_mount = QComboBox(); self.in_mount.addItems(MOUNTS)
        self.in_label = QComboBox(); self.in_label.setEditable(True)
        self.in_label.addItems(LABELS)
        self.in_notes = QLineEdit()
        form.addRow(_label("Subject", "Caption"), self.in_subject)
        form.addRow(_label("Mount", "Caption"), self.in_mount)
        form.addRow(_label("Activity", "Caption"), self.in_label)
        form.addRow(_label("Notes", "Caption"), self.in_notes)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.btn_rec = QPushButton("Record")
        self.btn_rec.setEnabled(False)
        self.btn_rec.clicked.connect(self.start_recording)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_recording)
        row.addWidget(self.btn_rec); row.addWidget(self.btn_stop)
        lay.addLayout(row)
        self.lbl_rec = _label("not recording", "Caption")
        lay.addWidget(self.lbl_rec)

        lay.addWidget(_hline())

        # thresholds
        lay.addWidget(_label("DETECTION THRESHOLDS", "Micro"))
        lay.addWidget(_label(
            "Starting points from the literature, not tuned values. "
            "Change them live, then confirm against recorded data.", "Caption"))
        tf = QFormLayout()
        tf.setSpacing(T.S2)
        for attr, text, unit, lo, hi in TUNABLES:
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(getattr(self.engine.th, attr))
            sp.setSuffix(f" {unit}")
            sp.valueChanged.connect(
                lambda v, a=attr: setattr(self.engine.th, a, v))
            self._spins[attr] = sp
            tf.addRow(_label(text, "Caption"), sp)
        lay.addLayout(tf)

        b = QPushButton("Reset to defaults")
        b.clicked.connect(self.reset_thresholds)
        lay.addWidget(b)
        lay.addStretch(1)
        return wrap

    def _status(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Status")
        bar.setFixedHeight(26)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(T.S4, 0, T.S4, 0)
        self.lbl_status = _label("disconnected", "Caption")
        lay.addWidget(self.lbl_status)
        lay.addStretch(1)
        self.lbl_stream = _label("", "MonoDim")
        lay.addWidget(self.lbl_stream)
        return bar

    # ── actions ──────────────────────────────────────────────────────────────
    def refresh_ports(self) -> None:
        self.port_cb.clear()
        found = False
        for dev, desc, is_board in list_ports():
            tag = "  ✓ falldetect board" if is_board else "  (not the board)"
            self.port_cb.addItem(f"{dev} — {desc}{tag}", (dev, is_board))
            found = found or is_board
        if not found:
            self.say("board not found — is it plugged in, and is datalog or "
                     "the probe flashed?")

    def toggle_connection(self) -> None:
        if self.link:
            if self.link.recording:
                self.stop_recording()
            self.link.shutdown()
            self.link = None
            self.btn_conn.setText("Connect")
            for b in (self.btn_cal, self.btn_rec, self.btn_stop):
                b.setEnabled(False)
            self.say("disconnected")
            return

        data = self.port_cb.currentData()
        if not data:
            self.say("no serial port selected")
            return
        dev, is_board = data
        if not is_board:
            # Connecting to a Bluetooth virtual port succeeds and then delivers
            # nothing, forever. Say so at the moment of the mistake.
            self.say(f"WARNING {dev} is not the falldetect board — expect no data. "
                     "Pick the port marked ✓.")
        self.engine = Engine(thresholds=self.engine.th)
        self.link = DeviceLink(dev, self.engine)
        self.link.start()
        self.btn_conn.setText("Disconnect")
        self.btn_cal.setEnabled(True)
        self.btn_rec.setEnabled(True)

    def calibrate(self) -> None:
        if self.engine.calibrate_upright():
            self.say("calibrated — this orientation is now 'upright'")
        else:
            self.say("calibration failed — stand still and upright, then retry")

    def zero_heading(self) -> None:
        self.engine.reset_heading()
        self.say("heading zeroed — it drifts, so re-zero whenever it matters")

    def clear_peaks(self) -> None:
        self.engine.reset_peaks()
        self.say("peak hold cleared")

    def reset_thresholds(self) -> None:
        self.engine.th = Thresholds()
        for attr, sp in self._spins.items():
            sp.blockSignals(True)
            sp.setValue(getattr(self.engine.th, attr))
            sp.blockSignals(False)
        self.say("thresholds reset to defaults")

    def start_recording(self) -> None:
        if not self.link:
            return
        label = self.in_label.currentText().strip() or "unlabelled"
        mount = self.in_mount.currentText()
        subject = self.in_subject.text().strip() or "anon"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DATA_DIR / mount / label / f"{stamp}_{subject}_{label}.csv"
        header = [
            "falldetect-gkl fd_studio v1",
            f"label={label}", f"mount={mount}", f"subject={subject}",
            f"started={datetime.now().isoformat(timespec='seconds')}",
            "units=accel milli-g, gyro deci-dps",
            f"firmware={self.link.identity or 'unknown'}",
            f"notes={self.in_notes.text().strip()}",
        ]
        self.link.start_recording(path, header)
        self.btn_rec.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.say(f"recording → {path.relative_to(ROOT)}")

    def stop_recording(self) -> None:
        if not self.link:
            return
        path, n = self.link.stop_recording()
        self.btn_rec.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if path:
            self.say(f"saved {path.name} — {n} samples")

    def say(self, msg: str) -> None:
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {msg}")

    # ── tick ─────────────────────────────────────────────────────────────────
    def _tick(self) -> None:
        """Timer entry point. Never allowed to raise: an exception here stops
        the UI updating forever while the window still looks alive."""
        try:
            self._tick_inner()
        except Exception as exc:  # noqa: BLE001
            if not self._tick_broken:
                self._tick_broken = True
                self.say(f"internal error in refresh: {exc!r}")

    def _tick_inner(self) -> None:
        # Bind the link ONCE. _on_event can set self.link to None (an error tears
        # the connection down), and re-reading self.link each iteration then
        # dereferences None — which is not queue.Empty, so it escapes and kills
        # the timer callback permanently. Reflashing while connected does
        # exactly that, because the board re-enumerates under the open port.
        link = self.link
        if link is not None:
            try:
                while True:
                    kind, payload = link.events.get_nowait()
                    self._on_event(kind, payload)
            except queue.Empty:
                pass

        eng = self.engine
        now = time.time()
        falling = now < self._fall_until
        stats = self.link.stats() if self.link else None

        # Priority matters. "Not calibrated" on a port that is delivering
        # nothing sends you off calibrating instead of fixing the actual fault,
        # so silence outranks every other state except an active fall.
        if not self.link:
            self.plate.set_state("NOT CONNECTED", "select a port and connect",
                                 "text_3", False)
        elif falling:
            self.plate.set_state("FALL DETECTED", "check on the wearer",
                                 "danger", True)
        elif stats and stats["samples"] == 0:
            self.plate.set_state("NO DATA",
                                 "nothing arriving — wrong port, or firmware "
                                 "not flashed", "danger", True)
        elif stats and stats["stale"] > 3.0:
            self.plate.set_state("LINK LOST",
                                 f"no samples for {int(stats['stale'])}s — reconnect",
                                 "danger", True)
        elif not eng.calibrated:
            self.plate.set_state("NOT CALIBRATED",
                                 "stand upright and still, then Calibrate",
                                 "warning", True)
        else:
            tone = {"WALKING": "accent", "LYING": "warning"}.get(
                eng.activity.value if eng.activity.value == "WALKING"
                else eng.posture.value, "success")
            self.plate.set_state(eng.state_text(),
                                 f"tilt {eng.tilt:.0f}°   ·   {eng.steps} steps",
                                 tone, eng.activity.value == "WALKING")

        self.cube.set_pose(eng.rotation(), eng.tilt, eng.th.upright_max_deg,
                           eng.th.lying_min_deg, eng.heading_age)
        dx, dy = eng.tilt_components()
        self.bubble.set_lean(dx, dy, eng.th.upright_max_deg,
                             eng.th.lying_min_deg, eng.calibrated)
        self.axes.set_values(eng.last_raw)
        self.lbl_peaks.setText(
            f"peak {int(eng.peak_mag)} mg   ·   peak tilt {eng.peak_tilt:.0f}°"
            f"   ·   heading {eng.heading:+.0f}°")

        self.lbl_metrics.setText(
            f"magnitude  {int(eng.mag):>5d} mg\n"
            f"variance   {int(eng.std):>5d} mg\n"
            f"tilt       {eng.tilt:>5.0f} °\n"
            f"steps      {eng.steps:>5d}\n"
            f"step rate  {eng.step_rate:>5.1f} /s"
        )
        d = eng.dwell
        self.lbl_dwell.setText(
            f"upright   {d[Posture.UPRIGHT]:8.0f} s\n"
            f"reclined  {d[Posture.RECLINED]:8.0f} s\n"
            f"lying     {d[Posture.LYING]:8.0f} s"
        )

        if self.link:
            self.plot.set_data(self.link.trace_copy(),
                               eng.th.freefall_mg, eng.th.impact_mg)

        self.stepper.set_flags(eng.flags,
                               armed=eng.stage is not Stage.IDLE,
                               failed=eng.stage is Stage.REJECTED)

        if self.link and stats:
            s = stats
            if s["stale"] > 3.0:
                self.lbl_status.setText(
                    f"NO DATA for {int(s['stale'])}s — reflashed? reconnect")
            elif 0 < s["rate"] < 100:
                # A 100 ms free-fall is one sample at 10 Hz. Posture and steps
                # are still meaningful; fall detection is not.
                self.lbl_status.setText(
                    f"{s['rate']:.0f} Hz — too slow for fall detection. "
                    "Flash apps/datalog for 208 Hz.")
            else:
                self.lbl_status.setText("receiving")
            self.lbl_stream.setText(
                f"{s['rate']:.0f} Hz   gaps {s['gaps']}   "
                f"samples {s['samples']}"
                + (f"   rec {s['rec_n']}" if self.link.recording else ""))
            if self.link.recording:
                self.lbl_rec.setText(f"recording — {s['rec_n']} samples")
            else:
                self.lbl_rec.setText("not recording")

    def _on_event(self, kind: str, payload) -> None:
        if kind == "log":
            self.say(payload)
        elif kind == "identity":
            self.lbl_ident.setText(payload)
            self.say("device " + payload)
        elif kind == "error":
            self.say("ERROR " + payload)
            self.say("tip: reflashing re-enumerates the board and drops the "
                     "port — reconnect after flashing")
            if self.link:
                self.link.shutdown()
                self.link = None
            self.btn_conn.setText("Connect")
            for b in (self.btn_cal, self.btn_rec, self.btn_stop):
                b.setEnabled(False)
        elif kind == "fall":
            ev = payload
            tag = "FALL" if ev.confirmed else "candidate"
            self.say(
                f"{tag}: peak {ev.peak_mg} mg, min {ev.min_mg} mg, "
                f"tilt {ev.tilt_before:.0f}°→{ev.tilt_after:.0f}°, "
                f"still σ {ev.still_std:.0f} mg — {ev.reason()}"
            )
            if ev.confirmed:
                self._fall_until = time.time() + 6.0

    def closeEvent(self, e):
        if self.link:
            if self.link.recording:
                self.link.stop_recording()
            self.link.shutdown()
        super().closeEvent(e)


def main() -> int:
    T.use("dark")
    app = QApplication([])
    app.setStyleSheet(T.qss())
    win = MainWindow()
    win.show()
    return app.exec()
