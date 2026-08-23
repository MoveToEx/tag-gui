from __future__ import annotations

from pathlib import Path
from typing import cast, override

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QCloseEvent, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from .domain import (
    ImageEntry,
    TagOperation,
    TraversalItem,
    TraversalSession,
    filter_traversal_entries,
    parse_requested_tags,
    parse_tags,
)
from .preview import ImageView, PreviewLoader
from .storage import (
    BatchCommitResult,
    BatchPreflightError,
    WriteRequest,
    write_tags_batch,
)


def _folder_ancestors(path: Path) -> list[Path]:
    if path == Path("."):
        return []
    return [Path(*path.parts[:length]) for length in range(1, len(path.parts) + 1)]


class TraversalDialog(QDialog):
    def __init__(
        self,
        entries: list[ImageEntry],
        operation: TagOperation,
        requested_tags: list[str] | None = None,
        parent=None,
        *,
        root_directory: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._entries = entries
        self._operation = operation
        self._requested_tags = requested_tags or []
        self._root_directory = root_directory
        self._session: TraversalSession | None = (
            TraversalSession(entries, operation, self._requested_tags)
            if root_directory is None
            else None
        )
        self.commit_result: BatchCommitResult | None = None
        self._allow_close = False
        self._populating_choices = False
        self._updating_folder_checks = False
        self._shortcuts: list[QShortcut] = []
        self._started = root_directory is None

        title = {
            TagOperation.ADD: "Add Tags Across Folder",
            TagOperation.DELETE: "Delete Tags Across Folder",
            TagOperation.TOGGLE: "Toggle Tags Across Folder",
        }[operation]
        self.setWindowTitle(title)
        self.resize(980, 680)

        self.progress_label = QLabel()
        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.shortcut_hint = QLabel(
            "Keyboard: ↑/↓ select tag · Space toggle · A all · Enter/→ next · ← previous"
        )
        self.shortcut_hint.setStyleSheet("QLabel { color: #667085; }")

        self.image_view = ImageView()
        self.preview_loader = PreviewLoader(self)
        self.preview_loader.loaded.connect(self._preview_loaded)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(8, 0, 0, 0)
        detail_layout.addWidget(QLabel("Current tags"))
        self.current_tags = QPlainTextEdit()
        self.current_tags.setReadOnly(True)
        self.current_tags.setMaximumHeight(100)
        detail_layout.addWidget(self.current_tags)

        self.choice_label = QLabel("Tags to apply")
        detail_layout.addWidget(self.choice_label)
        self.choices = QListWidget()
        self.choices.installEventFilter(self)
        self.choices.itemChanged.connect(self._update_result_preview)
        detail_layout.addWidget(self.choices, 1)

        self.temporary_label = QLabel("Temporary extra tags")
        self.temporary_input = QLineEdit()
        self.temporary_input.setPlaceholderText(
            "Comma-separated tags for this image only"
        )
        self.temporary_input.setClearButtonEnabled(True)
        self.temporary_input.textChanged.connect(self._update_temporary_button)
        self.temporary_input.returnPressed.connect(self._append_temporary_tags)
        self.temporary_add_button = QPushButton("+")
        self.temporary_add_button.setToolTip(
            "Append these tags to the current image operation"
        )
        self.temporary_add_button.setFixedWidth(34)
        self.temporary_add_button.clicked.connect(self._append_temporary_tags)
        temporary_layout = QHBoxLayout()
        temporary_layout.setContentsMargins(0, 0, 0, 0)
        temporary_layout.addWidget(self.temporary_input, 1)
        temporary_layout.addWidget(self.temporary_add_button)
        detail_layout.addWidget(self.temporary_label)
        detail_layout.addLayout(temporary_layout)

        detail_layout.addWidget(QLabel("Result"))
        self.result_tags = QPlainTextEdit()
        self.result_tags.setReadOnly(True)
        self.result_tags.setMaximumHeight(100)
        detail_layout.addWidget(self.result_tags)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.image_view)
        splitter.addWidget(detail_widget)
        splitter.setSizes([620, 340])

        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("Next")
        self.apply_all_button = QPushButton("Apply All to All Images")
        self.apply_all_button.setToolTip(
            "Apply every available option to every image in this traversal"
        )
        bulk_action_available = operation in {
            TagOperation.ADD,
            TagOperation.DELETE,
        }
        self.apply_all_button.setVisible(bulk_action_available)
        self.apply_all_button.setEnabled(bulk_action_available)
        self.finish_button = QPushButton("Finish")
        self.stop_button = QPushButton("Stop")
        self.back_button.clicked.connect(self._back)
        self.next_button.clicked.connect(self._next)
        self.apply_all_button.clicked.connect(self._apply_all)
        self.finish_button.clicked.connect(self._finish)
        self.stop_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.stop_button)
        button_layout.addWidget(self.apply_all_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self.back_button)
        button_layout.addWidget(self.next_button)
        self.skip_button = self.next_button
        button_layout.addWidget(self.finish_button)

        self.folder_setup = QWidget()
        folder_layout = QVBoxLayout(self.folder_setup)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.addWidget(
            QLabel(
                "Check one or more folders whose images should be included. "
                "Checking a folder also checks all of its subfolders."
            )
        )
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderLabel("Folder")
        self.folder_tree.setSelectionMode(
            QTreeWidget.SelectionMode.NoSelection
        )
        self.folder_tree.itemChanged.connect(self._folder_check_changed)
        folder_layout.addWidget(self.folder_tree, 1)
        self.tag_input_label = QLabel("Tags to apply")
        folder_layout.addWidget(self.tag_input_label)
        self.tag_input = QLineEdit()
        self.tag_input.setPlaceholderText("Comma-separated tags")
        self.tag_input.setClearButtonEnabled(True)
        self.tag_input.textChanged.connect(self._update_folder_selection)
        self.tag_input.returnPressed.connect(self._start_selected_folder)
        folder_layout.addWidget(self.tag_input)
        self.folder_selection_label = QLabel()
        folder_layout.addWidget(self.folder_selection_label)
        setup_buttons = QHBoxLayout()
        self.cancel_setup_button = QPushButton("Cancel")
        self.start_button = QPushButton("Start Traversal")
        self.cancel_setup_button.clicked.connect(self.reject)
        self.start_button.clicked.connect(self._start_selected_folder)
        setup_buttons.addStretch(1)
        setup_buttons.addWidget(self.cancel_setup_button)
        setup_buttons.addWidget(self.start_button)
        folder_layout.addLayout(setup_buttons)

        self.traversal_widget = QWidget()
        traversal_layout = QVBoxLayout(self.traversal_widget)
        traversal_layout.setContentsMargins(0, 0, 0, 0)
        traversal_layout.addWidget(self.progress_label)
        traversal_layout.addWidget(self.path_label)
        traversal_layout.addWidget(self.shortcut_hint)
        traversal_layout.addWidget(splitter, 1)
        traversal_layout.addLayout(button_layout)

        layout = QVBoxLayout(self)
        layout.addWidget(self.folder_setup, 1)
        layout.addWidget(self.traversal_widget, 1)

        self._create_shortcuts()
        if root_directory is None:
            self.folder_setup.hide()
            self._load_current()
        else:
            self.tag_input.setText(", ".join(self._requested_tags))
            self._populate_folder_tree(root_directory)
            self.traversal_widget.hide()

    @property
    def session(self) -> TraversalSession:
        if self._session is None:
            raise RuntimeError("Traversal has not started.")
        return self._session

    def _populate_folder_tree(self, root_directory: Path) -> None:
        self.folder_tree.clear()
        root_label = root_directory.name or str(root_directory)
        root_item = QTreeWidgetItem([root_label])
        root_item.setFlags(root_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        root_item.setData(0, Qt.ItemDataRole.UserRole, str(root_directory))
        root_item.setCheckState(0, Qt.CheckState.Unchecked)
        self.folder_tree.addTopLevelItem(root_item)
        items: dict[Path, QTreeWidgetItem] = {Path("."): root_item}
        relative_folders = {
            ancestor
            for entry in self._entries
            for ancestor in _folder_ancestors(
                entry.image_path.parent.relative_to(root_directory)
            )
        }
        for relative_folder in sorted(
            relative_folders,
            key=lambda path: (
                len(path.parts),
                tuple(part.casefold() for part in path.parts),
                path.as_posix(),
            ),
        ):
            item = QTreeWidgetItem([relative_folder.name])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                str(root_directory / relative_folder),
            )
            item.setCheckState(0, Qt.CheckState.Unchecked)
            items[relative_folder.parent].addChild(item)
            items[relative_folder] = item
        self.folder_tree.expandAll()
        root_item.setCheckState(0, Qt.CheckState.Checked)

    def _folder_check_changed(
        self, item: QTreeWidgetItem, column: int
    ) -> None:
        if self._updating_folder_checks or column != 0:
            return
        self._updating_folder_checks = True
        try:
            state = item.checkState(0)
            if state in {Qt.CheckState.Checked, Qt.CheckState.Unchecked}:
                self._set_descendant_check_state(item, state)
            self._update_ancestor_check_states(item.parent())
        finally:
            self._updating_folder_checks = False
        self._update_folder_selection()

    def _set_descendant_check_state(
        self, item: QTreeWidgetItem, state: Qt.CheckState
    ) -> None:
        for index in range(item.childCount()):
            child = item.child(index)
            if child is None:
                continue
            child.setCheckState(0, state)
            self._set_descendant_check_state(child, state)

    def _update_ancestor_check_states(
        self, item: QTreeWidgetItem | None
    ) -> None:
        while item is not None:
            child_states = [
                child.checkState(0)
                for index in range(item.childCount())
                if (child := item.child(index)) is not None
            ]
            if child_states and all(
                state == Qt.CheckState.Unchecked for state in child_states
            ):
                state = Qt.CheckState.Unchecked
            else:
                state = Qt.CheckState.PartiallyChecked
            item.setCheckState(0, state)
            item = item.parent()

    def _checked_folders(self) -> list[Path]:
        folders: list[Path] = []
        iterator = QTreeWidgetItemIterator(self.folder_tree)
        while iterator.value() is not None:
            item = iterator.value()
            if item.checkState(0) == Qt.CheckState.Checked:
                value = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(value, str):
                    folders.append(Path(value))
            iterator += 1
        return folders

    def _entries_in_checked_folders(self) -> list[ImageEntry]:
        folders = self._checked_folders()
        return [
            entry
            for entry in self._entries
            if any(
                entry.image_path.parent == folder
                or folder in entry.image_path.parent.parents
                for folder in folders
            )
        ]

    def _update_folder_selection(self) -> None:
        entries = self._entries_in_checked_folders()
        requested: list[str] | None = None
        try:
            requested = parse_requested_tags(self.tag_input.text())
        except ValueError:
            pass
        if requested is not None:
            entries = filter_traversal_entries(
                entries, self._operation, requested
            )
        self.folder_selection_label.setText(
            f"{len(entries)} matching image(s) will be included."
        )
        self.start_button.setEnabled(bool(entries) and requested is not None)

    def _start_selected_folder(self) -> None:
        entries = self._entries_in_checked_folders()
        if not entries:
            return
        try:
            requested = parse_requested_tags(self.tag_input.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Tags", str(exc))
            self.tag_input.setFocus()
            return
        entries = filter_traversal_entries(entries, self._operation, requested)
        if not entries:
            QMessageBox.information(
                self,
                "Nothing to Traverse",
                "No images in the selected folder match this operation.",
            )
            return
        try:
            self._session = TraversalSession(entries, self._operation, requested)
        except ValueError as exc:
            QMessageBox.information(self, "Nothing to Traverse", str(exc))
            return
        self._requested_tags = requested
        self._started = True
        for shortcut in self._shortcuts:
            shortcut.setEnabled(True)
        self.folder_setup.hide()
        self.traversal_widget.show()
        self._load_current()

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.choices and event.type() == QEvent.Type.KeyPress:
            key = cast(QKeyEvent, event).key()
            if key == Qt.Key.Key_Up:
                self._move_tag_selection_up()
                return True
            if key == Qt.Key.Key_Down:
                self._move_tag_selection_down()
                return True
            if key == Qt.Key.Key_Space:
                self._toggle_current_choice()
                return True
            if key == Qt.Key.Key_A:
                self._toggle_all_choices()
                return True
            if key in {
                Qt.Key.Key_Return,
                Qt.Key.Key_Enter,
                Qt.Key.Key_Right,
            }:
                self._advance_from_keyboard()
                return True
            if key == Qt.Key.Key_Left:
                self._back()
                return True
        return super().eventFilter(watched, event)

    def _create_shortcuts(self) -> None:
        shortcuts = {
            "Up": self._move_tag_selection_up,
            "Down": self._move_tag_selection_down,
            "Space": self._toggle_current_choice,
            "Return": self._advance_from_keyboard,
            "Enter": self._advance_from_keyboard,
            "Right": self._advance_from_keyboard,
            "Left": self._back,
        }
        for key, callback in shortcuts.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            shortcut.setEnabled(self._started)
            self._shortcuts.append(shortcut)

    def _preview_loaded(self, image, error: str) -> None:
        if error or image.isNull():
            self.image_view.clear_image(f"Could not display image\n{error}")
        else:
            self.image_view.set_image(image)

    def _load_current(self) -> None:
        item = self.session.current_item
        number = self.session.current_index + 1
        self.progress_label.setText(f"Image {number} of {len(self.session.items)}")
        self.path_label.setText(item.image_path.name)
        self.current_tags.setPlainText(", ".join(item.original_tags) or "(none)")
        self.image_view.clear_image("Loading image...")
        self.preview_loader.load(item.image_path)

        self._populating_choices = True
        self.temporary_input.clear()
        self.choices.clear()
        self.choice_label.setText("Tags to apply")
        self.choices.show()
        selected = set(self.session.selected_for())
        for tag in self.session.eligible_for():
            choice = QListWidgetItem(tag)
            choice.setFlags(choice.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            choice.setCheckState(
                Qt.CheckState.Checked
                if tag in selected
                else Qt.CheckState.Unchecked
            )
            self.choices.addItem(choice)
        if self.choices.count():
            self.choices.setCurrentRow(0)
            self.choices.setFocus()
        show_temporary = self.session.operation in {
            TagOperation.ADD,
            TagOperation.DELETE,
        }
        self.temporary_label.setVisible(show_temporary)
        self.temporary_input.setVisible(show_temporary)
        self.temporary_add_button.setVisible(show_temporary)
        self._populating_choices = False
        self._update_temporary_button()
        self._update_result_preview()
        self._update_buttons()

    def _checked_tags(self) -> list[str]:
        return [
            self.choices.item(row).text()
            for row in range(self.choices.count())
            if self.choices.item(row).checkState() == Qt.CheckState.Checked
        ]

    def _temporary_tags(self) -> list[str]:
        return self.session.extra_tags_for()

    def _update_temporary_button(self, *_args) -> None:
        self.temporary_add_button.setEnabled(
            bool(parse_tags(self.temporary_input.text()))
        )

    def _append_temporary_tags(self) -> None:
        entered = parse_tags(self.temporary_input.text())
        if not entered:
            return
        extras = [*self.session.extra_tags_for(), *entered]
        self.session.apply_current(self._checked_tags(), extras)
        self.temporary_input.clear()
        self._update_result_preview()

    def _move_tag_selection(self, offset: int) -> None:
        if not self.choices.isVisible() or not self.choices.count():
            return
        row = self.choices.currentRow()
        if row < 0:
            row = 0
        self.choices.setCurrentRow(max(0, min(self.choices.count() - 1, row + offset)))
        self.choices.setFocus()

    def _move_tag_selection_up(self) -> None:
        self._move_tag_selection(-1)

    def _move_tag_selection_down(self) -> None:
        self._move_tag_selection(1)

    def _toggle_current_choice(self) -> None:
        if not self.choices.isVisible():
            return
        item = self.choices.currentItem()
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        self.choices.setFocus()

    def _toggle_all_choices(self) -> None:
        if not self.choices.isVisible() or not self.choices.count():
            return
        all_checked = all(
            self.choices.item(row).checkState() == Qt.CheckState.Checked
            for row in range(self.choices.count())
        )
        state = (
            Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked
        )
        self._populating_choices = True
        for row in range(self.choices.count()):
            self.choices.item(row).setCheckState(state)
        self._populating_choices = False
        self.choices.setFocus()
        self._update_result_preview()

    def _advance_from_keyboard(self) -> None:
        self._next()

    def _update_result_preview(self, *_args) -> None:
        if self._populating_choices:
            return
        selected = self._checked_tags()
        extras = self._temporary_tags()
        result = self.session.apply_current(selected, extras)
        self.result_tags.setPlainText(", ".join(result) or "(none)")
        self._update_buttons()

    def _update_buttons(self) -> None:
        at_last = self.session.at_last
        self.back_button.setEnabled(not self.session.at_first)
        self.next_button.setEnabled(not at_last)
        self.finish_button.setEnabled(True)

    def _next(self) -> None:
        if self.session.move_next():
            self._load_current()
        else:
            self._update_buttons()

    def _back(self) -> None:
        if self.session.move_back():
            self._load_current()

    def _finish(self) -> None:
        self._commit_changes(self.session.staged_changes())

    def _apply_all(self) -> None:
        if self.session.operation not in {TagOperation.ADD, TagOperation.DELETE}:
            return
        changes = self.session.all_available_changes()
        operation = {
            TagOperation.ADD: "add all available tags to",
            TagOperation.DELETE: "delete all available tags from",
            TagOperation.TOGGLE: "toggle all available tags on",
        }[self.session.operation]
        message = (
            f"This will immediately {operation} all "
            f"{len(self.session.items)} image(s) in this traversal.\n\n"
            f"{len(changes)} sidecar file(s) will change."
        )
        if self.session.has_changes:
            message += "\n\nAny manual selections already made will be ignored."
        message += "\n\nContinue?"
        answer = QMessageBox.question(
            self,
            "Apply All to All Images?",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._commit_changes(changes)

    def _commit_changes(
        self, changes: list[tuple[TraversalItem, list[str]]]
    ) -> None:
        if not changes:
            self.commit_result = BatchCommitResult([], {})
            self._allow_close = True
            self.accept()
            return

        requests = [
            WriteRequest(
                path=item.tag_path,
                tags=tags,
                expected_bytes=item.source_bytes,
            )
            for item, tags in changes
        ]
        try:
            result = write_tags_batch(requests)
        except BatchPreflightError as exc:
            QMessageBox.critical(self, "Could Not Save", str(exc))
            return

        self.commit_result = result
        self._allow_close = True
        if result.failures:
            details = "\n".join(
                f"{path.name}: {message}" for path, message in result.failures.items()
            )
            QMessageBox.warning(
                self,
                "Some Files Were Not Saved",
                f"Saved {len(result.succeeded)} file(s).\n\n{details}",
            )
        self.accept()

    def _confirm_discard(self) -> bool:
        if not self._started:
            return True
        if not self.session.has_changes:
            return True
        answer = QMessageBox.question(
            self,
            "Discard Traversal Changes?",
            "The traversal has uncommitted changes. Discard them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    @override
    def reject(self) -> None:
        if self._allow_close or self._confirm_discard():
            self._allow_close = True
            super().reject()

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close or self._confirm_discard():
            self._allow_close = True
            event.accept()
        else:
            event.ignore()
