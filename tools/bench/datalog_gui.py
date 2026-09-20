#!/usr/bin/env python3
"""
Labelled activity recorder for falldetect-gkl.

Records IMU sessions from apps/datalog into CSV files you can train on. Pick a
mount point and an activity, press Record, perform the activity, press Stop.

Every file carries a provenance header — mount, subject, ODR, full-scale range,
firmware identity, and the measured gap count. Without that a corpus becomes
uninterpretable within weeks: nobody can later say whether a recording was neck
or wrist, or at what range, and the whole set has to be thrown away.

Files land in:  data/<mount>/<label>/<timestamp>_<subject>_<label>.csv

Wire format from the firmware:
    $I,<odr>,<accel_fs_g>,<gyro_fs_dps>,<pedo_ok>
    $D,<seq>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
    $P,<seq>,<steps>

Dependencies: pyserial. tkinter is standard library.
    python -m pip install pyserial
"""

import math
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("pyserial is required:  python -m pip install pyserial", file=sys.stderr)
    sys.exit(1)


# tools/bench/ -> tools/ -> repo root.
ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"

MOUNTS = ["neck", "wrist", "waist", "pocket"]

# Grouped so the operator is nudged toward a balanced corpus rather than
# recording twenty walks and one fall.
LABELS = [
    "— posture & motion —",
    "walking", "standing", "sitting", "lying", "walking_slow", "stairs_up", "stairs_down",
    "— transitions —",
    "sit_to_stand", "stand_to_sit", "lying_to_sit", "bending_picking_up",
    "— confounders —",
    "sitting_down_heavily", "walking_stick", "clapping", "brushing_teeth",
    "device_dropped_on_table", "device_put_on_taken_off", "jumping",
    "— falls (crash mat only) —",
    "fall_forward", "fall_backward", "fall_left", "fall_right",
    "fall_from_chair", "fall_slow_slump",
]

BG = "#14171b"
FG = "#e6e9ec"
DIM = "#7b8490"
GRID = "#262b32"
ACCENT = "#4ea3c8"
BRASS = "#c9a24d"
ALERT = "#e2624f"
OKC = "#5cbe91"

TRACE_LEN = 240


