from __future__ import annotations

from pathlib import Path
from typing import override

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap, QResizeEvent
from PySide6.QtWidgets import QLabel, QScrollArea


class _PreviewSignals(QObject):
    finished = Signal(int, QImage, str)


class _PreviewWorker(QRunnable):
    def __init__(self, generation: int, path: Path) -> None:
        super().__init__()
        self.generation = generation
        self.path = path
        self.signals = _PreviewSignals()

    @override
    def run(self) -> None:
        reader = QImageReader(str(self.path))
        reader.setAutoTransform(True)
        image = reader.read()
        error = "" if not image.isNull() else reader.errorString()
        self.signals.finished.emit(self.generation, image, error)


class PreviewLoader(QObject):
    loaded = Signal(QImage, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._generation = 0
        self._pool = QThreadPool.globalInstance()

    def clear(self) -> None:
        self._generation += 1

    def wait_for_done(self) -> bool:
        return self._pool.waitForDone()

    def load(self, path: Path) -> None:
        self._generation += 1
        worker = _PreviewWorker(self._generation, path)
        worker.signals.finished.connect(self._on_finished)
        self._pool.start(worker)

    def _on_finished(self, generation: int, image: QImage, error: str) -> None:
        if generation == self._generation:
            self.loaded.emit(image, error)


class ImageView(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrameShape(QScrollArea.Shape.NoFrame)

        self._label = QLabel("Open a folder to begin")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumSize(240, 180)
        self._label.setStyleSheet("QLabel { color: #667085; background: #f2f4f7; }")
        self.setWidget(self._label)

        self._pixmap: QPixmap | None = None
        self._fit_to_window = True
        self._zoom = 1.0

    @property
    def fit_to_window(self) -> bool:
        return self._fit_to_window

    def clear_image(self, message: str = "No image selected") -> None:
        self._pixmap = None
        self._label.clear()
        self._label.setText(message)
        self._label.resize(self.viewport().size())

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self._label.setText("")
        self._update_pixmap()

    def set_fit_to_window(self, enabled: bool) -> None:
        self._fit_to_window = enabled
        if enabled:
            self._zoom = 1.0
        self._update_pixmap()

    def actual_size(self) -> None:
        self._fit_to_window = False
        self._zoom = 1.0
        self._update_pixmap()

    def zoom_in(self) -> None:
        self._fit_to_window = False
        self._zoom = min(8.0, self._zoom * 1.25)
        self._update_pixmap()

    def zoom_out(self) -> None:
        self._fit_to_window = False
        self._zoom = max(0.1, self._zoom / 1.25)
        self._update_pixmap()

    @override
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._fit_to_window:
            self._update_pixmap()
        elif self._pixmap is None:
            self._label.resize(self.viewport().size())

    def _update_pixmap(self) -> None:
        if self._pixmap is None:
            return
        if self._fit_to_window:
            target = self.viewport().size()
            pixmap = self._pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            size = self._pixmap.size() * self._zoom
            pixmap = self._pixmap.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._label.setPixmap(pixmap)
        self._label.resize(pixmap.size())
