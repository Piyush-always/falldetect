"""
The Log tab: what the tool itself is doing.

Separate from the Debug tab's event pane on purpose. That pane answers "what
did the DETECTOR see" - falls, candidates, why one was rejected. This answers
"what is the TOOL doing" - scanning, connecting, dropping the link, pushing
firmware, failing to.

Those are different questions asked at different moments, and during this
project the second one was repeatedly asked while the first tab was the only
place with the answer. When a connection misbehaves, the last place you want
the explanation is inside a tab about detection.

Entries are capped rather than unbounded: at 208 Hz a chatty session could
otherwise grow the document until the UI slows down, and the oldest lines are
the least useful ones to keep.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from . import tokens as T

#: Kept lines. Enough to cover a whole session's connect/scan/update activity
#: without letting the document grow without bound.
MAX_LINES = 2000


class LogTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(T.S6, T.S4, T.S6, T.S4)
        lay.setSpacing(T.S3)

        head = QHBoxLayout()
        title = QLabel("Activity log")
        title.setObjectName("Heading")
        head.addWidget(title)
        head.addStretch(1)

        self.btn_copy = QPushButton("Copy all")
        self.btn_copy.clicked.connect(self._copy)
        head.addWidget(self.btn_copy)

        self.btn_save = QPushButton("Save to file")
        self.btn_save.clicked.connect(self._save)
        head.addWidget(self.btn_save)

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self._clear)
        head.addWidget(self.btn_clear)
        lay.addLayout(head)

        blurb = QLabel(
            "Everything the tool does: scanning, connecting, dropped links, "
            "firmware updates, and errors. Detection events stay on the Debug "
            "tab — this is about the tool, not the wearer.")
        blurb.setObjectName("Caption")
        blurb.setWordWrap(True)
        lay.addWidget(blurb)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        # Read-only, but explicitly selectable: "Copy all" covers the whole
        # log, and this covers selecting the three lines that actually matter
        # and pasting them somewhere. Read-only alone leaves selection on in
        # Qt, but stating it means a later style change cannot quietly take
        # it away.
        self.view.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setPlaceholderText(
            "Nothing yet.\n\n"
            "Press Scan to look for the device. Whatever happens — including "
            "what goes wrong — appears here with a timestamp.")
        lay.addWidget(self.view, 1)

        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("Caption")
        lay.addWidget(self.lbl_count)

        self._n = 0

    def append(self, msg: str) -> None:
        """Add one timestamped line. Safe to call from the GUI thread only."""
        self._n += 1
        self.view.appendPlainText(f"{time.strftime('%H:%M:%S')}  {msg}")
        # Follow the tail, which is what you want while watching something
        # happen. Scrolling up to read history pauses this naturally because
        # appendPlainText only autoscrolls when already at the bottom.
        self.lbl_count.setText(
            f"{self._n} entries"
            + (f" (showing last {MAX_LINES})" if self._n > MAX_LINES else ""))

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.view.toPlainText())
        self.append("log copied to clipboard")

    def _save(self) -> None:
        name = time.strftime("fd_studio_%Y%m%d_%H%M%S.log")
        path, _ = QFileDialog.getSaveFileName(self, "Save log", name,
                                              "Log files (*.log);;All files (*)")
        if not path:
            return
        try:
            Path(path).write_text(self.view.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self.append(f"could not save log: {exc}")
            return
        self.append(f"log saved to {path}")

    def _clear(self) -> None:
        self.view.clear()
        self._n = 0
        self.lbl_count.setText("")
