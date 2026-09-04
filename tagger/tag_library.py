from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import override
from urllib.request import ProxyHandler, Request, build_opener

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPoint,
    QStringListModel,
    QThread,
    Signal,
    Qt,
)
from PySide6.QtGui import QCloseEvent, QKeyEvent, QMouseEvent, QTextCursor
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .domain import ImageEntry
from .paths import PROJECT_ROOT, TAG_LIBRARY_PATH


DEFAULT_TAG_LIBRARY_PATH = TAG_LIBRARY_PATH
DATASET_ID = "qdlabs/danbooru-tags"
DATASET_TAGS_URL = (
    f"https://huggingface.co/datasets/{DATASET_ID}/resolve/main/tags.jsonl"
)


@dataclass(frozen=True)
class TagLibraryFileInfo:
    tag_count: int
    file_size: int
    modified_at: datetime


def get_tag_library_file_info(path: Path) -> TagLibraryFileInfo:
    stat = path.stat()
    return TagLibraryFileInfo(
        tag_count=_csv_tag_count(path),
        file_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime).astimezone(),
    )


def _search_key(tag: str) -> str:
    return tag.strip().casefold().replace(" ", "_")


def transform_tag_name(
    name: str,
    *,
    underscores_to_spaces: bool = False,
    escape_parentheses: bool = False,
) -> str:
    if underscores_to_spaces:
        name = name.replace("_", " ")
    if escape_parentheses:
        name = name.replace("(", r"\(").replace(")", r"\)")
    return name


def pending_tag(text: str) -> str:
    return text.rsplit(",", 1)[-1].strip()


def replace_pending_tag(text: str, tag: str) -> str:
    head, separator, _pending = text.rpartition(",")
    return f"{head}, {tag}" if separator else tag


