#!/usr/bin/env python3
"""
Live visual for the falldetect-gkl candidate-rate probe.

Shows what the wearable actually sees:

  * the six raw IMU axes, and the acceleration MAGNITUDE derived from them,
  * a rolling trace of that magnitude with the free-fall and impact bands drawn,
  * how many hardware free-fall events each FF_THS threshold produced per hour.

The magnitude is the number that matters. A watch sits at an arbitrary rotation
on a wrist, so no single axis means anything on its own; sqrt(ax^2+ay^2+az^2) is
orientation-independent. At rest it reads ~1000 mg (gravity). Free-fall drives it
toward zero, impact drives it well above.

Wire format emitted by src/main.c:
    $A,<ax>,<ay>,<az>,<gx>,<gy>,<gz>    10 Hz, accel milli-g, gyro deci-dps
    $E,<uptime_s>,<ths>                 one hardware free-fall event
    $S,<uptime_s>,<ths>,<drift>,e0,s0,...,e7,s7   every 2 s

Dependencies: pyserial. tkinter is standard library.
    python -m pip install pyserial
"""

import math
import queue
import sys
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("pyserial is required:  python -m pip install pyserial", file=sys.stderr)
    sys.exit(1)


N_THS = 8
TRACE_LEN = 300          # 30 s at 10 Hz
FREEFALL_MG = 500        # below this is the free-fall band
IMPACT_MG = 2500         # above this is the impact band
ACC_BAR_FS = 2000        # bar display range, milli-g (true range is +/-16000)
GYR_BAR_FS = 5000        # bar display range, deci-dps (true range is +/-20000)

BG = "#14171b"
FG = "#e6e9ec"
DIM = "#7b8490"
GRID = "#262b32"
ACCENT = "#4ea3c8"
BRASS = "#c9a24d"
ALERT = "#e2624f"
OKC = "#5cbe91"


