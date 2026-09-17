# PDM Microphone — Recording Tool Plan

**Status:** v0.1 — implemented as `apps/mic_record` + `tools/mic_gui.py`.
**Date:** 2026-08-01

> **Scope note.** [PROJECT_OUTLINE.md](PROJECT_OUTLINE.md) places voice at Phase 7,
> deferred. This is being built early by request. It is a **standalone recording tool**,
> not wearable firmware — it shares no code with the fall detector and does not disturb
> the accel path. Treat it as a bench instrument for capturing audio, and as proof the
> PDM peripheral works.

---

## 1. Goal

Press Start in a desktop GUI, record continuous audio from the on-board PDM mic, press
Stop, get a `.wav` on disk, and play it back immediately to confirm it is real audio.

---

## 2. Hardware and SDK facts (Verified against NCS v3.1.1)

| Item | Value | Source |
|---|---|---|
| Microphone | MSM261D3526H1CPM, PDM | board DTS |
| PDM peripheral | `pdm0` @ 0x4001D000, `nordic,nrf-pdm` | `nrf52840.dtsi:404` |
| **PDM default state** | **`status = "disabled"`** | `nrf52840.dtsi:408` |
| Pinctrl + clock source | `pdm0_default`, `PCLK32M` | `xiao_ble_nrf52840_sense.dts:49-52` |
| Mic power rail | **P1.10**, active-high | `xiao_ble_nrf52840_sense.dts:18` |
| Rail default state | **off** — no `regulator-boot-on` | `xiao_ble_nrf52840_sense.dts:16-20` |
| Driver | `dmic_nrfx_pdm.c`, `CONFIG_AUDIO_DMIC` | `drivers/audio/` |
| Console routing | `zephyr,console = &board_cdc_acm_uart` | `cdc_acm_serial.dtsi:9` |

Two things had to be fixed to use the mic at all, and Zephyr's own sample for **this exact
board** (`samples/drivers/audio/dmic/boards/xiao_ble_nrf52840_sense.overlay`) does both:

```dts
/ {
	msm261d3526hicpm-c-en {
		regulator-boot-on;      /* rail is off at boot; turn it on */
	};
};

dmic_dev: &pdm0 {
	status = "okay";            /* PDM is disabled by default */
};
```

Note the contrast with the IMU rail, which *does* carry `regulator-boot-on`. That
asymmetry is deliberate on Seeed's part and easy to trip over.

> **Power caveat:** `regulator-boot-on` leaves the mic powered permanently. Correct for a
> bench recorder; **wrong for the wearable**, where the mic must be off until needed
> (PROJECT_OUTLINE §7.5). When voice work becomes real, gate this rail explicitly.

---

## 3. Audio format and the throughput question

| Parameter | Value |
|---|---|
| Sample rate | 16 kHz |
| Width | 16-bit signed, little-endian |
| Channels | 1 (mono, `PDM_CHAN_LEFT`) |
| Bitrate | **32 000 B/s** |
| Block | 20 ms = 320 samples = 640 bytes |
| Slab | 8 blocks = 160 ms of buffering |

16 kHz mono is the natural fit: it is what the PDM decimation lands on cleanly, and it
is the standard rate for speech and keyword spotting later.

**32 KB/s over USB CDC is comfortable** — full-speed USB is 12 Mbit/s and CDC ACM
realistically sustains hundreds of KB/s. The bottleneck is not bandwidth, it is *stalls*:
if the host stops draining for 200 ms, anything not buffered is lost.

Which is why audio must go out as **binary, not text**. Hex or CSV would roughly triple
the rate and burn CPU formatting every sample.

---

## 4. The console collision, and the framing that solves it

The board routes the Zephyr console to the same USB CDC port we want for audio. Any
`printk` mid-stream would inject ASCII into the PCM and produce a burst of noise in the
recording.