class TagLibrary(QObject):
    changed = Signal()

    def __init__(
        self,
        csv_path: Path = DEFAULT_TAG_LIBRARY_PATH,
        parent: QObject | None = None,
        *,
        underscores_to_spaces: bool = False,
        escape_parentheses: bool = False,
    ) -> None:
        super().__init__(parent)
        self.csv_path = csv_path
        self.underscores_to_spaces = underscores_to_spaces
        self.escape_parentheses = escape_parentheses
        self._danbooru: dict[str, tuple[str, int]] = {}
        self._folder: dict[str, tuple[str, int]] = {}
        self._ranked: list[tuple[str, str, int]] = []
        self._trigrams: dict[str, list[int]] = {}
        self.reload_danbooru()

    def reload_danbooru(self) -> None:
        records: dict[str, tuple[str, int]] = {}
        try:
            with self.csv_path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames:
                    raise ValueError("The tag CSV has no header.")
                name_field = _find_field(reader.fieldnames, ("name", "tag", "tag_name"))
                count_field = _find_field(
                    reader.fieldnames,
                    ("post_count", "count", "posts", "tag_count"),
                )
                for row in reader:
                    name = transform_tag_name(
                        (row.get(name_field) or "").strip(),
                        underscores_to_spaces=self.underscores_to_spaces,
                        escape_parentheses=self.escape_parentheses,
                    )
                    if not name:
                        continue
                    try:
                        count = int(float(row.get(count_field) or 0))
                    except ValueError:
                        continue
                    key = _search_key(name)
                    existing = records.get(key)
                    if existing is None or count > existing[1]:
                        records[key] = (name, count)
        except FileNotFoundError:
            pass
        self._danbooru = records
        self._rebuild_index()
        self.changed.emit()

    def set_transform_options(
        self, *, underscores_to_spaces: bool, escape_parentheses: bool
    ) -> None:
        changed = (
            self.underscores_to_spaces != underscores_to_spaces
            or self.escape_parentheses != escape_parentheses
        )
        self.underscores_to_spaces = underscores_to_spaces
        self.escape_parentheses = escape_parentheses
        if changed:
            self.reload_danbooru()

    def set_folder_entries(self, entries: Iterable[ImageEntry]) -> None:
        counts = Counter(tag for entry in entries for tag in entry.tags)
        self._folder = {
            _search_key(name): (name, count) for name, count in counts.items()
        }
        self._rebuild_index()
        self.changed.emit()

    def clear_folder_tags(self) -> None:
        if not self._folder:
            return
        self._folder.clear()
        self._rebuild_index()
        self.changed.emit()

    def suggestions(self, text: str, limit: int = 12) -> list[str]:
        query = _search_key(pending_tag(text))
        if not query or limit <= 0:
            return []
        completed_text, separator, _pending = text.rpartition(",")
        completed = (
            {
                _search_key(tag)
                for tag in completed_text.split(",")
                if tag.strip()
            }
            if separator
            else set()
        )
        if len(query) >= 3:
            grams = {query[index : index + 3] for index in range(len(query) - 2)}
            posting_lists = [self._trigrams.get(gram, ()) for gram in grams]
            if not posting_lists or any(not posting for posting in posting_lists):
                return []
            candidates: Iterable[int] = min(posting_lists, key=len)
        else:
            candidates = range(len(self._ranked))

        result: list[str] = []
        for index in candidates:
            name, key, _count = self._ranked[index]
            if key not in completed and query in key:
                result.append(name)
                if len(result) == limit:
                    break
        return result

    @property
    def size(self) -> int:
        return len(self._ranked)

    def _rebuild_index(self) -> None:
        merged = dict(self._danbooru)
        for key, (name, count) in self._folder.items():
            existing = merged.get(key)
            merged[key] = (name, max(count, existing[1] if existing else 0))
        self._ranked = sorted(
            ((name, key, count) for key, (name, count) in merged.items()),
            key=lambda item: (-item[2], item[1], item[0]),
        )
        trigrams: dict[str, list[int]] = {}
        for index, (_name, key, _count) in enumerate(self._ranked):
            for gram in {key[offset : offset + 3] for offset in range(len(key) - 2)}:
                trigrams.setdefault(gram, []).append(index)
        self._trigrams = trigrams


