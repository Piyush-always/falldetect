"""
BLE transport: the device's telemetry stream over Nordic UART Service (NUS).

This is the link the product actually uses. apps/datalog notifies the exact
same "$D,/$P,/$I," lines over NUS that it writes to USB CDC, so everything
downstream of feed_bytes() — parsing, gap detection, recording, the engine —
is shared with the serial path and needs no BLE-specific handling.

EVERYTHING HERE RUNS ON THE SHARED LOOP
---------------------------------------
No asyncio.run(), no per-operation event loop, no per-operation thread. See
ble_worker.py for the failure this avoids: repeatedly creating and destroying
loops tears down the WinRT COM apartment and orphans advertisement watchers
until Windows refuses to start new ones, which surfaces as a permanent and
very misleading "Failed to start scanner. Is Bluetooth turned on?".

Scanning uses BleakScanner(detection_callback=...) with explicit
start()/stop() in a try/finally rather than BleakScanner.discover(), so the
watcher is released even when the scan raises. A leaked watcher is the thing
that wedges the stack.

THROUGHPUT CAVEAT (unverified)
------------------------------
At 208 Hz x ~48 byte lines this is ~10 KB/s, which needs a short connection
interval and a large MTU to sustain. Whether a given Windows BLE adapter
actually negotiates that has NOT been measured on hardware yet. The seq
counter makes any shortfall visible rather than silent: watch the gap count
in the status strip, and treat a climbing gap count as "this adapter cannot
keep up", not as a firmware fault.
"""

from __future__ import annotations

import asyncio

from bleak import BleakClient, BleakScanner

from .ble_worker import BleWorker
from .engine import Engine
from .link import LinkBase

# Nordic UART Service. Note TX/RX are named from the DEVICE's point of view:
# the device notifies on TX, so that is the one this host subscribes to.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

DEVICE_NAME_PREFIX = "falldetect"


def explain(exc: Exception) -> str:
    """Turn a BLE exception into something worth showing a person.

    The two Windows failures look alike and mean opposite things, so they are
    separated deliberately. Verified against bleak 3.0.2's
    backends/winrt/scanner.py:

      - A genuinely powered-off radio raises BleakBluetoothNotAvailableError
        (reason POWERED_OFF) at ~line 260, BEFORE the watcher is ever started.
        That one really does mean "switch Bluetooth on".

      - "Failed to start scanner. Is Bluetooth turned on?" at line 310 is only
        reachable AFTER bleak has confirmed an adapter exists, supports the
        central role, and radio.state == RadioState.ON. It means the watcher
        went ABORTED. The radio is ON and the message is wrong. Telling the
        user to switch Bluetooth on here sends them to a setting that is
        already correct - which is exactly what happened during development.
    """
    name = type(exc).__name__
    text = str(exc)

    if "BluetoothNotAvailable" in name or "POWERED_OFF" in text:
        return ("Bluetooth is switched off. Turn it on in Windows Settings > "
                "Bluetooth & devices, then press Scan again.")

    if "Failed to start scanner" in text:
        return ("Windows refused to start a Bluetooth scan. The radio is on, "
                "so this is usually another app holding the Bluetooth scanner "
                "(nRF Connect for Desktop is the common culprit) or a wedged "
                "Bluetooth stack. Close other Bluetooth tools; if that does "
                "not help, toggle Bluetooth off and on, or restart the PC.")

    if "WinRT" in text or "RadioState" in text:
        return (f"Bluetooth is unavailable ({text}). Toggling Bluetooth off "
                "and on in Windows Settings usually clears this.")

    return text


async def _scan(timeout: float) -> list[tuple[str, str, int]]:
    """Devices as (name, address, rssi), falldetect boards first.

    try/finally around stop() matters: if this coroutine is cancelled or the
    sleep raises, an un-stopped watcher stays registered with Windows and
    contributes to wedging the stack for every later scan.
    """
    found: dict[str, tuple[str, int]] = {}

    def on_detect(device, adv) -> None:
        name = device.name or ""
        if name:
            found[device.address] = (name, adv.rssi)

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        try:
            await scanner.stop()
        except Exception:  # noqa: BLE001
            pass

    out = [(n, addr, rssi) for addr, (n, rssi) in found.items()]
    out.sort(key=lambda t: (not t[0].startswith(DEVICE_NAME_PREFIX), -t[2]))
    return out


def scan_blocking(timeout: float = 6.0) -> list[tuple[str, str, int]]:
    """Scan on the shared loop and wait for the result.

    Blocks the CALLING thread, so never call this from the Qt GUI thread -
    app.py runs it on a worker thread and reports back via a Qt signal.
    """
    fut = BleWorker.instance().submit(_scan(timeout))
    return fut.result(timeout=timeout + 20.0)


class BleDeviceLink(LinkBase):
    """NUS notification transport. `target` is a BLE name or address."""

    def __init__(self, target: str, engine: Engine):
        super().__init__(engine)
        self.target = target
        self._future = None

    def start(self) -> None:
        self._future = BleWorker.instance().submit(self._run())

    def shutdown(self) -> None:
        # _run() polls this and unwinds cleanly, unsubscribing and closing the
        # client. Cancelling the future instead would drop the connection
        # without stop_notify(), which is the kind of thing that leaves the
        # stack unhappy.
        super().shutdown()

    async def _run(self) -> None:
        try:
            await self._connect_and_stream()
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", explain(exc)))
        finally:
            self.stop_recording()
            self.events.put(("log", "BLE disconnected"))

    async def _connect_and_stream(self) -> None:
        self.events.put(("log", f"looking for {self.target}..."))

        # Resolve the target to a concrete device via our own scan rather than
        # find_device_by_name(), so the watcher lifecycle stays in one place.
        device = None
        for name, address, _rssi in await _scan(6.0):
            if name == self.target or address == self.target:
                device = address
                break
        if device is None:
            self.events.put(("error",
                             f"'{self.target}' not found. Is it powered and in range?"))
            return

        def on_notify(_characteristic, data: bytearray) -> None:
            self.feed_bytes(bytes(data))

        disconnected = asyncio.Event()

        def on_disconnect(_client) -> None:
            disconnected.set()

        async with BleakClient(device, disconnected_callback=on_disconnect) as client:
            try:
                await client.start_notify(NUS_TX_UUID, on_notify)
            except Exception as exc:  # noqa: BLE001
                self.events.put((
                    "error",
                    "Connected, but this device has no telemetry service "
                    f"({exc}). Update it to a datalog build that includes it."))
                return

            self.events.put(("log", f"connected to {self.target}"))

            while not self._stop.is_set() and not disconnected.is_set():
                await asyncio.sleep(0.2)

            if disconnected.is_set():
                self.events.put(("error", "Device disconnected."))
            else:
                try:
                    await client.stop_notify(NUS_TX_UUID)
                except Exception:  # noqa: BLE001
                    pass
