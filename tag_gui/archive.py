from __future__ import annotations

from pathlib import Path
from typing import override

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from .domain import ImageEntry
from .storage import archive_entries


class _ArchiveSignals(QObject):
    progress = Signal(int, int, str)
    completed = Signal(int)
    failed = Signal(str)


class _ArchiveWorker(QRunnable):
    def __init__(
        self, entries: list[ImageEntry], destination: Path
    ) -> None:
        super().__init__()
        self.entries = entries
        self.destination = destination
        self.signals = _ArchiveSignals()

    @override
    def run(self) -> None:
        try:
            result = archive_entries(
                self.entries,
                self.destination,
                self.signals.progress.emit,
            )
        except Exception as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.completed.emit(len(result.archived))


class ArchiveProgressDialog(QDialog):
    completed = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        entries: list[ImageEntry],
        destination: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Creating Archive")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(440)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        self.current_file_label = QLabel("Preparing archive...")
        self.current_file_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, len(entries) * 2)
        self.progress_bar.setValue(0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self.current_file_label)
        layout.addWidget(self.progress_bar)

        self._running = False
        self._worker = _ArchiveWorker(entries, destination)
        self._worker.signals.progress.connect(self._update_progress)
        self._worker.signals.completed.connect(self._complete)
        self._worker.signals.failed.connect(self._fail)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        QThreadPool.globalInstance().start(self._worker)

    def _update_progress(
        self, completed: int, total: int, current_file: str
    ) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(completed)
        self.current_file_label.setText(f"Archiving: {current_file}")

    def _complete(self, archived_count: int) -> None:
        self._running = False
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.accept()
        self.completed.emit(archived_count)

    def _fail(self, message: str) -> None:
        self._running = False
        self.reject()
        self.failed.emit(message)

    @override
    def reject(self) -> None:
        if self._running:
            return
        super().reject()

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._running:
            event.ignore()
            return
        super().closeEvent(event)