class TagCompleter(QObject):
    """Autocomplete for a comma-separated tag line edit.

    The popup is deliberately independent of ``QCompleter``.  Qt's built-in
    completer consumes navigation keys and applies the current row while it
    moves, which makes it impossible to keep the pending text untouched.
    """

    def __init__(self, line_edit: QLineEdit, library: TagLibrary) -> None:
        super().__init__(line_edit)
        self._line_edit = line_edit
        self._library = library
        self._model = QStringListModel(self)
        self._popup = _TagCompletionPopup(line_edit.window())
        self._popup.setModel(self._model)
        self._popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._popup.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._popup.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._popup.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._popup.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._popup.installEventFilter(self)
        self._popup.viewport().installEventFilter(self)
        line_edit.installEventFilter(self)
        line_edit.textChanged.connect(self.refresh)
        library.changed.connect(self.refresh)
        self.refresh()

    def popup(self) -> QListView:
        return self._popup

    def splitPath(self, path: str) -> list[str]:
        return [pending_tag(path)]

    def pathFromIndex(self, index) -> str:
        return replace_pending_tag(self._line_edit.text(), str(index.data()))

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._line_edit:
            if event.type() == QEvent.Type.FocusIn:
                self.refresh()
                return False
            if event.type() == QEvent.Type.FocusOut:
                self._popup.hide()
                return False
            if event.type() != QEvent.Type.KeyPress:
                return False
            key_event = event if isinstance(event, QKeyEvent) else None
            if key_event is None:
                return False
            key = key_event.key()
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                if self._popup.isVisible() and self._model.rowCount() > 0:
                    self._move_popup_selection(key)
                    return True
                return False
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._popup.isVisible():
                    index = self._popup.currentIndex()
                    if index.isValid():
                        self._insert_completion(index)
                        return True
                    self._popup.hide()
                # Returning False preserves QLineEdit.returnPressed.
                return False
            return False

        if obj is self._popup or obj is self._popup.viewport():
            if event.type() == QEvent.Type.KeyPress:
                key_event = event if isinstance(event, QKeyEvent) else None
                if key_event is not None and key_event.key() in (
                    Qt.Key.Key_Up,
                    Qt.Key.Key_Down,
                ):
                    self._move_popup_selection(key_event.key())
                    return True
                if key_event is not None and key_event.key() in (
                    Qt.Key.Key_Return,
                    Qt.Key.Key_Enter,
                ):
                    index = self._popup.currentIndex()
                    if index.isValid():
                        self._insert_completion(index)
                    return True
            return False

        return False

    def _move_popup_selection(self, key: int) -> bool:
        popup = self._popup
        count = self._model.rowCount()
        if count == 0:
            return False
        current_row = popup.currentIndex().row()
        if key == Qt.Key.Key_Down:
            row = 0 if current_row < 0 else min(current_row + 1, count - 1)
        elif key == Qt.Key.Key_Up:
            row = 0 if current_row < 0 else max(current_row - 1, 0)
        else:
            return False
        selection_model = popup.selectionModel()
        if selection_model is None:
            return False
        index = self._model.index(row, 0)
        selection_model.setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )
        popup.scrollTo(index)
        return True

    def _clear_popup_selection(self) -> None:
        popup = self._popup
        selection_model = popup.selectionModel()
        if selection_model is None:
            return
        popup.clearSelection()
        popup.setCurrentIndex(QModelIndex())

    def _insert_completion(self, index) -> None:
        updated = self.pathFromIndex(index)
        self._line_edit.setText(updated)
        self._line_edit.setCursorPosition(len(self._line_edit.text()))
        self._popup.hide()

    def refresh(self, text: str | None = None) -> None:
        value = self._line_edit.text() if text is None else text
        suggestions = self._library.suggestions(value)
        self._model.setStringList(suggestions)
        self._clear_popup_selection()
        if suggestions and self._line_edit.hasFocus():
            self._show_popup()
        else:
            self._popup.hide()

    def _show_popup(self) -> None:
        popup = self._popup
        popup.ensurePolished()
        row_height = popup.sizeHintForRow(0) or self._line_edit.fontMetrics().height()
        visible_rows = min(self._model.rowCount(), 12)
        height = row_height * visible_rows + 2
        width = max(self._line_edit.width(), popup.sizeHintForColumn(0) + 24)
        top_left = self._line_edit.mapToGlobal(QPoint(0, self._line_edit.height()))
        popup.setGeometry(top_left.x(), top_left.y(), width, height)
        popup.show()
        popup.raise_()
        self._line_edit.setFocus()


