from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast, override

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QFont,
    QKeyEvent,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from .domain import ImageEntry, normalize_tags, parse_tags
from .preview import ImageView, PreviewLoader
from .python_syntax import PythonSyntaxHighlighter
from .storage import (
    BatchCommitResult,
    BatchPreflightError,
    WriteRequest,
    write_tags_batch,
)
from .tag_library import TagLibrary, attach_plain_text_tag_completer


DEFAULT_PROCESS_CODE = "def process(fn: str, tags: set[str]) -> set[str]:\n\treturn tags"

ENTRY_ROLE = int(Qt.ItemDataRole.UserRole) + 1


@dataclass(frozen=True)
class BulkChange:
    entry: ImageEntry
    proposed_tags: tuple[str, ...]


def _folder_ancestors(path: Path) -> list[Path]:
    if path == Path("."):
        return []
    return [Path(*path.parts[:length]) for length in range(1, len(path.parts) + 1)]


class BulkOperationDialog(QDialog):
    """Run Python tag processing and approve changed images one at a time."""

    def __init__(
        self,
        entries: Sequence[ImageEntry],
        parent=None,
        *,
        root_directory: Path,
        tag_library: TagLibrary | None = None,
    ) -> None:
        super().__init__(parent)
        self._entries = [entry for entry in entries if entry.editable]
        self._root_directory = root_directory
        self._tag_library = tag_library
        self._updating_checks = False
        self._changes: list[BulkChange] = []
        self._current_index = 0
        self._approved: dict[int, list[str]] = {}
        self._decided: set[int] = set()
        self._allow_close = False
        self.commit_result: BatchCommitResult | None = None

        self.setWindowTitle("Bulk Operation")
        self.resize(1100, 760)

        self.pages = QStackedWidget()
        self.selection_page = self._create_selection_page()
        self.code_page = self._create_code_page()
        self.approval_page = self._create_approval_page()
        self.pages.addWidget(self.selection_page)
        self.pages.addWidget(self.code_page)
        self.pages.addWidget(self.approval_page)

        layout = QVBoxLayout(self)
        layout.addWidget(self.pages)

        self._populate_tree()
        self._update_selection()
        self._install_approval_key_filter()

    def _create_selection_page(self) -> QWidget:
        page = QWidget()
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderLabel("Folder / Image")
        self.folder_tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.folder_tree.itemChanged.connect(self._check_changed)
        self.folder_selection_label = QLabel()
        self.selection_next_button = QPushButton("Next")
        self.selection_next_button.clicked.connect(self._show_code_page)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel_button)
        buttons.addWidget(self.selection_next_button)

        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Select images to process"))
        layout.addWidget(self.folder_tree, 1)
        layout.addWidget(self.folder_selection_label)
        layout.addLayout(buttons)
        return page

    def _create_code_page(self) -> QWidget:
        page = QWidget()
        self.code_input = QPlainTextEdit()
        self.code_input.setPlainText(DEFAULT_PROCESS_CODE)
        self.code_input.setPlaceholderText(
            "Define process(fn: str, tags: set[str]) -> set[str] here."
        )
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.code_input.setFont(font)
        self.code_input.setTabStopDistance(
            self.code_input.fontMetrics().horizontalAdvance(" ") * 4
        )
        self.highlighter = PythonSyntaxHighlighter(self.code_input.document())

        self.code_error_label = QLabel()
        self.code_error_label.setWordWrap(True)
        self.code_error_label.setStyleSheet("QLabel { color: #b42318; }")
        self.code_error_label.hide()

        back_button = QPushButton("Back")
        back_button.clicked.connect(self._show_selection_page)
        self.run_button = QPushButton("Review Changes")
        self.run_button.clicked.connect(self._run_code)

        buttons = QHBoxLayout()
        buttons.addWidget(back_button)
        buttons.addStretch(1)
        buttons.addWidget(self.run_button)

        layout = QVBoxLayout(page)
        layout.addWidget(self.code_input, 1)
        layout.addWidget(self.code_error_label)
        layout.addLayout(buttons)
        return page

    def _create_approval_page(self) -> QWidget:
        page = QWidget()
        self.progress_label = QLabel()
        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.image_view = ImageView()
        self.preview_loader = PreviewLoader(self)
        self.preview_loader.loaded.connect(self._preview_loaded)

        self.original_tags_input = QPlainTextEdit()
        self.original_tags_input.setReadOnly(True)
        self.original_tags_input.setMaximumHeight(100)
        self.changes_text = QTextEdit()
        self.changes_text.setReadOnly(True)
        self.changes_text.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.new_tags_input = QPlainTextEdit()
        self.new_tags_input.setPlaceholderText("Comma-separated tags")
        self.new_tags_input.setMaximumHeight(100)
        self.new_tags_input.textChanged.connect(self._update_changes_text)
        self.new_tags_completer = attach_plain_text_tag_completer(
            self.new_tags_input, self._tag_library
        )

        details_layout = QVBoxLayout()
        details_layout.setContentsMargins(12, 0, 0, 0)
        details_layout.setSpacing(8)
        details_layout.addWidget(QLabel("Current tags"))
        details_layout.addWidget(self.original_tags_input)
        details_layout.addWidget(QLabel("Tag changes"))
        details_layout.addWidget(self.changes_text, 1)
        details_layout.addWidget(QLabel("Result tags"))
        details_layout.addWidget(self.new_tags_input)
        details_panel = QWidget()
        details_panel.setLayout(details_layout)

        self.approval_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.approval_splitter.addWidget(self.image_view)
        self.approval_splitter.addWidget(details_panel)
        self.approval_splitter.setStretchFactor(0, 2)
        self.approval_splitter.setStretchFactor(1, 1)
        self.approval_splitter.setSizes([720, 360])

        self.discard_button = QPushButton("Back")
        self.discard_button.clicked.connect(self._discard_and_edit_code)
        self.skip_button = QPushButton("Skip")
        self.skip_button.clicked.connect(self._skip_current)
        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.clicked.connect(self._confirm_current)

        buttons = QHBoxLayout()
        buttons.addWidget(self.discard_button)
        buttons.addStretch(1)
        buttons.addWidget(self.skip_button)
        buttons.addWidget(self.confirm_button)

        layout = QVBoxLayout(page)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.path_label)
        layout.addWidget(self.approval_splitter, 1)
        layout.addLayout(buttons)
        return page

    def _install_approval_key_filter(self) -> None:
        self.approval_page.installEventFilter(self)
        for widget in self.approval_page.findChildren(QWidget):
            widget.installEventFilter(self)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            self.pages.currentWidget() is self.approval_page
            and event.type() == QEvent.Type.KeyPress
        ):
            key_event = cast(QKeyEvent, event)
            if key_event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                self._confirm_current()
                return True
            if key_event.key() == Qt.Key.Key_Space:
                self._skip_current()
                return True
        return super().eventFilter(watched, event)

    def _populate_tree(self) -> None:
        self.folder_tree.clear()
        root = self._root_directory
        root_item = QTreeWidgetItem([root.name or str(root)])
        self._make_checkable(root_item)
        self.folder_tree.addTopLevelItem(root_item)

        folders: dict[Path, QTreeWidgetItem] = {Path("."): root_item}
        relative_folders = {
            ancestor
            for entry in self._entries
            for ancestor in _folder_ancestors(
                entry.image_path.parent.relative_to(root)
            )
        }
        for folder in sorted(
            relative_folders,
            key=lambda path: (len(path.parts), path.as_posix().casefold()),
        ):
            item = QTreeWidgetItem([folder.name])
            self._make_checkable(item)
            folders[folder.parent].addChild(item)
            folders[folder] = item

        for entry in self._entries:
            relative_parent = entry.image_path.parent.relative_to(root)
            item = QTreeWidgetItem([entry.image_path.name])
            self._make_checkable(item)
            item.setData(0, ENTRY_ROLE, entry)
            folders[relative_parent].addChild(item)

        self.folder_tree.expandAll()
        root_item.setCheckState(0, Qt.CheckState.Checked)

    @staticmethod
    def _make_checkable(item: QTreeWidgetItem) -> None:
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)

    def _check_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_checks or column != 0:
            return
        self._updating_checks = True
        try:
            state = item.checkState(0)
            if state in (Qt.CheckState.Checked, Qt.CheckState.Unchecked):
                self._set_descendants(item, state)
            self._update_ancestors(item.parent())
        finally:
            self._updating_checks = False
        self._update_selection()

    def _set_descendants(
        self, item: QTreeWidgetItem, state: Qt.CheckState
    ) -> None:
        for index in range(item.childCount()):
            child = item.child(index)
            if child is not None:
                child.setCheckState(0, state)
                self._set_descendants(child, state)

    def _update_ancestors(self, item: QTreeWidgetItem | None) -> None:
        while item is not None:
            states = [
                child.checkState(0)
                for index in range(item.childCount())
                if (child := item.child(index)) is not None
            ]
            if states and all(state == Qt.CheckState.Checked for state in states):
                item.setCheckState(0, Qt.CheckState.Checked)
            elif states and all(
                state == Qt.CheckState.Unchecked for state in states
            ):
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                item.setCheckState(0, Qt.CheckState.PartiallyChecked)
            item = item.parent()

    def _checked_entries(self) -> list[ImageEntry]:
        checked: set[Path] = set()
        iterator = QTreeWidgetItemIterator(self.folder_tree)
        while iterator.value() is not None:
            item = iterator.value()
            if item.checkState(0) == Qt.CheckState.Checked:
                entry = item.data(0, ENTRY_ROLE)
                if isinstance(entry, ImageEntry):
                    checked.add(entry.image_path)
            iterator += 1
        return [entry for entry in self._entries if entry.image_path in checked]

    def _update_selection(self) -> None:
        count = len(self._checked_entries())
        self.folder_selection_label.setText(f"{count} image(s) selected.")
        self.selection_next_button.setEnabled(count > 0)

    def _show_selection_page(self) -> None:
        self.code_error_label.hide()
        self.pages.setCurrentWidget(self.selection_page)

    def _show_code_page(self) -> None:
        self.code_error_label.hide()
        self.pages.setCurrentWidget(self.code_page)
        self.code_input.setFocus()

    def _show_code_error(self, message: str) -> None:
        self.code_error_label.setText(message)
        self.code_error_label.show()

    def _run_code(self) -> None:
        self.code_error_label.clear()
        self.code_error_label.hide()
        try:
            code = compile(self.code_input.toPlainText(), "<bulk-operation>", "exec")
            namespace: dict[str, object] = {}
            exec(code, namespace)
        except Exception as exc:
            self._show_code_error(f"Could not compile or execute code: {exc}")
            return

        process = namespace.get("process")
        if not callable(process):
            self._show_code_error(
                "The code must define "
                "process(fn: str, tags: set[str]) -> set[str]."
            )
            return
        process_function = cast(Callable[[str, set[str]], object], process)

        changes: list[BulkChange] = []
        for entry in self._checked_entries():
            try:
                result = process_function(entry.image_path.name, set(entry.tags))
            except Exception as exc:
                self._show_code_error(
                    f"process() failed for {entry.image_path.name}: {exc}"
                )
                return
            if not isinstance(result, set):
                self._show_code_error(
                    "process() must return set[str]; it returned "
                    f"{type(result).__name__} for {entry.image_path.name}."
                )
                return
            if not all(isinstance(tag, str) for tag in result):
                self._show_code_error(
                    "process() must return set[str]; a non-string tag was returned "
                    f"for {entry.image_path.name}."
                )
                return

            proposed_tags = normalize_tags(result)
            if set(proposed_tags) == set(entry.tags):
                continue
            changes.append(BulkChange(entry, tuple(proposed_tags)))

        if not changes:
            QMessageBox.information(
                self,
                "No Tag Changes",
                "The code produced no tag changes for the selected images.",
            )
            return

        self._changes = changes
        self._current_index = 0
        self._approved.clear()
        self._decided.clear()
        self.pages.setCurrentWidget(self.approval_page)
        self._load_current_change()

    @property
    def current_change(self) -> BulkChange:
        return self._changes[self._current_index]

    def _load_current_change(self) -> None:
        change = self.current_change
        self.progress_label.setText(
            f"Image {self._current_index + 1} of {len(self._changes)}"
            f" | Confirmed {len(self._approved)}"
            f" | Skipped {len(self._decided) - len(self._approved)}"
        )
        self.path_label.setText(str(change.entry.image_path))
        self.original_tags_input.setPlainText(", ".join(change.entry.tags))
        self.new_tags_input.setPlainText(", ".join(change.proposed_tags))
        self.image_view.clear_image("Loading image...")
        self.preview_loader.load(change.entry.image_path)
        self.confirm_button.setFocus()
        self._update_changes_text()

    def _current_new_tags(self) -> list[str]:
        return normalize_tags(parse_tags(self.new_tags_input.toPlainText()))

    def _update_changes_text(self, *_args) -> None:
        if not self._changes:
            self.changes_text.clear()
            return
        original = set(self.current_change.entry.tags)
        proposed = set(self._current_new_tags())
        removed = sorted(original - proposed, key=lambda tag: (tag.casefold(), tag))
        added = sorted(proposed - original, key=lambda tag: (tag.casefold(), tag))

        self.changes_text.clear()
        cursor = QTextCursor(self.changes_text.document())
        for prefix, tags, color in (
            ("[-]", removed, QColor("#b42318")),
            ("[+]", added, QColor("#027a48")),
        ):
            format_ = QTextCharFormat()
            format_.setForeground(color)
            for tag in tags:
                if not cursor.atStart():
                    cursor.insertBlock()
                cursor.insertText(f"{prefix} {tag}", format_)

    def _confirm_current(self) -> None:
        if self.pages.currentWidget() is not self.approval_page:
            return
        tags = self._current_new_tags()
        self._decided.add(self._current_index)
        if set(tags) == set(self.current_change.entry.tags):
            self._approved.pop(self._current_index, None)
        else:
            self._approved[self._current_index] = tags
        self._advance_or_commit()

    def _skip_current(self) -> None:
        if self.pages.currentWidget() is not self.approval_page:
            return
        self._decided.add(self._current_index)
        self._approved.pop(self._current_index, None)
        self._advance_or_commit()

    def _advance_or_commit(self) -> None:
        if self._current_index + 1 < len(self._changes):
            self._current_index += 1
            self._load_current_change()
            return
        self._commit()

    def _commit(self) -> None:
        requests = [
            WriteRequest(
                path=self._changes[index].entry.tag_path,
                tags=tags,
                expected_bytes=self._changes[index].entry.source_bytes,
            )
            for index, tags in sorted(self._approved.items())
        ]
        if not requests:
            self.commit_result = BatchCommitResult([], {})
            self._allow_close = True
            self.accept()
            return
        try:
            result = write_tags_batch(requests)
        except BatchPreflightError as exc:
            QMessageBox.critical(self, "Could Not Save Tags", str(exc))
            return

        self.commit_result = result
        self._allow_close = True
        if result.failures:
            details = "\n".join(
                f"{path.name}: {message}"
                for path, message in result.failures.items()
            )
            QMessageBox.warning(
                self,
                "Some Files Were Not Saved",
                f"Saved {len(result.succeeded)} file(s).\n\n{details}",
            )
        self.accept()

    def _discard_and_edit_code(self) -> None:
        self.preview_loader.clear()
        self._changes.clear()
        self._approved.clear()
        self._decided.clear()
        self._current_index = 0
        self.pages.setCurrentWidget(self.code_page)
        self.code_input.setFocus()

    def _preview_loaded(self, image, error: str) -> None:
        if error or image.isNull():
            self.image_view.clear_image(f"Could not display image\n{error}")
        else:
            self.image_view.set_image(image)

    def _confirm_close(self) -> bool:
        if self.pages.currentWidget() is not self.approval_page:
            return True
        answer = QMessageBox.question(
            self,
            "Discard Bulk Operation?",
            "Discard the pending bulk tag changes?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    @override
    def reject(self) -> None:
        if self._allow_close or self._confirm_close():
            self._allow_close = True
            super().reject()

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close or self._confirm_close():
            self._allow_close = True
            event.accept()
        else:
            event.ignore()