class Reader(threading.Thread):
    """Owns the port and the open CSV. Counters are read by the GUI thread."""

    def __init__(self, port, evq):
        super().__init__(daemon=True)
        self.port = port
        self.evq = evq
        self._stop = threading.Event()

        self.lock = threading.Lock()
        self.fh = None
        self.path = None

        self.samples = 0
        self.gaps = 0
        self.last_seq = None
        self.steps = 0
        self.mag = 0
        self.trace = deque([1000] * TRACE_LEN, maxlen=TRACE_LEN)
        self.rx_count = 0
        self.last_rx = time.time()
        self.identity = None
        self.t_start = None

    def shutdown(self):
        self._stop.set()

    # -- recording (called from GUI thread) --------------------------------
    def start_record(self, path, header_lines):
        with self.lock:
            self._close()
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "w", newline="", encoding="ascii")
            for line in header_lines:
                fh.write(f"# {line}\n")
            fh.write("seq,ax,ay,az,gx,gy,gz\n")
            self.fh = fh
            self.path = path
            self.samples = 0
            self.gaps = 0
            self.last_seq = None
            self.t_start = time.time()

    def stop_record(self):
        with self.lock:
            path, n, gaps = self.path, self.samples, self.gaps
            dur = (time.time() - self.t_start) if self.t_start else 0.0
            if self.fh:
                self.fh.write(f"# samples={n}\n")
                self.fh.write(f"# gaps={gaps}\n")
                self.fh.write(f"# duration_s={dur:.2f}\n")
                self.fh.write(f"# effective_hz={(n / dur) if dur > 0 else 0:.1f}\n")
            self._close()
            return path, n, gaps, dur

    def _close(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:  # noqa: BLE001
                pass
        self.fh = None

    @property
    def recording(self):
        return self.fh is not None

    # -- thread ------------------------------------------------------------
    def run(self):
        try:
            ser = serial.Serial(self.port, 115200, timeout=0.2)
        except Exception as exc:  # noqa: BLE001
            self.evq.put(("error", f"Cannot open {self.port}: {exc}"))
            return

        self.evq.put(("log", f"Connected to {self.port}"))
        buf = b""
        try:
            while not self._stop.is_set():
                data = ser.read(ser.in_waiting or 1)
                if not data:
                    continue
                self.last_rx = time.time()
                # CRLF, and USB packet boundaries can leave a bare CR.
                buf += data.replace(b"\r", b"\n")
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("ascii", "replace").strip()
                    if text:
                        self._parse(text)
        except Exception as exc:  # noqa: BLE001
            self.evq.put(("error", f"Serial error: {exc}"))
        finally:
            with self.lock:
                self._close()
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.evq.put(("log", "Port closed"))

    def _parse(self, line):
        if not line.startswith("$"):
            self.evq.put(("log", line))
            return
        p = line.split(",")
        try:
            if p[0] == "$D" and len(p) == 8:
                self._on_sample(int(p[1]), [int(v) for v in p[2:8]])
            elif p[0] == "$P" and len(p) == 3:
                self.steps = int(p[2])
            elif p[0] == "$I":
                self.identity = line
                self.evq.put(("identity", line))
            elif p[0] == "$X":
                self.evq.put(("log", "DEVICE ERROR: " + line))
        except ValueError:
            pass

    def _on_sample(self, seq, vals):
        self.rx_count += 1
        ax, ay, az = vals[0], vals[1], vals[2]
        self.mag = int(math.sqrt(ax * ax + ay * ay + az * az))
        self.trace.append(self.mag)

        with self.lock:
            if self.fh is None:
                self.last_seq = seq
                return
            # A jump in seq means samples went missing between here and the
            # device. Count it rather than let the hole pass unnoticed.
            if self.last_seq is not None:
                step = (seq - self.last_seq) & 0xFFFFFFFF
                if step != 1:
                    self.gaps += 1
            self.last_seq = seq
            self.samples += 1
            self.fh.write(f"{seq},{vals[0]},{vals[1]},{vals[2]},"
                          f"{vals[3]},{vals[4]},{vals[5]}\n")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("falldetect-gkl — activity recorder")
        self.configure(bg=BG)
        self.resizable(False, False)

        self.evq = queue.Queue()
        self.reader = None
        self.identity = ""
        self.prev_rx = 0
        self.prev_t = time.time()
        self.hz = 0.0

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.pump)

    def _build(self):
        pad = {"padx": 8, "pady": 4}

        top = tk.Frame(self, bg=BG)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        self.port_cb = ttk.Combobox(top, width=34, state="readonly")
        self.port_cb.pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Refresh", command=self.refresh_ports).pack(side="left", padx=3)
        self.btn_conn = ttk.Button(top, text="Connect", command=self.toggle_conn)
        self.btn_conn.pack(side="left", padx=3)
        self.lbl_state = tk.Label(top, text="disconnected", bg=BG, fg=DIM,
                                  font=("Consolas", 9))
        self.lbl_state.pack(side="left", padx=10)

        # session metadata
        meta = tk.LabelFrame(self, text=" session ", bg=BG, fg=DIM,
                             font=("Consolas", 9), bd=1, relief="solid")
        meta.grid(row=1, column=0, sticky="nw", **pad)

        tk.Label(meta, text="subject", bg=BG, fg=DIM,
                 font=("Consolas", 9)).grid(row=0, column=0, sticky="e", padx=6, pady=3)
        self.subject = ttk.Entry(meta, width=16)
        self.subject.insert(0, "s01")
        self.subject.grid(row=0, column=1, padx=6, pady=3)

        tk.Label(meta, text="mount", bg=BG, fg=DIM,
                 font=("Consolas", 9)).grid(row=1, column=0, sticky="e", padx=6, pady=3)
        self.mount = ttk.Combobox(meta, width=14, values=MOUNTS, state="readonly")
        self.mount.current(0)
        self.mount.grid(row=1, column=1, padx=6, pady=3)

        tk.Label(meta, text="activity", bg=BG, fg=DIM,
                 font=("Consolas", 9)).grid(row=2, column=0, sticky="e", padx=6, pady=3)
        self.label = ttk.Combobox(meta, width=24, values=LABELS)
        self.label.set("walking")
        self.label.grid(row=2, column=1, padx=6, pady=3)

        tk.Label(meta, text="notes", bg=BG, fg=DIM,
                 font=("Consolas", 9)).grid(row=3, column=0, sticky="e", padx=6, pady=3)
        self.notes = ttk.Entry(meta, width=24)
        self.notes.grid(row=3, column=1, padx=6, pady=3)

        self.btn_rec = ttk.Button(meta, text="●  Record", width=14,
                                  command=self.start_rec, state="disabled")
        self.btn_rec.grid(row=4, column=0, padx=6, pady=(10, 8))
        self.btn_stop = ttk.Button(meta, text="■  Stop", width=14,
                                   command=self.stop_rec, state="disabled")
        self.btn_stop.grid(row=4, column=1, padx=6, pady=(10, 8))

        # live
        live = tk.Frame(self, bg=BG)
        live.grid(row=1, column=1, sticky="nw", **pad)

        self.lbl_live = tk.Label(live, text="", bg=BG, fg=FG, justify="left",
                                 font=("Consolas", 10))
        self.lbl_live.pack(anchor="w")

        self.trace_cv = tk.Canvas(live, width=470, height=130, bg=BG,
                                  highlightthickness=1, highlightbackground=GRID)
        self.trace_cv.pack(anchor="w", pady=(8, 0))

        self.lbl_rec = tk.Label(live, text="idle", bg=BG, fg=DIM,
                                font=("Consolas", 11, "bold"))
        self.lbl_rec.pack(anchor="w", pady=(8, 0))

        self.log = tk.Text(self, height=8, width=96, bg="#0e1114", fg=DIM,
                           font=("Consolas", 8), state="disabled",
                           highlightthickness=0, bd=0)
        self.log.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)

        self.refresh_ports()

    # -- helpers -----------------------------------------------------------
    def say(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        self.port_cb["values"] = [f"{p.device} - {p.description}" for p in ports]
        if not ports:
            return

        def rank(p):
            d = (p.description or "").lower()
            h = (p.hwid or "").upper()
            if "VID:PID=2FE3:0100" in h:
                return 0
            if "bluetooth" in d:
                return 2
            return 1

        self.port_cb.current(min(range(len(ports)), key=lambda i: rank(ports[i])))

    def toggle_conn(self):
        if self.reader:
            if self.reader.recording:
                self.stop_rec()
            self.reader.shutdown()
            self.reader = None
            self.btn_conn.configure(text="Connect")
            self.btn_rec.configure(state="disabled")
            self.lbl_state.configure(text="disconnected", fg=DIM)
            return
        raw = self.port_cb.get()
        if not raw:
            messagebox.showwarning("No port", "Select a serial port first.")
            return
        self.reader = Reader(raw.split(" - ")[0], self.evq)
        self.reader.start()
        self.btn_conn.configure(text="Disconnect")
        self.btn_rec.configure(state="normal")

    # -- recording ---------------------------------------------------------
    def start_rec(self):
        if not self.reader:
            return
        label = self.label.get().strip()
        if not label or label.startswith("—"):
            messagebox.showwarning("Pick an activity",
                                   "Choose a real activity, not a group heading.")
            return
        subject = self.subject.get().strip() or "anon"
        mount = self.mount.get()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DATA_DIR / mount / label / f"{stamp}_{subject}_{label}.csv"

        header = [
            "falldetect-gkl datalog v1",
            f"label={label}",
            f"mount={mount}",
            f"subject={subject}",
            f"started={datetime.now().isoformat(timespec='seconds')}",
            "units=accel milli-g, gyro deci-dps",
            f"firmware={self.identity or 'unknown'}",
            f"notes={self.notes.get().strip()}",
        ]
        self.reader.start_record(path, header)
        self.btn_rec.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_rec.configure(text=f"RECORDING  {label}", fg=ALERT)
        self.say(f"recording -> {path.relative_to(ROOT)}")

    def stop_rec(self):
        if not self.reader or not self.reader.recording:
            return
        path, n, gaps, dur = self.reader.stop_record()
        self.btn_rec.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        hz = (n / dur) if dur > 0 else 0
        self.lbl_rec.configure(text="idle", fg=DIM)
        self.say(f"saved {path.name}  {n} samples  {dur:.1f}s  {hz:.1f} Hz  gaps={gaps}")
        if gaps:
            self.say("WARNING: sample gaps — this recording has holes in it")

    # -- draw --------------------------------------------------------------
    def draw_trace(self):
        c = self.trace_cv
        c.delete("all")
        w, h, top_mg = 470, 130, 4000.0

        def y_of(mg):
            return h - 8 - (min(mg, top_mg) / top_mg) * (h - 16)

        for mg in (0, 1000, 2000, 3000, 4000):
            y = y_of(mg)
            c.create_line(0, y, w, y, fill=GRID)
            c.create_text(3, y - 6, text=str(mg), fill=DIM, anchor="w",
                          font=("Consolas", 7))
        c.create_line(0, y_of(1000), w, y_of(1000), fill=DIM, dash=(2, 3))

        if not self.reader:
            return
        trace = list(self.reader.trace)
        step = w / float(len(trace) - 1)
        pts = []
        for i, mg in enumerate(trace):
            pts.extend([i * step, y_of(mg)])
        if len(pts) >= 4:
            col = ALERT if self.reader.recording else ACCENT
            c.create_line(*pts, fill=col, width=2)

    # -- pump --------------------------------------------------------------
    def pump(self):
        try:
            while True:
                kind, payload = self.evq.get_nowait()
                if kind == "log":
                    self.say(payload)
                elif kind == "identity":
                    self.identity = payload
                    self.say("device: " + payload)
                elif kind == "error":
                    self.say("ERROR: " + payload)
                    messagebox.showerror("Serial", payload)
                    if self.reader:
                        self.reader.shutdown()
                        self.reader = None
                    self.btn_conn.configure(text="Connect")
                    self.btn_rec.configure(state="disabled")
                    self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass

        r = self.reader
        if r:
            now = time.time()
            dt = now - self.prev_t
            if dt >= 1.0:
                self.hz = (r.rx_count - self.prev_rx) / dt
                self.prev_rx, self.prev_t = r.rx_count, now

            stale = now - r.last_rx
            if stale > 3.0:
                self.lbl_state.configure(text=f"NO DATA for {int(stale)}s — reconnect",
                                         fg=ALERT)
            else:
                self.lbl_state.configure(text="receiving", fg=OKC)

            dur = (now - r.t_start) if (r.recording and r.t_start) else 0.0
            self.lbl_live.configure(
                text=(f"rate     {self.hz:6.1f} Hz\n"
                      f"mag      {r.mag:6d} mg\n"
                      f"steps    {r.steps:6d}\n"
                      f"recorded {r.samples:6d} samples   {dur:5.1f} s\n"
                      f"gaps     {r.gaps:6d}"))

        self.draw_trace()
        self.after(100, self.pump)

    def on_close(self):
        if self.reader:
            if self.reader.recording:
                self.reader.stop_record()
            self.reader.shutdown()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
