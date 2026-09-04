from __future__ import annotations

from pathlib import Path
from typing import override

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRunnable,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QImage,
    QImageReader,
    QMouseEvent,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
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
    fit_to_window_changed = Signal(bool)

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
        self._drag_start: QPoint | None = None
        self._drag_scroll_start: tuple[int, int] | None = None
        self._label.installEventFilter(self)
        self.viewport().installEventFilter(self)

    @property
    def fit_to_window(self) -> bool:
        return self._fit_to_window

    def clear_image(self, message: str = "No image selected") -> None:
        self._end_drag()
        self._pixmap = None
        self._label.clear()
        self._label.setText(message)
        self._label.resize(self.viewport().size())

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self._label.setText("")
        self._update_pixmap()

    def set_fit_to_window(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._fit_to_window != enabled:
            self._fit_to_window = enabled
            self.fit_to_window_changed.emit(enabled)
        if enabled:
            self._zoom = 1.0
        self._update_pixmap()

    def actual_size(self) -> None:
        self._set_manual_zoom()
        self._zoom = 1.0
        self._update_pixmap()

    def zoom_in(self) -> None:
        self._set_manual_zoom()
        self._zoom = min(8.0, self._zoom * 1.25)
        self._update_pixmap()

    def zoom_out(self) -> None:
        self._set_manual_zoom()
        self._zoom = max(0.1, self._zoom / 1.25)
        self._update_pixmap()

    def _set_manual_zoom(self) -> None:
        if self._fit_to_window:
            self._fit_to_window = False
            self.fit_to_window_changed.emit(False)

    def _zoom_at(self, position: QPoint, factor: float) -> None:
        if self._pixmap is None:
            return
        old_size = self._label.size()
        if old_size.width() <= 0 or old_size.height() <= 0:
            return

        label_position = self._label.mapFrom(self.viewport(), position)
        image_x = max(0.0, min(1.0, label_position.x() / old_size.width()))
        image_y = max(0.0, min(1.0, label_position.y() / old_size.height()))
        old_zoom = self._zoom
        self._set_manual_zoom()
        self._zoom = max(0.1, min(8.0, old_zoom * factor))
        if self._zoom == old_zoom:
            return
        self._update_pixmap()

        new_position = QPoint(
            round(image_x * self._label.width()),
            round(image_y * self._label.height()),
        )
        old_origin = self._label.mapTo(self.viewport(), QPoint(0, 0))
        desired_origin = position - new_position
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value()
            + old_origin.x()
            - desired_origin.x()
        )
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().value()
            + old_origin.y()
            - desired_origin.y()
        )

    def _handle_wheel(self, event: QWheelEvent, position: QPoint) -> bool:
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta == 0:
            return True
        self._zoom_at(position, 1.25 if delta > 0 else 0.8)
        event.accept()
        return True

    def _event_position(self, event: QMouseEvent) -> QPoint:
        position = event.globalPosition().toPoint()
        return self.viewport().mapFromGlobal(position)

    def _end_drag(self) -> None:
        self._drag_start = None
        self._drag_scroll_start = None
        self.viewport().unsetCursor()

    @override
    def wheelEvent(self, event: QWheelEvent) -> None:
        position = self.viewport().mapFrom(
            self, event.position().toPoint()
        )
        if self._handle_wheel(event, position):
            return
        super().wheelEvent(event)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched not in (self._label, self.viewport()):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.Wheel:
            wheel_event = event
            if isinstance(wheel_event, QWheelEvent):
                if watched is self._label:
                    position = self._label.mapTo(
                        self.viewport(), wheel_event.position().toPoint()
                    )
                else:
                    position = wheel_event.position().toPoint()
                return self._handle_wheel(wheel_event, position)
        if event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            if (
                isinstance(mouse_event, QMouseEvent)
                and mouse_event.button() == Qt.MouseButton.LeftButton
                and self._pixmap is not None
            ):
                self._drag_start = self._event_position(mouse_event)
                self._drag_scroll_start = (
                    self.horizontalScrollBar().value(),
                    self.verticalScrollBar().value(),
                )
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
        elif event.type() == QEvent.Type.MouseMove:
            mouse_event = event
            if (
                isinstance(mouse_event, QMouseEvent)
                and self._drag_start is not None
                and self._drag_scroll_start is not None
            ):
                position = self._event_position(mouse_event)
                delta = position - self._drag_start
                self.horizontalScrollBar().setValue(
                    self._drag_scroll_start[0] - delta.x()
                )
                self.verticalScrollBar().setValue(
                    self._drag_scroll_start[1] - delta.y()
                )
                return True
        elif event.type() == QEvent.Type.MouseButtonRelease:
            mouse_event = event
            if (
                isinstance(mouse_event, QMouseEvent)
                and mouse_event.button() == Qt.MouseButton.LeftButton
                and self._drag_start is not None
            ):
                self._end_drag()
                return True
        return super().eventFilter(watched, event)

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
