#!/usr/bin/env python3
"""
Recorder GUI for the falldetect-gkl PDM microphone firmware (apps/mic_record).

Connect, press Record, speak, press Stop, press Play. The recording is written
as a .wav into ./recordings/.

Wire format (little-endian), matching apps/mic_record/src/main.c:

    0..3  magic 'F','D','A','1'
    4     type   1=AUDIO 2=TEXT 3=START 4=STOP
    5..6  seq    (wraps at 65536)
    7..8  len    (payload bytes)
    9..   payload

Dependencies: pyserial. Everything else - tkinter, wave, winsound - is stdlib.

    pip install pyserial
"""

import array
import os
import queue
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("pyserial is required:  pip install pyserial", file=sys.stderr)
    sys.exit(1)

try:
    import winsound  # Windows only; playback degrades gracefully without it.
except ImportError:
    winsound = None


MAGIC = b"FDA1"
HDR_LEN = 9

FRAME_AUDIO = 0x01
FRAME_TEXT = 0x02
FRAME_START = 0x03
FRAME_STOP = 0x04

# tools/bench/ -> tools/ -> repo root.
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "recordings"


class Reader(threading.Thread):
    """Owns the serial port and the WAV file. Pushes events to the GUI queue.

    All file I/O happens here so the GUI thread never blocks; tkinter widgets
    are never touched from this thread.
    """

    def __init__(self, port, evq):
        super().__init__(daemon=True)
        self.port = port
        self.evq = evq
        self._stop = threading.Event()
        self._cmd = queue.Queue()
        self.ser = None

        self.buf = bytearray()
        self.wav = None
        self.wav_path = None
        self.expect_seq = None
        self.gaps = 0
        self.resyncs = 0
        self.frames = 0
        self.audio_bytes = 0
        self.rate = 16000
        self.bits = 16
        self.channels = 1

    # -- public, called from GUI thread ------------------------------------
    def send(self, byte):
        self._cmd.put(byte)

    def shutdown(self):
        self._stop.set()

    # -- internals ---------------------------------------------------------
    def _emit(self, kind, **kw):
        self.evq.put((kind, kw))

    def run(self):
        try:
            self.ser = serial.Serial(self.port, 115200, timeout=0.05)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self._emit("error", msg=f"Cannot open {self.port}: {exc}")
            return

        self._emit("status", msg=f"Connected to {self.port}")

        try:
            while not self._stop.is_set():
                while not self._cmd.empty():
                    self.ser.write(self._cmd.get_nowait())
                    self.ser.flush()

                n = self.ser.in_waiting
                data = self.ser.read(n if n else 1)
                if data:
                    self.buf.extend(data)
                    self._parse()
        except Exception as exc:  # noqa: BLE001
            self._emit("error", msg=f"Serial error: {exc}")
        finally:
            self._close_wav()
            if self.ser and self.ser.is_open:
                self.ser.close()
            self._emit("closed")

    def _parse(self):
        while True:
            i = self.buf.find(MAGIC)
            if i < 0:
                # Keep a short tail: the magic may straddle two reads.
                if len(self.buf) > 3:
                    del self.buf[: len(self.buf) - 3]
                return
            if i > 0:
                # Bytes before a valid magic mean we lost sync somewhere.
                del self.buf[:i]
                self.resyncs += 1
            if len(self.buf) < HDR_LEN:
                return

            ftype = self.buf[4]
            seq = int.from_bytes(self.buf[5:7], "little")
            ln = int.from_bytes(self.buf[7:9], "little")

            if len(self.buf) < HDR_LEN + ln:
                return

            payload = bytes(self.buf[HDR_LEN : HDR_LEN + ln])
            del self.buf[: HDR_LEN + ln]
            self._handle(ftype, seq, payload)

    def _handle(self, ftype, seq, payload):
        # seq counts every frame, so a gap means real data went missing.
        if self.expect_seq is not None and seq != self.expect_seq:
            self.gaps += 1
            self._emit("status", msg=f"Sequence gap: expected {self.expect_seq}, got {seq}")
        self.expect_seq = (seq + 1) & 0xFFFF
        self.frames += 1

        if ftype == FRAME_TEXT:
            self._emit("status", msg="device: " + payload.decode("ascii", "replace"))

        elif ftype == FRAME_START:
            if len(payload) >= 8:
                self.rate = int.from_bytes(payload[0:4], "little")
                self.bits = int.from_bytes(payload[4:6], "little")
                self.channels = int.from_bytes(payload[6:8], "little")
            self._open_wav()
            self._emit(
                "started",
                msg=f"Recording {self.rate} Hz / {self.bits}-bit / {self.channels}ch",
            )

        elif ftype == FRAME_AUDIO:
            if self.wav:
                self.wav.writeframes(payload)
            self.audio_bytes += len(payload)
            self._emit("level", peak=self._peak(payload), nbytes=self.audio_bytes,
                       gaps=self.gaps)

        elif ftype == FRAME_STOP:
            dropped = int.from_bytes(payload[0:4], "little") if len(payload) >= 4 else 0
            path = self.wav_path
            self._close_wav()
            self._emit("stopped", path=path, dropped=dropped, gaps=self.gaps,
                       resyncs=self.resyncs, nbytes=self.audio_bytes)

    @staticmethod
    def _peak(payload):
        if len(payload) < 2:
            return 0.0
        a = array.array("h")
        a.frombytes(payload[: len(payload) - (len(payload) % 2)])
        if sys.byteorder != "little":
            a.byteswap()
        return max(abs(min(a)), abs(max(a))) / 32768.0 if a else 0.0

    def _open_wav(self):
        self._close_wav()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("mic_%Y%m%d_%H%M%S.wav")
        self.wav_path = OUT_DIR / name
        self.wav = wave.open(str(self.wav_path), "wb")
        self.wav.setnchannels(self.channels)
        self.wav.setsampwidth(self.bits // 8)
        self.wav.setframerate(self.rate)
        self.audio_bytes = 0
        self.gaps = 0
        self.resyncs = 0

    def _close_wav(self):
        if self.wav:
            try:
                self.wav.close()  # writes the RIFF header
            except Exception:  # noqa: BLE001
                pass
            self.wav = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("falldetect-gkl - Mic Recorder")
        self.resizable(False, False)

        self.evq = queue.Queue()
        self.reader = None
        self.recording = False
        self.t0 = None
        self.last_path = None
        self.level = 0.0

        pad = dict(padx=8, pady=4)

        # -- connection ----------------------------------------------------
        top = ttk.LabelFrame(self, text="Connection")
        top.grid(row=0, column=0, sticky="ew", **pad)
        self.port_cb = ttk.Combobox(top, width=34, state="readonly")
        self.port_cb.grid(row=0, column=0, padx=6, pady=6)
        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(row=0, column=1, padx=4)
        self.btn_conn = ttk.Button(top, text="Connect", command=self.toggle_conn)
        self.btn_conn.grid(row=0, column=2, padx=6)

        # -- transport -----------------------------------------------------
        mid = ttk.LabelFrame(self, text="Record")
        mid.grid(row=1, column=0, sticky="ew", **pad)
        self.btn_rec = ttk.Button(mid, text="●  Record", width=16,
                                  command=self.start_rec, state="disabled")
        self.btn_rec.grid(row=0, column=0, padx=6, pady=8)
        self.btn_stop = ttk.Button(mid, text="■  Stop", width=16,
                                   command=self.stop_rec, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=6, pady=8)

        ttk.Label(mid, text="Level").grid(row=1, column=0, sticky="w", padx=8)
        self.meter = ttk.Progressbar(mid, length=300, maximum=100)
        self.meter.grid(row=1, column=1, padx=8, pady=4, sticky="w")

        self.lbl_stats = ttk.Label(mid, text="idle", font=("Consolas", 9))
        self.lbl_stats.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 8))

        # -- playback ------------------------------------------------------
        bot = ttk.LabelFrame(self, text="Playback")
        bot.grid(row=2, column=0, sticky="ew", **pad)
        self.btn_play = ttk.Button(bot, text="▶  Play last", width=16,
                                   command=self.play_last, state="disabled")
        self.btn_play.grid(row=0, column=0, padx=6, pady=6)
        ttk.Button(bot, text="Open folder", width=16,
                   command=self.open_folder).grid(row=0, column=1, padx=6)

        # -- log -----------------------------------------------------------
        self.log = tk.Text(self, height=9, width=64, state="disabled",
                           font=("Consolas", 9))
        self.log.grid(row=3, column=0, sticky="ew", **pad)

        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self.pump)

    # -- helpers -----------------------------------------------------------
    def say(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        values = [f"{p.device} - {p.description}" for p in ports]
        self.port_cb["values"] = values
        if not values:
            return

        def rank(p):
            """Lower is better. Windows usually reports the board as a generic
            'USB Serial Device', so match on the USB VID and de-prioritise the
            Bluetooth virtual ports that would otherwise be picked first."""
            desc = (p.description or "").lower()
            hwid = (p.hwid or "").upper()
            if "VID:PID=2FE3:0100" in hwid or "mic recorder" in desc:
                return 0
            if "bluetooth" in desc:
                return 2
            return 1

        best = min(range(len(ports)), key=lambda i: rank(ports[i]))
        self.port_cb.current(best)

    def selected_port(self):
        raw = self.port_cb.get()
        return raw.split(" - ")[0] if raw else None

    # -- connection --------------------------------------------------------
    def toggle_conn(self):
        if self.reader:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        port = self.selected_port()
        if not port:
            messagebox.showwarning("No port", "Select a serial port first.")
            return
        self.reader = Reader(port, self.evq)
        self.reader.start()
        self.btn_conn.configure(text="Disconnect")
        self.btn_rec.configure(state="normal")

    def disconnect(self):
        if self.recording:
            self.stop_rec()
        if self.reader:
            self.reader.shutdown()
            self.reader = None
        self.btn_conn.configure(text="Connect")
        self.btn_rec.configure(state="disabled")
        self.btn_stop.configure(state="disabled")
        self.say("Disconnected")

    # -- transport ---------------------------------------------------------
    def start_rec(self):
        if not self.reader:
            return
        self.reader.send(b"S")
        self.recording = True
        self.t0 = time.time()
        self.btn_rec.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.say("Record requested")

    def stop_rec(self):
        if not self.reader:
            return
        self.reader.send(b"X")
        self.recording = False
        self.btn_rec.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.say("Stop requested")

    def play_last(self):
        if not self.last_path or not Path(self.last_path).exists():
            return
        if winsound:
            winsound.PlaySound(str(self.last_path),
                               winsound.SND_FILENAME | winsound.SND_ASYNC)
            self.say(f"Playing {Path(self.last_path).name}")
        else:
            os.startfile(str(self.last_path))  # noqa: S606 - user-initiated

    def open_folder(self):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(OUT_DIR))  # noqa: S606 - user-initiated

    # -- event pump --------------------------------------------------------
    def pump(self):
        try:
            while True:
                kind, kw = self.evq.get_nowait()
                self.on_event(kind, kw)
        except queue.Empty:
            pass

        # Decay the meter so it falls back smoothly instead of latching.
        self.level *= 0.75
        self.meter["value"] = min(100.0, self.level * 100.0)

        if self.recording and self.t0:
            secs = time.time() - self.t0
            nb = self.reader.audio_bytes if self.reader else 0
            self.lbl_stats.configure(
                text=f"{secs:6.1f} s   {nb/1024:8.1f} KiB   {nb//2:>9} samples"
            )

        self.after(50, self.pump)

    def on_event(self, kind, kw):
        if kind == "status":
            self.say(kw["msg"])
        elif kind == "error":
            self.say("ERROR: " + kw["msg"])
            messagebox.showerror("Serial", kw["msg"])
            self.disconnect()
        elif kind == "started":
            self.say(kw["msg"])
        elif kind == "level":
            self.level = max(self.level, kw["peak"])
        elif kind == "stopped":
            self.last_path = kw.get("path")
            nb = kw.get("nbytes", 0)
            secs = nb / 2 / 16000.0
            self.say(
                f"Saved {Path(self.last_path).name if self.last_path else '(none)'}"
                f"  {secs:.1f}s  gaps={kw.get('gaps')}"
                f"  dropped={kw.get('dropped')}  resyncs={kw.get('resyncs')}"
            )
            if kw.get("gaps") or kw.get("dropped"):
                self.say("WARNING: audio was lost - recording has gaps.")
            self.lbl_stats.configure(text=f"saved {secs:.1f} s")
            if self.last_path:
                self.btn_play.configure(state="normal")
        elif kind == "closed":
            self.say("Port closed")

    def on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