Rather than fight it with a second CDC instance, **everything on the wire is a frame** and
logging to that port is disabled (`CONFIG_LOG=n`, no `printk`). Status messages travel as
frames too, so we keep debuggability without corrupting audio.

```
 offset  size  field
   0      4    magic  'F','D','A','1'
   4      1    type
   5      2    seq     (LE, wraps at 65536)
   7      2    len     (LE, payload bytes)
   9    len    payload
```

| Type | Name | Payload |
|---|---|---|
| 0x01 | AUDIO | PCM int16 LE |
| 0x02 | TEXT | ASCII status |
| 0x03 | START | `rate:u32, bits:u16, channels:u16` |
| 0x04 | STOP | `dropped:u32` |

Host → device commands are single bytes: `S` start, `X` stop, `P` ping.

**`seq` exists to make dropped audio visible.** A gap means a real hole in the recording;
without the counter that shows up only as an unexplained click, and you would not know
whether the mic, the link, or the host was at fault. The device also reports its own
`dropped` count in the STOP frame, so host-side loss and device-side overflow can be told
apart.

---

## 5. Firmware structure (`apps/mic_record`)

Execution contexts are explicit, per the concurrency rules:

```
 DMIC driver ──(slab blocks)──► main thread          : dmic_read(), 20 ms blocks
                                     │
                                     │ copy + frame
                                     ▼
                                 TX ring buffer      : 8 KB, SPSC
                                     │
                                     ▼
                                 UART TX ISR         : uart_fifo_fill(), short, non-blocking
```

- **Main thread** owns the DMIC, builds frames, and pushes into the ring buffer. It frees
  each slab block immediately after copying, so the driver never starves.
- **UART ISR** only moves bytes from the ring buffer into the CDC FIFO, and takes inbound
  command bytes. No allocation, no blocking, no logging — per the ISR rules.
- The ring buffer is **single-producer/single-consumer** (thread produces, ISR consumes),
  which Zephyr's `ring_buf` supports without locking. Space is checked before a frame is
  written so a frame is never partially emitted.
- If the ring buffer is full the whole frame is dropped and counted. Deliberate: a torn
  frame would desynchronise the host, and a counted drop is recoverable information.

The device waits for **DTR** before sending anything, so opening the port mid-boot does
not lose the START frame.

---

## 6. Host tool (`tools/mic_gui.py`)

tkinter GUI. Dependencies: **`pyserial` only** — `tkinter`, `wave`, `winsound` and
`threading` are all standard library, and `winsound` gives playback on Windows with no
extra install.

- Port auto-detect with a refresh button
- **Start / Stop**, elapsed time, bytes captured, dropped-frame count
- **Live level meter** — the fastest way to tell a working mic from a silent one, before
  you have a file to open
- Writes a timestamped `.wav` via the `wave` module
- **Play** the last recording; **Open folder**

Serial reads happen on a background thread and reach the GUI through a queue — tkinter is
not thread-safe and must only be touched from the main loop.

---

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Host stall drops audio | Gaps/clicks | 160 ms slab + 8 KB ring buffer; `seq` makes losses visible |
| `printk` corrupting the stream | Noise burst in recording | `CONFIG_LOG=n`, status only via TEXT frames |
| Mic rail left on | Battery — irrelevant here, **not** for wearable | Flagged in §2; gate it in Phase 7 |
| Wrong PDM clock / rate | Distorted or silent audio | Clock limits copied from Zephyr's own sample (1.0–3.5 MHz, 40–60% DC) |
| Frame desync | Garbage audio | 4-byte magic + length; host resynchronises |

---

## 8. Verify

1. Board enumerates a COM port; GUI connects and shows a START frame at 16 kHz mono.
2. Level meter tracks speech and drops near silence.
3. 30-second recording: **zero `seq` gaps**, device-reported `dropped` = 0.
4. Playback is intelligible speech at correct pitch — wrong pitch means a sample-rate
   mismatch between device and WAV header.
