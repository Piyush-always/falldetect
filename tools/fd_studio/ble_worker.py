"""
One asyncio event loop, on one thread, for the entire process lifetime.

WHY THIS EXISTS - the bug it prevents
-------------------------------------
Every BLE operation here used to call asyncio.run(), which creates a loop,
runs one operation, and destroys the loop. On Windows that also tears down and
re-creates the WinRT COM apartment each time, and it can orphan the
BluetoothLEAdvertisementWatcher that bleak's scanner allocates. After a few
cycles Windows starts refusing to start new watchers, and every later scan
fails with:

    BleakError: Failed to start scanner. Is Bluetooth turned on?

That message is a lie, and it cost a lot of time. Read bleak 3.0.2's
backends/winrt/scanner.py: a genuinely powered-off radio raises a DIFFERENT
exception (BleakBluetoothNotAvailableError, reason POWERED_OFF) around line
260. The "Is Bluetooth turned on?" string at line 310 is only reachable AFTER
bleak has already confirmed an adapter exists, supports the central role, and
has radio.state == RadioState.ON. It means the watcher went ABORTED - a wedged
Windows BLE stack - not a radio that is off.

Compounding it, bleak's assert_mta() (backends/winrt/util.py) caches its
verdict in a PROCESS-GLOBAL sticky flag:

    if hasattr(allow_sta, "_allowed"):
        return

so once that flag is set, the apartment check is skipped for every thread
created afterwards. Combined with per-operation loops, the failure is
sticky and asymmetric: it works a few times, then never again.

The fix is structural rather than defensive: keep exactly ONE loop alive for
the whole process and submit every BLE coroutine to it. Nothing is torn down,
so nothing leaks. This mirrors the BleWorker design in the DUSQ tool, which
does not contain a single COM/apartment workaround and does not need one.

USAGE
-----
    fut = BleWorker.instance().submit(some_coroutine())
    result = fut.result(timeout=30)      # from any non-loop thread

Never call asyncio.run() anywhere in this package again.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Coroutine


class BleWorker:
    """A singleton loop-on-a-thread. Started lazily, never stopped."""

    _instance: "BleWorker | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="ble-worker", daemon=True)
        self._thread.start()
        # Submitting before run_forever() is actually running would queue the
        # callback but never execute it, so callers would block on a future
        # that can never complete.
        self._ready.wait(timeout=5.0)

    @classmethod
    def instance(cls) -> "BleWorker":
        # Double-checked so two tabs constructing at once cannot race into two
        # loops, which would reintroduce exactly the problem this prevents.
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = BleWorker()
        return cls._instance

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        """Run `coro` on the shared loop. Returns a concurrent.futures.Future.

        Safe to call from the Qt GUI thread; the returned future must NOT be
        waited on from the GUI thread for anything slow, or the UI freezes.
        """
        return asyncio.run_coroutine_threadsafe(coro, self.loop)
