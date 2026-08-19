from __future__ import annotations

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
    QVBoxLayout,
    QWidget,
)

from .domain import ImageEntry, TagOperation, TraversalSession, parse_tags
from .preview import ImageView, PreviewLoader
from .storage import (
    BatchCommitResult,
    BatchPreflightError,
    WriteRequest,
    write_tags_batch,
)


class TraversalDialog(QDialog):
    def __init__(
        self,
        entries: list[ImageEntry],
        operation: TagOperation,
        requested_tags: list[str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.session = TraversalSession(entries, operation, requested_tags or [])
        self.commit_result: BatchCommitResult | None = None
        self._allow_close = False
        self._populating_choices = False
        self._shortcuts: list[QShortcut] = []

        title = {
            TagOperation.ADD: "Add Tags Across Folder",
            TagOperation.DELETE: "Delete Tags Across Folder",
            TagOperation.TOGGLE: "Toggle Tags Across Folder",
            TagOperation.NORMALIZE: "Normalize Tags Across Folder",
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
        self.temporary_input.textChanged.connect(self._update_result_preview)
        detail_layout.addWidget(self.temporary_label)
        detail_layout.addWidget(self.temporary_input)

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
        self.skip_button = QPushButton("Skip & Next")
        self.apply_button = QPushButton("Apply & Next")
        self.finish_button = QPushButton("Finish")
        self.stop_button = QPushButton("Stop")
        self.back_button.clicked.connect(self._back)
        self.skip_button.clicked.connect(self._skip)
        self.apply_button.clicked.connect(self._apply)
        self.finish_button.clicked.connect(self._finish)
        self.stop_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.stop_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self.back_button)
        button_layout.addWidget(self.skip_button)
        button_layout.addWidget(self.apply_button)
        button_layout.addWidget(self.finish_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.path_label)
        layout.addWidget(self.shortcut_hint)
        layout.addWidget(splitter, 1)
        layout.addLayout(button_layout)

        self._create_shortcuts()
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
        if self.session.operation == TagOperation.NORMALIZE:
            self.choice_label.setText("Normalization")
            self.choices.hide()
        else:
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
        self._populating_choices = False
        self._update_result_preview()
        self._update_buttons()

    def _checked_tags(self) -> list[str]:
        return [
            self.choices.item(row).text()
            for row in range(self.choices.count())
            if self.choices.item(row).checkState() == Qt.CheckState.Checked
        ]

    def _temporary_tags(self) -> list[str]:
        if self.session.operation not in {TagOperation.ADD, TagOperation.DELETE}:
            return []
        return parse_tags(self.temporary_input.text())

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
        if self.apply_button.isEnabled():
            self._apply()
        else:
            self._skip()

    def _update_result_preview(self, *_args) -> None:
        if self._populating_choices:
            return
        selected = (
            []
            if self.session.operation == TagOperation.NORMALIZE
            else self._checked_tags()
        )
        extras = self._temporary_tags()
        prior = self.session.selections.get(self.session.current_index)
        staged = self.session.staged.get(self.session.current_index)
        if staged is not None and prior == tuple(selected) and not extras:
            result = list(staged)
        else:
            result = self.session.result_for(selected, extras)
        self.result_tags.setPlainText(", ".join(result) or "(none)")
        self.apply_button.setEnabled(
            self.session.operation == TagOperation.NORMALIZE
            or bool(selected)
            or bool(extras)
        )
        if self.session.operation != TagOperation.NORMALIZE:
            if prior is not None and tuple(selected) == prior and not extras:
                self.session.reviewed.add(self.session.current_index)
            elif prior is not None:
                self.session.reviewed.discard(self.session.current_index)
            self._update_buttons()

    def _update_buttons(self) -> None:
        at_last = self.session.at_last
        self.back_button.setEnabled(not self.session.at_first)
        self.apply_button.setText("Apply" if at_last else "Apply & Next")
        self.skip_button.setText("Skip" if at_last else "Skip & Next")
        self.finish_button.setEnabled(
            at_last and self.session.current_index in self.session.reviewed
        )

    def _apply(self) -> None:
        self.session.apply_current(self._checked_tags(), self._temporary_tags())
        if self.session.move_next():
            self._load_current()
        else:
            self._update_buttons()

    def _skip(self) -> None:
        self.session.skip_current()
        if self.session.move_next():
            self._load_current()
        else:
            self._update_buttons()

    def _back(self) -> None:
        if self.session.move_back():
            self._load_current()

    def _finish(self) -> None:
        if self.session.current_index not in self.session.reviewed:
            return
        changes = self.session.staged_changes()
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