class Reader(threading.Thread):
    """Owns the serial port. Pushes parsed events onto a queue for the GUI."""

    def __init__(self, port, evq):
        super().__init__(daemon=True)
        self.port = port
        self.evq = evq
        self._stop = threading.Event()

    def shutdown(self):
        self._stop.set()

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
                # Zephyr's console emits CRLF, and USB packet boundaries can
                # leave a bare CR between records. Normalising both to LF means
                # a CR-joined pair is split instead of silently failing the
                # field-count check and being dropped.
                buf += data.replace(b"\r", b"\n")
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("ascii", "replace").strip()
                    if text:
                        self._parse(text)
        except Exception as exc:  # noqa: BLE001
            self.evq.put(("error", f"Serial error: {exc}"))
        finally:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.evq.put(("log", "Port closed"))

    def _parse(self, line):
        if not line:
            return
        if not line.startswith("$"):
            self.evq.put(("log", line))
            return
        parts = line.split(",")
        try:
            if parts[0] == "$A" and len(parts) == 7:
                self.evq.put(("sample", [int(v) for v in parts[1:7]]))
            elif parts[0] == "$E" and len(parts) == 3:
                self.evq.put(("event", (int(parts[1]), int(parts[2]))))
            elif parts[0] == "$S" and len(parts) == 4 + 2 * N_THS:
                self.evq.put(("status", [int(v) for v in parts[1:]]))
        except ValueError:
            # A truncated first line while syncing is expected, not an error.
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("falldetect-gkl — free-fall probe")
        self.configure(bg=BG)
        self.resizable(False, False)

        self.evq = queue.Queue()
        self.reader = None

        self.trace = deque([1000] * TRACE_LEN, maxlen=TRACE_LEN)
        self.axes = [0] * 6
        self.mag = 0
        self.uptime = 0
        self.cur_ths = 0
        self.drift = 0
        self.events = [0] * N_THS
        self.secs = [0] * N_THS
        self.event_flash = 0
        self.event_marks = deque(maxlen=TRACE_LEN)
        self.last_rx = 0.0

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self.pump)

    # -- layout ------------------------------------------------------------
    def _build(self):
        pad = {"padx": 8, "pady": 4}

        top = tk.Frame(self, bg=BG)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        self.port_cb = ttk.Combobox(top, width=38, state="readonly")
        self.port_cb.pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Refresh", command=self.refresh_ports).pack(side="left", padx=3)
        self.btn = ttk.Button(top, text="Connect", command=self.toggle)
        self.btn.pack(side="left", padx=3)
        self.lbl_state = tk.Label(top, text="disconnected", bg=BG, fg=DIM,
                                  font=("Consolas", 9))
        self.lbl_state.pack(side="left", padx=10)

        # magnitude readout + axes
        left = tk.Frame(self, bg=BG)
        left.grid(row=1, column=0, sticky="n", **pad)

        tk.Label(left, text="MAGNITUDE", bg=BG, fg=DIM,
                 font=("Consolas", 9)).pack(anchor="w")
        self.lbl_mag = tk.Label(left, text="----", bg=BG, fg=FG,
                                font=("Consolas", 34, "bold"))
        self.lbl_mag.pack(anchor="w")
        tk.Label(left, text="milli-g   sqrt(ax²+ay²+az²)", bg=BG, fg=DIM,
                 font=("Consolas", 8)).pack(anchor="w")

        self.axes_cv = tk.Canvas(left, width=250, height=170, bg=BG,
                                 highlightthickness=0)
        self.axes_cv.pack(anchor="w", pady=(10, 0))

        self.lbl_stats = tk.Label(left, text="", bg=BG, fg=FG, justify="left",
                                  font=("Consolas", 9))
        self.lbl_stats.pack(anchor="w", pady=(10, 0))

        # trace + histogram
        right = tk.Frame(self, bg=BG)
        right.grid(row=1, column=1, sticky="n", **pad)

        tk.Label(right, text="MAGNITUDE — last 30 s", bg=BG, fg=DIM,
                 font=("Consolas", 9)).pack(anchor="w")
        self.trace_cv = tk.Canvas(right, width=620, height=190, bg=BG,
                                  highlightthickness=1,
                                  highlightbackground=GRID)
        self.trace_cv.pack(anchor="w")

        tk.Label(right, text="FREE-FALL EVENTS PER HOUR, BY THRESHOLD CODE",
                 bg=BG, fg=DIM, font=("Consolas", 9)).pack(anchor="w", pady=(10, 0))
        self.hist_cv = tk.Canvas(right, width=620, height=150, bg=BG,
                                 highlightthickness=1, highlightbackground=GRID)
        self.hist_cv.pack(anchor="w")

        self.log = tk.Text(self, height=7, width=104, bg="#0e1114", fg=DIM,
                           font=("Consolas", 8), state="disabled",
                           highlightthickness=0, bd=0)
        self.log.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)

        self.refresh_ports()

    # -- serial ------------------------------------------------------------
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

    def toggle(self):
        if self.reader:
            self.reader.shutdown()
            self.reader = None
            self.btn.configure(text="Connect")
            self.lbl_state.configure(text="disconnected", fg=DIM)
            return
        raw = self.port_cb.get()
        if not raw:
            messagebox.showwarning("No port", "Select a serial port first.")
            return
        self.last_rx = time.time()
        self.reader = Reader(raw.split(" - ")[0], self.evq)
        self.reader.start()
        self.btn.configure(text="Disconnect")
        self.lbl_state.configure(text="connected", fg=OKC)

    def say(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- drawing -----------------------------------------------------------
    def draw_axes(self):
        c = self.axes_cv
        c.delete("all")
        names = ["ax", "ay", "az", "gx", "gy", "gz"]
        units = ["mg", "mg", "mg", "dd", "dd", "dd"]
        w, x0, bw = 250, 34, 150
        mid = x0 + bw / 2

        for i, (n, u) in enumerate(zip(names, units)):
            y = 14 + i * 26
            val = self.axes[i]
            fs = ACC_BAR_FS if i < 3 else GYR_BAR_FS
            frac = max(-1.0, min(1.0, val / fs))

            c.create_text(x0 - 6, y, text=n, fill=DIM, anchor="e",
                          font=("Consolas", 9))
            c.create_rectangle(x0, y - 7, x0 + bw, y + 7, outline=GRID)
            c.create_line(mid, y - 8, mid, y + 8, fill=GRID)

            col = BRASS if i < 3 else ACCENT
            if frac >= 0:
                c.create_rectangle(mid, y - 6, mid + frac * bw / 2, y + 6,
                                   fill=col, outline="")
            else:
                c.create_rectangle(mid + frac * bw / 2, y - 6, mid, y + 6,
                                   fill=col, outline="")
            c.create_text(w - 2, y, text=f"{val:>6d}", fill=FG, anchor="e",
                          font=("Consolas", 9))

    def draw_trace(self):
        c = self.trace_cv
        c.delete("all")
        w, h = 620, 190
        top_mg = 4000.0

        def y_of(mg):
            return h - 10 - (min(mg, top_mg) / top_mg) * (h - 20)

        # bands: free-fall below, impact above
        c.create_rectangle(0, y_of(FREEFALL_MG), w, h, fill="#1d1a16", outline="")
        c.create_rectangle(0, 0, w, y_of(IMPACT_MG), fill="#1e1517", outline="")

        for mg in (0, 1000, 2000, 3000, 4000):
            y = y_of(mg)
            c.create_line(0, y, w, y, fill=GRID)
            c.create_text(4, y - 7, text=f"{mg}", fill=DIM, anchor="w",
                          font=("Consolas", 7))

        c.create_line(0, y_of(1000), w, y_of(1000), fill=DIM, dash=(2, 3))

        step = w / float(TRACE_LEN - 1)
        pts = []
        for i, mg in enumerate(self.trace):
            pts.extend([i * step, y_of(mg)])
        if len(pts) >= 4:
            c.create_line(*pts, fill=ACCENT, width=2, smooth=False)

        # event markers
        for idx in self.event_marks:
            x = idx * step
            c.create_line(x, 0, x, h, fill=ALERT, width=1)

        cur = self.trace[-1] if self.trace else 0
        col = ALERT if (cur < FREEFALL_MG or cur > IMPACT_MG) else FG
        c.create_text(w - 6, 12, text=f"{cur} mg", fill=col, anchor="e",
                      font=("Consolas", 11, "bold"))

    def draw_hist(self):
        c = self.hist_cv
        c.delete("all")
        w, h = 620, 150
        base = h - 24
        bw = w / float(N_THS)

        rates = []
        for i in range(N_THS):
            s = self.secs[i]
            rates.append((self.events[i] * 3600.0 / s) if s > 0 else 0.0)
        top = max(max(rates), 1.0) * 1.2

        for i in range(N_THS):
            x = i * bw
            cur = (i == self.cur_ths)
            hgt = (rates[i] / top) * (base - 16)
            col = BRASS if cur else "#3d4650"

            if cur:
                c.create_rectangle(x + 2, 2, x + bw - 2, h - 2,
                                   outline=BRASS, dash=(2, 2))
            c.create_rectangle(x + 12, base - hgt, x + bw - 12, base,
                               fill=col, outline="")
            c.create_text(x + bw / 2, base + 12, text=str(i),
                          fill=FG if cur else DIM, font=("Consolas", 9))
            label = f"{rates[i]:.1f}" if self.secs[i] else "-"
            c.create_text(x + bw / 2, base - hgt - 9, text=label,
                          fill=FG if cur else DIM, font=("Consolas", 8))
            c.create_text(x + bw / 2, base - hgt - 21,
                          text=f"n={self.events[i]}" if self.secs[i] else "",
                          fill=DIM, font=("Consolas", 7))

        c.create_line(0, base, w, base, fill=GRID)
        c.create_text(w - 4, 10, text="events/hour", fill=DIM, anchor="e",
                      font=("Consolas", 8))

    # -- event pump --------------------------------------------------------
    def pump(self):
        try:
            while True:
                kind, payload = self.evq.get_nowait()
                self.on_event(kind, payload)
        except queue.Empty:
            pass

        self.draw_axes()
        self.draw_trace()
        self.draw_hist()

        total = sum(self.events)
        self.lbl_stats.configure(
            text=(f"uptime   {self.uptime // 3600}h{(self.uptime % 3600) // 60:02d}m\n"
                  f"FF_THS   {self.cur_ths}\n"
                  f"events   {total}\n"
                  f"drift    {self.drift}")
        )
        if self.event_flash > 0:
            self.event_flash -= 1
            self.lbl_mag.configure(fg=ALERT)
        else:
            cur = self.trace[-1] if self.trace else 0
            self.lbl_mag.configure(
                fg=ALERT if (cur < FREEFALL_MG or cur > IMPACT_MG) else FG)

        # A stale handle after the board re-enumerates (which reflashing always
        # causes) reports "connected" forever while delivering nothing. Say so
        # rather than letting a dead link look like a calm device.
        if self.reader:
            age = time.time() - self.last_rx
            if age > 3.0:
                self.lbl_state.configure(
                    text=f"NO DATA for {int(age)}s — reconnect", fg=ALERT)
            else:
                self.lbl_state.configure(text="receiving", fg=OKC)

        self.after(50, self.pump)

    def on_event(self, kind, payload):
        self.last_rx = time.time()
        if kind == "sample":
            self.axes = payload
            ax, ay, az = payload[0], payload[1], payload[2]
            self.mag = int(math.sqrt(ax * ax + ay * ay + az * az))
            self.trace.append(self.mag)
            self.lbl_mag.configure(text=str(self.mag))
            self.event_marks = deque(
                (i - 1 for i in self.event_marks if i > 0), maxlen=TRACE_LEN)
        elif kind == "event":
            uptime, ths = payload
            self.event_marks.append(TRACE_LEN - 1)
            self.event_flash = 8
            self.say(f"FREE-FALL  t={uptime}s  FF_THS={ths}")
        elif kind == "status":
            self.uptime, self.cur_ths, self.drift = payload[0], payload[1], payload[2]
            rest = payload[3:]
            self.events = rest[0::2]
            self.secs = rest[1::2]
        elif kind == "log":
            self.say(payload)
        elif kind == "error":
            self.say("ERROR: " + payload)
            messagebox.showerror("Serial", payload)
            if self.reader:
                self.reader.shutdown()
                self.reader = None
            self.btn.configure(text="Connect")
            self.lbl_state.configure(text="disconnected", fg=DIM)

    def on_close(self):
        if self.reader:
            self.reader.shutdown()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
