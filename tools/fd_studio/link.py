"""
Serial link to apps/datalog, and the thread that drives the engine.

The engine runs on the reader thread, not the GUI thread. At 208 Hz a per-sample
Qt signal would mean 208 queued events a second and a UI that spends its life
repainting; instead the thread owns the engine and the GUI samples a snapshot on
a timer. Repaint cost is the single biggest reason a tool feels cheap.

Discrete things the GUI must not miss — fall events, identity, errors — go
through a queue, because dropping one of those is not a cosmetic problem.
"""

from __future__ import annotations

import queue
import threading
import time

import serial
import serial.tools.list_ports

from .engine import Engine


BOARD_VIDPID = "VID:PID=2FE3:0100"


def list_ports() -> list[tuple[str, str, bool]]:
    """Ports as (device, description, is_board), best candidate first.

    Windows reports the board as a generic "USB Serial Device", which looks
    identical to a Bluetooth virtual port in a dropdown. Matching the VID/PID is
    the only reliable way to tell them apart, and connecting to a Bluetooth port
    by mistake produces a tool that sits there looking connected forever.
    """
    ports = list(serial.tools.list_ports.comports())

    def is_board(p) -> bool:
        return BOARD_VIDPID in (p.hwid or "").upper()

    def rank(p):
        if is_board(p):
            return 0
        if "bluetooth" in (p.description or "").lower():
            return 2
        return 1

    ports.sort(key=rank)
    return [(p.device, p.description or "", is_board(p)) for p in ports]


class DeviceLink(threading.Thread):
    def __init__(self, port: str, engine: Engine):
        super().__init__(daemon=True)
        self.port = port
        self.engine = engine
        self.events: queue.Queue = queue.Queue()

        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.samples = 0
        self.gaps = 0
        self.last_seq: int | None = None
        self.last_rx = time.time()
        self.rate = 0.0
        self.identity = ""

        self._csv = None
        self._csv_n = 0
        self._synth_seq = 0

        self._rate_mark = (time.time(), 0)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            ser = serial.Serial(self.port, 115200, timeout=0.2)
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", f"Cannot open {self.port}: {exc}"))
            return

        self.events.put(("log", f"connected to {self.port}"))
        buf = b""
        try:
            while not self._stop.is_set():
                data = ser.read(ser.in_waiting or 1)
                if not data:
                    continue
                self.last_rx = time.time()
                # CRLF, plus USB packet boundaries can leave a bare CR.
                buf += data.replace(b"\r", b"\n")
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("ascii", "replace").strip()
                    if line:
                        self._parse(line)
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", f"serial error: {exc}"))
        finally:
            self.stop_recording()
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.events.put(("log", "port closed"))

    # ── parsing ──────────────────────────────────────────────────────────────
    def _parse(self, line: str) -> None:
        if not line.startswith("$"):
            self.events.put(("log", line))
            return
        p = line.split(",")
        try:
            if p[0] == "$D" and len(p) == 8:
                self._sample(int(p[1]), [int(v) for v in p[2:8]])
            elif p[0] == "$A" and len(p) == 7:
                # The candidate-rate probe (root firmware) emits $A at 10 Hz
                # with no sequence number. Accepted so the tool is never
                # mysteriously blank on the wrong image — but 10 Hz cannot
                # resolve a 100 ms free-fall, and the status strip says so.
                self._synth_seq += 1
                self._sample(self._synth_seq, [int(v) for v in p[1:7]])
            elif p[0] == "$P" and len(p) == 3:
                self.engine.push_steps(int(p[2]), time.time())
            elif p[0] == "$I":
                self.identity = line
                self.events.put(("identity", line))
            elif p[0] == "$X":
                self.events.put(("log", "DEVICE ERROR " + line))
        except ValueError:
            pass  # a truncated first line while syncing is expected

    def _sample(self, seq: int, v: list[int]) -> None:
        now = time.time()

        with self._lock:
            if self.last_seq is not None and ((seq - self.last_seq) & 0xFFFFFFFF) != 1:
                self.gaps += 1
            self.last_seq = seq
            self.samples += 1

            t0, n0 = self._rate_mark
            if now - t0 >= 1.0:
                self.rate = (self.samples - n0) / (now - t0)
                self._rate_mark = (now, self.samples)

            ev = self.engine.push_sample(v[0], v[1], v[2], v[3], v[4], v[5], now)

            if self._csv is not None:
                self._csv.write(f"{seq},{v[0]},{v[1]},{v[2]},{v[3]},{v[4]},{v[5]}\n")
                self._csv_n += 1

        if ev is not None:
            self.events.put(("fall", ev))

    # ── recording ────────────────────────────────────────────────────────────
    def start_recording(self, path, header_lines) -> None:
        with self._lock:
            self._close_csv()
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "w", newline="", encoding="ascii")
            for line in header_lines:
                fh.write(f"# {line}\n")
            fh.write("seq,ax,ay,az,gx,gy,gz\n")
            self._csv = fh
            self._csv_path = path
            self._csv_n = 0

    def stop_recording(self):
        with self._lock:
            if self._csv is None:
                return None, 0
            self._csv.write(f"# samples={self._csv_n}\n")
            self._csv.write(f"# gaps={self.gaps}\n")
            path, n = self._csv_path, self._csv_n
            self._close_csv()
            return path, n

    def _close_csv(self) -> None:
        if self._csv is not None:
            try:
                self._csv.close()
            except Exception:  # noqa: BLE001
                pass
        self._csv = None

    @property
    def recording(self) -> bool:
        return self._csv is not None

    # ── snapshot for the GUI ─────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            return {
                "samples": self.samples,
                "gaps": self.gaps,
                "rate": self.rate,
                "stale": time.time() - self.last_rx,
                "rec_n": self._csv_n if self._csv else 0,
            }

    def trace_copy(self) -> list:
        with self._lock:
            return list(self.engine.trace)
