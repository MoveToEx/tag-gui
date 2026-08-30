from __future__ import annotations

from pathlib import Path
from typing import cast, override

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from .domain import ImageEntry, ReviewSession, parse_tags
from .preview import ImageView, PreviewLoader
from .storage import BatchCommitResult, BatchPreflightError, WriteRequest, write_tags_batch


def _folder_ancestors(path: Path) -> list[Path]:
    if path == Path("."):
        return []
    return [Path(*path.parts[:length]) for length in range(1, len(path.parts) + 1)]


class ReviewDialog(QDialog):
    """Review existing tags one at a time and commit changes on Finish."""

    def __init__(
        self,
        entries: list[ImageEntry],
        parent=None,
        *,
        root_directory: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._entries = entries
        self._root_directory = root_directory
        self._session: ReviewSession | None = (
            ReviewSession(entries) if root_directory is None else None
        )
        self.commit_result: BatchCommitResult | None = None
        self._allow_close = False
        self._updating_checks = False
        self._started = root_directory is None
        self._shortcuts: list[QShortcut] = []

        self.setWindowTitle("Review Tags")
        self.resize(980, 680)

        self.folder_setup = QWidget()
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderLabel("Folder / Image")
        self.folder_tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.folder_tree.itemChanged.connect(self._check_changed)
        setup_label = QLabel(
            "Check folders or individual images to include in the review."
        )
        self.folder_selection_label = QLabel()
        self.start_button = QPushButton("Start Review")
        self.start_button.clicked.connect(self._start_selected)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        setup_buttons = QHBoxLayout()
        setup_buttons.addStretch(1)
        setup_buttons.addWidget(cancel_button)
        setup_buttons.addWidget(self.start_button)
        setup_layout = QVBoxLayout(self.folder_setup)
        setup_layout.addWidget(setup_label)
        setup_layout.addWidget(self.folder_tree, 1)
        setup_layout.addWidget(self.folder_selection_label)
        setup_layout.addLayout(setup_buttons)

        self.progress_label = QLabel()
        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.image_view = ImageView()
        self.preview_loader = PreviewLoader(self)
        self.preview_loader.loaded.connect(self._preview_loaded)

        self.tag_label = QLabel()
        self.tag_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tag_label.setWordWrap(True)
        self.tag_label.setStyleSheet(
            "QLabel { font-size: 28px; font-weight: 600; padding: 24px; "
            "color: #101828; background: #f2f4f7; }"
        )
        self.tag_label.ensurePolished()
        self.tag_label.setFixedHeight(
            self.tag_label.fontMetrics().lineSpacing() * 3 + 48
        )
        self.tag_status_list = QListWidget()
        self.tag_status_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self.tag_status_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tag_status_list.setFixedHeight(150)
        self.temporary_input = QLineEdit()
        self.temporary_input.setPlaceholderText("Comma-separated tags for this image")
        self.temporary_input.setClearButtonEnabled(True)
        self.temporary_input.textChanged.connect(self._update_temporary_button)
        self.temporary_input.returnPressed.connect(self._append_temporary_tags)
        self.temporary_add_button = QPushButton("+")
        self.temporary_add_button.setFixedWidth(34)
        self.temporary_add_button.setToolTip("Add tags to the current image and keep them")
        self.temporary_add_button.clicked.connect(self._append_temporary_tags)
        temporary_layout = QHBoxLayout()
        temporary_layout.setContentsMargins(0, 0, 0, 0)
        temporary_layout.setSpacing(8)
        temporary_layout.addWidget(self.temporary_input, 1)
        temporary_layout.addWidget(self.temporary_add_button)
        self.fit_action = QAction("Fit to Window", self)
        self.fit_action.setCheckable(True)
        self.fit_action.setChecked(True)
        self.fit_action.toggled.connect(self.image_view.set_fit_to_window)
        self.actual_size_action = QAction("Original Size", self)
        self.actual_size_action.triggered.connect(self._actual_size)
        scale_buttons = QHBoxLayout()
        fit_button = QPushButton("Fit to Window")
        fit_button.setCheckable(True)
        fit_button.setChecked(True)
        fit_button.toggled.connect(self.fit_action.setChecked)
        original_button = QPushButton("Original Size")
        original_button.clicked.connect(self._actual_size)
        scale_buttons.addWidget(fit_button)
        scale_buttons.addWidget(original_button)
        scale_buttons.addStretch(1)
        self.fit_button = fit_button
        self.original_size_button = original_button

        self.back_button = QPushButton("Back")
        self.keep_button = QPushButton("Keep Tag")
        self.delete_button = QPushButton("Delete Tag")
        self.delete_button.setStyleSheet(
            "QPushButton { color: #b42318; } "
            "QPushButton:disabled { color: #d0d5dd; }"
        )
        self.next_button = QPushButton("Next")
        self.discard_button = QPushButton("Discard Changes")
        self.finish_button = QPushButton("Finish")
        self.confirm_button = self.keep_button
        self.back_button.clicked.connect(self._back)
        self.keep_button.clicked.connect(self._keep)
        self.delete_button.clicked.connect(self._delete)
        self.next_button.clicked.connect(self._keep)
        self.discard_button.clicked.connect(self._discard_and_close)
        self.finish_button.clicked.connect(self._finish)

        tag_panel_layout = QVBoxLayout()
        tag_panel_layout.setContentsMargins(12, 12, 12, 12)
        tag_panel_layout.setSpacing(10)
        tag_panel_layout.addWidget(QLabel("Current tag"))
        tag_panel_layout.addWidget(self.tag_label, 1)
        tag_panel_layout.addWidget(QLabel("Tag decisions"))
        tag_panel_layout.addWidget(self.tag_status_list)
        tag_panel_layout.addWidget(QLabel("Temporary tags for this image"))
        tag_panel_layout.addLayout(temporary_layout)
        tag_panel_layout.addLayout(scale_buttons)
        self.tag_panel = self._layout_widget(tag_panel_layout)

        decision_layout = QHBoxLayout()
        decision_layout.setContentsMargins(10, 8, 10, 8)
        decision_layout.setSpacing(8)
        decision_layout.addWidget(self.keep_button)
        decision_layout.addWidget(self.delete_button)
        decision_group = QGroupBox("Tag decision")
        decision_group.setLayout(decision_layout)

        navigation_layout = QHBoxLayout()
        navigation_layout.setContentsMargins(10, 8, 10, 8)
        navigation_layout.setSpacing(8)
        navigation_layout.addWidget(self.back_button)
        navigation_layout.addStretch(1)
        navigation_layout.addWidget(self.next_button)
        navigation_group = QGroupBox("Navigation")
        navigation_group.setLayout(navigation_layout)

        session_layout = QHBoxLayout()
        session_layout.setContentsMargins(10, 8, 10, 8)
        session_layout.setSpacing(8)
        session_layout.addStretch(1)
        session_layout.addWidget(self.discard_button)
        session_layout.addWidget(self.finish_button)
        session_group = QGroupBox("Review session")
        session_group.setLayout(session_layout)

        controls_layout = QVBoxLayout()
        controls_layout.setContentsMargins(12, 12, 12, 12)
        controls_layout.setSpacing(10)
        controls_layout.addWidget(decision_group)
        controls_layout.addWidget(navigation_group)
        controls_layout.addWidget(session_group)
        controls_layout.addStretch(1)
        self.controls_panel = self._layout_widget(controls_layout)

        self.right_splitter = QSplitter(Qt.Orientation.Vertical)
        self.right_splitter.addWidget(self.tag_panel)
        self.right_splitter.addWidget(self.controls_panel)
        self.right_splitter.setSizes([400, 260])
        self.right_splitter.setStretchFactor(0, 1)
        self.right_splitter.setStretchFactor(1, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.image_view)
        splitter.addWidget(self.right_splitter)
        splitter.setSizes([650, 330])

        self.review_widget = self._layout_widget(
            self._review_layout(self.progress_label, self.path_label, splitter)
        )
        self._root_layout = QVBoxLayout(self)
        self._root_layout.addWidget(self.folder_setup, 1)
        self._root_layout.addWidget(self.review_widget, 1)
        self.review_widget.hide()
        self._create_shortcuts()
        self.installEventFilter(self)
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)

        if root_directory is None:
            self.folder_setup.hide()
            self.review_widget.show()
            self._load_current()
        else:
            self._populate_tree(root_directory)
            self._update_selection()

    @staticmethod
    def _layout_widget(layout) -> QWidget:
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    @staticmethod
    def _review_layout(progress, path, splitter):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(progress)
        layout.addWidget(path)
        layout.addWidget(splitter, 1)
        return layout

    @property
    def session(self) -> ReviewSession:
        if self._session is None:
            raise RuntimeError("Review has not started.")
        return self._session

    def _populate_tree(self, root: Path) -> None:
        self.folder_tree.clear()
        root_item = QTreeWidgetItem([root.name or str(root)])
        self._make_checkable(root_item, root)
        self.folder_tree.addTopLevelItem(root_item)
        folders: dict[Path, QTreeWidgetItem] = {Path("."): root_item}
        relative_folders = {
            ancestor
            for entry in self._entries
            for ancestor in _folder_ancestors(entry.image_path.parent.relative_to(root))
        }
        for folder in sorted(relative_folders, key=lambda p: (len(p.parts), p.as_posix().casefold())):
            item = QTreeWidgetItem([folder.name])
            self._make_checkable(item, root / folder)
            folders[folder.parent].addChild(item)
            folders[folder] = item
        for entry in self._entries:
            if not entry.editable or not entry.tags:
                continue
            relative_parent = entry.image_path.parent.relative_to(root)
            item = QTreeWidgetItem([entry.image_path.name])
            self._make_checkable(item, entry.image_path)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, entry)
            folders[relative_parent].addChild(item)
        self.folder_tree.expandAll()
        root_item.setCheckState(0, Qt.CheckState.Checked)

    @staticmethod
    def _make_checkable(item: QTreeWidgetItem, path: Path) -> None:
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(0, Qt.ItemDataRole.UserRole, str(path))
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

    def _set_descendants(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for index in range(item.childCount()):
            child = item.child(index)
            if child is not None:
                child.setCheckState(0, state)
                self._set_descendants(child, state)

    def _update_ancestors(self, item: QTreeWidgetItem | None) -> None:
        while item is not None:
            states = [item.child(i).checkState(0) for i in range(item.childCount()) if item.child(i) is not None]
            if states and all(state == Qt.CheckState.Checked for state in states):
                item.setCheckState(0, Qt.CheckState.Checked)
            elif states and all(state == Qt.CheckState.Unchecked for state in states):
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
                entry = item.data(0, Qt.ItemDataRole.UserRole + 1)
                if isinstance(entry, ImageEntry):
                    checked.add(entry.image_path)
            iterator += 1
        return [entry for entry in self._entries if entry.image_path in checked]

    def _update_selection(self) -> None:
        count = len(self._checked_entries())
        self.folder_selection_label.setText(f"{count} image(s) will be reviewed.")
        self.start_button.setEnabled(count > 0)

    def _start_selected(self) -> None:
        entries = self._checked_entries()
        if not entries:
            return
        self._session = ReviewSession(entries)
        if not self.session.items:
            QMessageBox.information(self, "Nothing to Review", "The selected images have no editable tags.")
            return
        self._started = True
        for shortcut in self._shortcuts:
            shortcut.setEnabled(True)
        self.folder_setup.hide()
        self.review_widget.show()
        self._load_current()

    def _load_current(self) -> None:
        session = self.session
        self.temporary_input.clear()
        self._update_tag_status()
        if session.finished:
            self.progress_label.setText(
                "Review complete | "
                f"Reviewed tags {session.reviewed_tag_count} of "
                f"{session.total_tag_count}"
            )
            self.tag_label.setText("No more tags to review")
            self.image_view.clear_image("Review complete")
            self._update_buttons()
            return
        self.progress_label.setText(
            f"Image {session.current_index + 1} of {len(session.items)}"
            f" | Tag {session.current_tag_index + 1} of {len(session.current_tags)}"
            f" | Reviewed tags {session.reviewed_tag_count} of "
            f"{session.total_tag_count}"
        )
        self.path_label.setText(str(session.current_item.image_path))
        self.tag_label.setText(session.current_tag)
        self.image_view.clear_image("Loading image...")
        self.preview_loader.load(session.current_item.image_path)
        self._update_buttons()

    def _update_tag_status(self) -> None:
        session = self.session
        self.tag_status_list.clear()
        if not session.items:
            return
        reviewed = session.reviewed_tags[session.current_index]
        current_tags = set(session.current_tags)
        current_tag = session.current_tag
        current_row = -1
        for row, tag in enumerate(session.current_item.original_tags):
            if tag in reviewed:
                marker = "[kept]" if tag in current_tags else "[deleted]"
            else:
                marker = "[pending]"
            item = QListWidgetItem(f"{marker} {tag}")
            self.tag_status_list.addItem(item)
            if tag == current_tag:
                current_row = row
        original_tags = set(session.current_item.original_tags)
        for tag in session.current_tags:
            if tag not in original_tags:
                self.tag_status_list.addItem(QListWidgetItem(f"[kept] {tag}"))
        if current_row >= 0:
            self.tag_status_list.setCurrentRow(current_row)

    def _update_temporary_button(self, *_args) -> None:
        if self._session is None:
            self.temporary_add_button.setEnabled(False)
            return
        self.temporary_add_button.setEnabled(
            bool(parse_tags(self.temporary_input.text()))
            and not self.session.finished
        )

    def _append_temporary_tags(self) -> None:
        additions = self.session.add_kept_tags(parse_tags(self.temporary_input.text()))
        if not additions:
            return
        self.temporary_input.clear()
        self._update_tag_status()
        self._update_buttons()

    def _preview_loaded(self, image, error: str) -> None:
        if error or image.isNull():
            self.image_view.clear_image(f"Could not display image\n{error}")
        else:
            self.image_view.set_image(image)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._started and event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if key_event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                self._keep()
                return True
            if key_event.key() == Qt.Key.Key_Space:
                self._delete()
                return True
            if key_event.key() == Qt.Key.Key_Left:
                self._back()
                return True
            if key_event.key() == Qt.Key.Key_Right:
                self._keep()
                return True
        return super().eventFilter(watched, event)

    def _create_shortcuts(self) -> None:
        shortcuts = {
            "Return": self._keep,
            "Enter": self._keep,
            "Space": self._delete,
            "Left": self._back,
            "Right": self._keep,
        }
        for key, callback in shortcuts.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            shortcut.setEnabled(self._started)
            self._shortcuts.append(shortcut)

    def _actual_size(self) -> None:
        self.fit_action.setChecked(False)
        self.image_view.actual_size()
        self.fit_button.setChecked(False)

    def _keep(self) -> None:
        self.session.keep_current()
        self._load_current()

    def _delete(self) -> None:
        self.session.delete_current()
        self._load_current()

    def _back(self) -> None:
        if self.session.move_back():
            self._load_current()

    def _update_buttons(self) -> None:
        done = self.session.finished
        self.back_button.setEnabled(not self.session.at_first and not done)
        self.keep_button.setEnabled(not done)
        self.delete_button.setEnabled(not done)
        self.next_button.setEnabled(not done)
        self.finish_button.setEnabled(True)
        self._update_temporary_button()

    def _finish(self) -> None:
        changes = self.session.staged_changes()
        deleted_count = self.session.deleted_tag_count
        if deleted_count:
            answer = QMessageBox.question(
                self,
                "Confirm Tag Deletions",
                f"This review will delete {deleted_count} tag(s).\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not changes:
            self.commit_result = BatchCommitResult([], {})
            self._allow_close = True
            self.accept()
            return
        requests = [WriteRequest(item.tag_path, tags, item.source_bytes) for item, tags in changes]
        try:
            result = write_tags_batch(requests)
        except BatchPreflightError as exc:
            QMessageBox.critical(self, "Could Not Save", str(exc))
            return
        self.commit_result = result
        self._allow_close = True
        if result.failures:
            QMessageBox.warning(self, "Some Files Were Not Saved", "\n".join(f"{path.name}: {message}" for path, message in result.failures.items()))
        self.accept()

    def _discard_and_close(self) -> None:
        if not self._confirm_discard():
            return
        self._allow_close = True
        super().reject()

    def _confirm_discard(self) -> bool:
        if not self._started or not self.session.has_changes:
            return True
        answer = QMessageBox.question(
            self,
            "Discard Review Changes?",
            "The review has uncommitted changes. Discard them?",
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
