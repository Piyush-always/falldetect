"""
The OTA tab: update the device's firmware wirelessly.

Runs the same upload/test-boot/reset sequence as tools/scripts/ota_flash.py by calling
the same module (fd_studio.ota), so the GUI and the CLI cannot drift.

Threading: smpclient is asyncio, this app is threads + a Qt timer. The update
runs on a worker thread with its own event loop, and progress arrives back on
the GUI thread through Qt signals rather than by touching widgets directly -
calling into a widget from a worker thread is a crash, not a warning.

The device cannot stream telemetry to us while the update runs (it reboots at
the end), so the tab disconnects the live link first and says so, rather than
letting the Debug tab quietly show a frozen trace.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QProgressBar, QPushButton, QVBoxLayout,
                               QWidget)

from . import tokens as T

# Frozen into an exe (tools/build_exe.ps1): data/ and firmware/ live beside it.
ROOT = (Path(sys.executable).parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent.parent)
from .ble_worker import BleWorker
from .ota import (APP_BLE_NAMES, FIRMWARE_DIR, OtaError, archive_image,
                  find_signed_image, image_version, push_update,
                  read_device_version)


class _Worker(QObject):
    progress = Signal(int, int)
    status = Signal(str)
    finished = Signal(bool, str)
    device_version = Signal(str)


class OtaTab(QWidget):
    def __init__(self, get_target, on_before_update, on_version=None, parent=None):
        super().__init__(parent)
        self._get_target = get_target
        self._on_before_update = on_before_update
        # Lets the User tab show the running firmware version too.
        self._on_version = on_version
        self._busy = False

        self._worker = _Worker()
        self._worker.progress.connect(self._on_progress)
        self._worker.status.connect(self._on_status)
        self._worker.finished.connect(self._on_finished)
        self._worker.device_version.connect(self._on_device_version)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(T.S8, T.S6, T.S8, T.S6)
        outer.setSpacing(T.S4)

        centre = QWidget()
        centre.setMaximumWidth(560)
        lay = QVBoxLayout(centre)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(T.S4)

        title = QLabel("Update firmware over Bluetooth")
        title.setObjectName("Heading")
        lay.addWidget(title)

        blurb = QLabel(
            "Sends a new build to the device wirelessly. The device restarts "
            "into it and keeps it only if it starts up correctly — otherwise "
            "it goes back to the previous version on its own.")
        blurb.setObjectName("Caption")
        blurb.setWordWrap(True)
        lay.addWidget(blurb)

        form = QFormLayout()
        form.setSpacing(T.S2)
        self.app_cb = QComboBox()
        # Only firmware that is actually shipped to a worn device. blink is a
        # bring-up toy; pushing it over the air would strand the device with no
        # telemetry and no detector.
        for app in ("datalog", "falldetect"):
            self.app_cb.addItem(app, app)
        self.app_cb.currentIndexChanged.connect(self._refresh_versions)
        form.addRow(self._cap("Firmware"), self.app_cb)
        lay.addLayout(form)

        # Versions are the whole point of this panel: which build is on the
        # device, and which one am I about to send. Filenames and byte counts
        # answer neither question.
        vers = QHBoxLayout()
        vers.setSpacing(T.S4)

        col_dev = QVBoxLayout()
        col_dev.setSpacing(2)
        col_dev.addWidget(self._cap("On the device"))
        self.lbl_dev_ver = QLabel("—")
        self.lbl_dev_ver.setObjectName("Heading")
        col_dev.addWidget(self.lbl_dev_ver)
        vers.addLayout(col_dev)

        col_new = QVBoxLayout()
        col_new.setSpacing(2)
        col_new.addWidget(self._cap("This build"))
        self.lbl_new_ver = QLabel("—")
        self.lbl_new_ver.setObjectName("Heading")
        col_new.addWidget(self.lbl_new_ver)
        vers.addLayout(col_new)

        vers.addStretch(1)
        lay.addLayout(vers)

        self.lbl_compare = QLabel("")
        self.lbl_compare.setObjectName("Caption")
        self.lbl_compare.setWordWrap(True)
        lay.addWidget(self.lbl_compare)

        # Send a specific file instead of the current build. Needed to roll
        # back: every pushed image is kept in firmware/, so going back to a
        # known-good version is picking it here, not rebuilding an old commit.
        pick = QHBoxLayout()
        self.btn_pick = QPushButton("Choose file...")
        self.btn_pick.clicked.connect(self._choose_file)
        pick.addWidget(self.btn_pick)
        self.btn_clear = QPushButton("Use current build")
        self.btn_clear.clicked.connect(self._clear_file)
        self.btn_clear.setEnabled(False)
        pick.addWidget(self.btn_clear)
        pick.addStretch(1)
        lay.addLayout(pick)

        self.lbl_file = QLabel("")
        self.lbl_file.setObjectName("MonoDim")
        self.lbl_file.setWordWrap(True)
        lay.addWidget(self.lbl_file)

        row = QHBoxLayout()
        self.btn_push = QPushButton("Send update")
        self.btn_push.setObjectName("Primary")
        self.btn_push.setMinimumHeight(40)
        self.btn_push.clicked.connect(self._start)
        row.addWidget(self.btn_push)
        lay.addLayout(row)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setMinimumHeight(6)
        self.bar.setMaximumHeight(6)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setObjectName("Caption")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        lay.addStretch(1)

        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addWidget(centre)
        wrap.addStretch(1)
        outer.addLayout(wrap, 1)

        self._dev_ver = None
        self._new_ver = None
        self._chosen = None
        self._refresh_versions()

    def _cap(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("Caption")
        return lab


    def _choose_file(self) -> None:
        start = FIRMWARE_DIR if FIRMWARE_DIR.exists() else ROOT
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose firmware image", str(start),
            "Firmware release (*.zip *.bin);;All files (*)")
        if not path:
            return
        chosen = Path(path)
        try:
            ver = image_version(chosen)
        except Exception as exc:  # noqa: BLE001
            # Not an MCUboot image: say so now rather than after a long upload
            # that the device would reject anyway.
            self.lbl_file.setText(f"Not a signed MCUboot image: {exc}")
            return
        self._chosen = chosen
        self._new_ver = ver
        self.lbl_new_ver.setText(ver)
        self.lbl_file.setText(chosen.name)
        self.btn_clear.setEnabled(True)
        self.app_cb.setEnabled(False)
        self._compare()

    def _clear_file(self) -> None:
        self._chosen = None
        self.lbl_file.setText("")
        self.btn_clear.setEnabled(False)
        self.app_cb.setEnabled(True)
        self._refresh_versions()

    def _refresh_versions(self) -> None:
        """Local build version now; device version needs a BLE round trip."""
        if getattr(self, "_chosen", None) is not None:
            return
        app = self.app_cb.currentData()
        try:
            self._new_ver = image_version(find_signed_image(app))
            self.lbl_new_ver.setText(self._new_ver)
            self.lbl_compare.setText("")
        except OtaError as exc:
            self._new_ver = None
            self.lbl_new_ver.setText("—")
            self.lbl_compare.setText(str(exc))
        self._compare()

    def refresh_device_version(self) -> None:
        """Ask the device what it is running. Safe to call when disconnected."""
        target = self._get_target()
        if not target or self._busy:
            return
        self.lbl_dev_ver.setText("…")

        def work() -> None:
            try:
                fut = BleWorker.instance().submit(read_device_version(target))
                ver = fut.result(timeout=40)
            except Exception:  # noqa: BLE001
                ver = None
            self._worker.device_version.emit(ver or "unknown")

        threading.Thread(target=work, daemon=True).start()

    def _on_device_version(self, ver: str) -> None:
        self._dev_ver = None if ver == "unknown" else ver
        self.lbl_dev_ver.setText(ver)
        if self._on_version is not None and self._dev_ver:
            self._on_version(self._dev_ver)
        self._compare()

    def _compare(self) -> None:
        dev, new = getattr(self, "_dev_ver", None), getattr(self, "_new_ver", None)
        if not dev or not new:
            return
        if dev == new:
            self.lbl_compare.setText(
                "The device is already running this version. Bump the app's "
                "VERSION file before sending, or the update is indistinguishable "
                "from what is already installed.")
        else:
            self.lbl_compare.setText(f"Will replace {dev} with {new}.")

    # ── run ──────────────────────────────────────────────────────────────────
    def _start(self) -> None:
        if self._busy:
            return
        app = self.app_cb.currentData()
        chosen = getattr(self, "_chosen", None)
        if chosen is not None:
            image_path = chosen
        else:
            try:
                image_path = find_signed_image(app)
            except OtaError as exc:
                self.lbl_status.setText(str(exc))
                return
            # Keep a copy so this build stays re-pushable later.
            try:
                archive_image(app, image_path)
            except OSError:
                pass

        target = self._get_target() or APP_BLE_NAMES.get(app)
        if not target:
            self.lbl_status.setText("No device selected. Pick one at the top first.")
            return

        # The device reboots at the end of this, so a live telemetry link
        # would go stale mid-update and look like a fault.
        self._on_before_update()

        self._busy = True
        self.btn_push.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.lbl_status.setText(f"Starting update of {target}...")

        threading.Thread(target=self._run, args=(target, image_path),
                         daemon=True).start()

    def _run(self, target: str, image_path: Path) -> None:
        try:
            # Submitted to the SHARED loop, not asyncio.run() on this thread.
            # A private loop per update is what wedged the Windows BLE stack;
            # see ble_worker.py.
            fut = BleWorker.instance().submit(push_update(
                target, image_path,
                progress=lambda sent, total: self._worker.progress.emit(sent, total),
                status=lambda msg: self._worker.status.emit(msg),
            ))
            fut.result(timeout=600)
        except OtaError as exc:
            self._worker.finished.emit(False, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._worker.finished.emit(False, f"Update failed: {exc}")
            return
        self._worker.finished.emit(True, "Update complete. The device is "
                                         "restarting into the new firmware.")

    def _on_progress(self, sent: int, total: int) -> None:
        self.bar.setValue((sent * 100) // total if total else 0)

    def _on_status(self, msg: str) -> None:
        self.lbl_status.setText(msg)

    def _on_finished(self, ok: bool, msg: str) -> None:
        self._busy = False
        self.btn_push.setEnabled(True)
        self.bar.setVisible(False)
        self.lbl_status.setText(msg if ok else f"⚠  {msg}")
        if ok:
            # The device reboots into the new image; re-read so the
            # panel reflects reality rather than the pre-update value.
            self._dev_ver = None
            self.lbl_dev_ver.setText("—")