class _TagCompletionPopup(QListView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            selection_model = self.selectionModel()
            if index.isValid() and selection_model is not None:
                selection_model.setCurrentIndex(
                    index,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect,
                )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def attach_tag_completer(
    line_edit: QLineEdit, library: TagLibrary | None
) -> TagCompleter | None:
    if library is None:
        return None
    completer = TagCompleter(line_edit, library)
    line_edit.setProperty("tag_completer", completer)
    return completer


class PlainTextTagCompleter(QObject):
    def __init__(self, edit: QPlainTextEdit, library: TagLibrary) -> None:
        super().__init__(edit)
        self._edit = edit
        self._library = library
        self._model = QStringListModel(self)
        self.completer = QCompleter(self._model, self)
        self.completer.setWidget(edit)
        self.completer.setCompletionMode(
            QCompleter.CompletionMode.UnfilteredPopupCompletion
        )
        self.completer.setMaxVisibleItems(12)
        self.completer.activated.connect(self._insert_completion)
        edit.textChanged.connect(self.refresh)
        library.changed.connect(self.refresh)

    def refresh(self) -> None:
        text = self._edit.toPlainText()
        suggestions = self._library.suggestions(text)
        self._model.setStringList(suggestions)
        popup = self.completer.popup()
        if suggestions and self._edit.hasFocus():
            rectangle = self._edit.cursorRect()
            if popup is not None:
                rectangle.setWidth(
                    popup.sizeHintForColumn(0)
                    + popup.verticalScrollBar().sizeHint().width()
                )
            self.completer.complete(rectangle)
        elif popup is not None:
            popup.hide()

    def _insert_completion(self, completion: str) -> None:
        cursor = self._edit.textCursor()
        text = self._edit.toPlainText()
        updated = replace_pending_tag(text, completion)
        self._edit.setPlainText(updated)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._edit.setTextCursor(cursor)


def attach_plain_text_tag_completer(
    edit: QPlainTextEdit, library: TagLibrary | None
) -> PlainTextTagCompleter | None:
    if library is None:
        return None
    completer = PlainTextTagCompleter(edit, library)
    edit.setProperty("tag_completer", completer)
    return completer


def _find_field(fieldnames: Sequence[str], choices: Sequence[str]) -> str:
    fields = {field.strip().casefold(): field for field in fieldnames}
    for choice in choices:
        if choice in fields:
            return fields[choice]
    raise ValueError(f"CSV is missing one of these columns: {', '.join(choices)}")


def download_danbooru_tags(
    destination: Path = DEFAULT_TAG_LIBRARY_PATH,
    *,
    minimum_posts: int = 20,
    proxy: str | None = "",
    progress: Callable[[int, str], None] | None = None,
    source_url: str | None = None,
) -> int:
    proxy_url = proxy.strip() if proxy is not None else None
    proxy_handler = (
        ProxyHandler()
        if proxy_url is None
        else ProxyHandler(
            {"http": proxy_url, "https": proxy_url} if proxy_url else {}
        )
    )
    opener = build_opener(proxy_handler)
    url = source_url or DATASET_TAGS_URL
    request = Request(url, headers={"User-Agent": "tag-gui/0.1"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with opener.open(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length", 0) or 0)
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".jsonl", delete=False, dir=destination.parent
            ) as download_stream:
                temporary_path = Path(download_stream.name)
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    download_stream.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        percent = int(downloaded * 70 / total) if total else 0
                        progress(percent, "Downloading tag data...")

        with temporary_path.open("r", encoding="utf-8-sig") as source:
            records: dict[str, tuple[str, int]] = {}
            for row_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in tags.jsonl at line {row_number}: {exc.msg}"
                    ) from exc
                if not isinstance(row, dict):
                    raise ValueError(
                        f"Expected a JSON object in tags.jsonl at line {row_number}."
                    )
                name_value = _mapping_value(row, ("name", "tag", "tag_name"))
                count_value = _mapping_value(
                    row, ("post_count", "count", "posts", "tag_count")
                )
                name = str(name_value).strip() if name_value is not None else ""
                try:
                    count = (
                        int(float(str(count_value)))
                        if count_value is not None
                        else 0
                    )
                except (TypeError, ValueError):
                    continue
                if name and count >= minimum_posts:
                    key = _search_key(name)
                    existing = records.get(key)
                    if existing is None or count > existing[1]:
                        records[key] = (name, count)
                if progress and row_number % 20_000 == 0:
                    progress(75, f"Filtering tags ({row_number:,} rows)...")

        output_fd, output_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(output_fd, "w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output, lineterminator="\n")
                writer.writerow(("name", "post_count"))
                sorted_records: list[tuple[str, int]] = sorted(
                    records.values(), key=_record_sort_key
                )
                writer.writerows(sorted_records)
                output.flush()
                os.fsync(output.fileno())
            os.replace(output_name, destination)
        except BaseException:
            try:
                os.unlink(output_name)
            except FileNotFoundError:
                pass
            raise
        if progress:
            progress(100, "Tag library ready.")
        return len(records)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _record_sort_key(item: tuple[str, int]) -> tuple[int, str]:
    return -item[1], _search_key(item[0])


def _mapping_value(
    mapping: dict[object, object], choices: Sequence[str]
) -> object | None:
    fields = {
        str(key).strip().casefold(): value for key, value in mapping.items()
    }
    return next((fields[choice] for choice in choices if choice in fields), None)


class _DownloadWorker(QObject):
    progress = Signal(int, str)
    completed = Signal(int, str)
    failed = Signal(str)

    def __init__(
        self, minimum_posts: int, proxy: str | None, destination: Path
    ) -> None:
        super().__init__()
        self.minimum_posts = minimum_posts
        self.proxy = proxy
        self.destination = destination

    def run(self) -> None:
        try:
            count = download_danbooru_tags(
                self.destination,
                minimum_posts=self.minimum_posts,
                proxy=self.proxy,
                progress=self.progress.emit,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.completed.emit(count, str(self.destination))


class DownloadTagsDialog(QDialog):
    downloaded = Signal(str)
    library_changed = Signal(str)

    def __init__(
        self,
        parent=None,
        destination: Path = DEFAULT_TAG_LIBRARY_PATH,
        *,
        proxy: str | None = "",
    ) -> None:
        super().__init__(parent)
        self.destination = destination
        self.proxy = proxy.strip() if proxy is not None else None
        self._thread: QThread | None = None
        self._worker: _DownloadWorker | None = None
        self.setWindowTitle("Manage Tag Library")
        self.setMinimumWidth(520)

        self.minimum_posts_input = QSpinBox()
        self.minimum_posts_input.setRange(0, 2_000_000_000)
        self.minimum_posts_input.setValue(20)
        self.minimum_posts_input.setSuffix(" posts")
        self.destination_label = QLabel(str(destination))
        self.destination_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        form = QFormLayout()
        form.addRow("Minimum post count", self.minimum_posts_input)
        form.addRow("Save to", self.destination_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.status_label = QLabel("Ready to download.")
        self.status_label.setWordWrap(True)
        self.download_button = QPushButton("Download")
        self.close_button = QPushButton("Close")
        self.download_button.clicked.connect(self._start_download)
        self.close_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.download_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

    def _start_download(self) -> None:
        if self._thread is not None:
            return
        self.download_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.minimum_posts_input.setEnabled(False)
        self.progress_bar.setValue(0)
        thread = QThread(self)
        worker = _DownloadWorker(
            self.minimum_posts_input.value(),
            self.proxy,
            self.destination,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._show_progress)
        worker.completed.connect(self._download_completed)
        worker.failed.connect(self._download_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _show_progress(self, value: int, message: str) -> None:
        self.progress_bar.setValue(value)
        self.status_label.setText(message)

    def _download_completed(self, count: int, path: str) -> None:
        self.progress_bar.setValue(100)
        self.status_label.setText(f"Saved {count:,} tags to {path}")
        self.downloaded.emit(path)
        self.library_changed.emit(path)

    def _download_failed(self, message: str) -> None:
        self.status_label.setText(message)
        QMessageBox.critical(self, "Could Not Download Tags", message)

    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self.close_button.setEnabled(True)
        self.download_button.setEnabled(True)
        self.minimum_posts_input.setEnabled(True)

    def set_proxy(self, proxy: str | None) -> None:
        self.proxy = proxy.strip() if proxy is not None else None

    @override
    def reject(self) -> None:
        if self._thread is None:
            super().reject()

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is None:
            event.accept()
        else:
            event.ignore()


def _csv_tag_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("The tag CSV has no header.")
        name_field = _find_field(reader.fieldnames, ("name", "tag", "tag_name"))
        return sum(1 for row in reader if (row.get(name_field) or "").strip())
