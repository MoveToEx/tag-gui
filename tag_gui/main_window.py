from __future__ import annotations

from pathlib import Path
from typing import cast, override

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QSettings,
    Qt,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QGuiApplication,
    QImageReader,
    QKeyEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QSplitter,
    QSizePolicy,
    QStyle,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .catalog import ImageCatalogModel
from .domain import (
    ImageEntry,
    TagOperation,
    apply_tag_operation,
    filter_traversal_entries,
    parse_requested_tags,
    tag_matches_pattern,
)
from .preview import ImageView, PreviewLoader
from .storage import ExternalChangeError, scan_folder, write_tags_atomic
from .traversal import TraversalDialog


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Image Tagger")
        self.resize(1200, 760)
        self.setAcceptDrops(True)
        self.settings = QSettings()
        self.directory: Path | None = None

        self.catalog = ImageCatalogModel(self)
        self.image_list = QListView()
        self.image_list.setModel(self.catalog)
        self.image_list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.image_list.setMinimumWidth(220)
        self.image_list.selectionModel().currentChanged.connect(
            self._current_image_changed
        )

        self.image_view = ImageView()
        self.preview_loader = PreviewLoader(self)
        self.preview_loader.loaded.connect(self._preview_loaded)

        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.tag_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tag_list.customContextMenuRequested.connect(
            self._show_tag_context_menu
        )
        self.tag_input = QLineEdit()
        self.tag_input.setPlaceholderText("Comma-separated tags")
        self.tag_input.returnPressed.connect(self._add_current_tags)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search tags (* wildcard)")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(180)
        self.search_input.setMaximumWidth(300)
        self.search_input.setToolTip(
            "Search tags in the opened folder. Use * as a wildcard. "
            "Press Enter for the next match or Shift+Enter for the previous match."
        )
        self.search_input.installEventFilter(self)

        add_button = QPushButton("Add")
        toggle_button = QPushButton("Toggle")
        delete_button = QPushButton("Delete Selected")
        add_button.clicked.connect(self._add_current_tags)
        toggle_button.clicked.connect(self._toggle_current_tags)
        delete_button.clicked.connect(self._delete_selected_tags)
        self.inline_buttons = [add_button, toggle_button, delete_button]

        input_buttons = QGridLayout()
        input_buttons.addWidget(add_button, 0, 0)
        input_buttons.addWidget(toggle_button, 0, 1)
        input_buttons.addWidget(delete_button, 1, 0, 1, 2)

        tag_panel = QWidget()
        tag_layout = QVBoxLayout(tag_panel)
        tag_layout.setContentsMargins(8, 0, 0, 0)
        tag_layout.addWidget(QLabel("Current tags"))
        tag_layout.addWidget(self.tag_list, 1)
        tag_layout.addWidget(self.tag_input)
        tag_layout.addLayout(input_buttons)
        tag_panel.setMinimumWidth(280)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.image_list)
        self.splitter.addWidget(self.image_view)
        self.splitter.addWidget(tag_panel)
        self.splitter.setSizes([250, 650, 300])
        self.setCentralWidget(self.splitter)

        self._create_actions()
        self._create_menus_and_toolbar()
        self._restore_settings()
        self._update_action_states()

    def _create_actions(self) -> None:
        self.open_action = QAction(
            "Open Folder...",
            self,
        )
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.open_folder)

        self.close_folder_action = QAction("Close Folder", self)
        self.close_folder_action.setShortcut(QKeySequence.StandardKey.Close)
        self.close_folder_action.triggered.connect(self.close_folder)

        self.rescan_action = QAction("Rescan", self)
        self.rescan_action.setShortcut(QKeySequence("F5"))
        self.rescan_action.triggered.connect(self.rescan)

        self.exit_action = QAction("Exit", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)

        self.focus_search_action = QAction("Find Tag", self)
        self.focus_search_action.setShortcut(QKeySequence.StandardKey.Find)
        self.focus_search_action.triggered.connect(self._focus_tag_search)

        self.first_action = QAction("First", self)
        self.previous_action = QAction(
            "Previous", self
        )
        self.next_action = QAction(
            "Next", self
        )
        self.last_action = QAction("Last", self)
        self.first_action.setShortcut(QKeySequence("Ctrl+Home"))
        self.previous_action.setShortcut(QKeySequence("PgUp"))
        self.next_action.setShortcut(QKeySequence("PgDown"))
        self.last_action.setShortcut(QKeySequence("Ctrl+End"))
        self.first_action.triggered.connect(lambda: self._select_row(0))
        self.previous_action.triggered.connect(lambda: self._move_selection(-1))
        self.next_action.triggered.connect(lambda: self._move_selection(1))
        self.last_action.triggered.connect(
            lambda: self._select_row(self.catalog.rowCount() - 1)
        )

        self.folder_tag_actions: dict[TagOperation, QAction] = {}
        labels = {
            TagOperation.ADD: "Add Tags...",
            TagOperation.DELETE: "Delete Tags...",
            TagOperation.TOGGLE: "Toggle Tags...",
            TagOperation.NORMALIZE: "Normalize Tags...",
        }
        for operation, label in labels.items():
            action = QAction(label, self)
            action.triggered.connect(
                lambda checked=False, op=operation: self._start_traversal(op)
            )
            self.folder_tag_actions[operation] = action

        self.fit_action = QAction("Fit to Window", self)
        self.fit_action.setCheckable(True)
        self.fit_action.setChecked(True)
        self.fit_action.setShortcut(QKeySequence("Ctrl+0"))
        self.fit_action.toggled.connect(self.image_view.set_fit_to_window)

        self.actual_size_action = QAction("Actual Size", self)
        self.actual_size_action.setShortcut(QKeySequence("Ctrl+1"))
        self.actual_size_action.triggered.connect(self._actual_size)

        self.zoom_in_action = QAction("Zoom In", self)
        self.zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self.zoom_in_action.triggered.connect(self._zoom_in)
        self.zoom_out_action = QAction("Zoom Out", self)
        self.zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.zoom_out_action.triggered.connect(self._zoom_out)

    def _create_menus_and_toolbar(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.close_folder_action)
        file_menu.addAction(self.rescan_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        navigate_menu = self.menuBar().addMenu("&Navigate")
        navigate_menu.addActions(
            [
                self.first_action,
                self.previous_action,
                self.next_action,
                self.last_action,
            ]
        )

        tags_menu = self.menuBar().addMenu("&Tags")
        tags_menu.addAction(self.focus_search_action)
        tags_menu.addSeparator()
        for operation in TagOperation:
            tags_menu.addAction(self.folder_tag_actions[operation])

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.fit_action)
        view_menu.addAction(self.actual_size_action)
        view_menu.addSeparator()
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)

        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        toolbar.addAction(self.open_action)
        toolbar.addSeparator()
        toolbar.addAction(self.previous_action)
        toolbar.addAction(self.next_action)
        toolbar.addSeparator()
        toolbar.addAction(self.fit_action)
        search_spacer = QWidget()
        search_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        toolbar.addWidget(search_spacer)
        toolbar.addWidget(self.search_input)
        self.addToolBar(toolbar)

    def _supported_extensions(self) -> set[str]:
        return {
            "."
            + bytes(image_format.data()).decode("ascii", errors="ignore").casefold()
            for image_format in QImageReader.supportedImageFormats()
        }

    def open_folder(self) -> None:
        start = str(self.settings.value("last_directory", "", type=str))
        selected = QFileDialog.getExistingDirectory(
            self, "Open Image Folder", start
        )
        if selected:
            self._load_directory(Path(selected), show_issues=True)

    def close_folder(self) -> None:
        if self.directory is None:
            return

        self.directory = None
        self.preview_loader.clear()
        self.image_list.clearSelection()
        self.image_list.setCurrentIndex(QModelIndex())
        self.catalog.set_entries([])
        self.image_view.clear_image("Open a folder to begin")
        self.tag_list.clear()
        self.tag_input.clear()
        self.search_input.clear()
        self.statusBar().clearMessage()
        self._update_action_states()

    def _dropped_directory(self, event) -> Path | None:
        if self.directory is not None or not event.mimeData().hasUrls():
            return None
        urls = event.mimeData().urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return None
        path = Path(urls[0].toLocalFile())
        return path if path.is_dir() else None

    @override
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._dropped_directory(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    @override
    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._dropped_directory(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    @override
    def dropEvent(self, event: QDropEvent) -> None:
        directory = self._dropped_directory(event)
        if directory is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._load_directory(directory, show_issues=True)

    def rescan(self) -> None:
        if self.directory is None:
            return
        current = self._current_entry()
        self._load_directory(
            self.directory,
            preferred_image=current.image_path if current else None,
            show_issues=True,
        )

    def _load_directory(
        self,
        directory: Path,
        preferred_image: Path | None = None,
        *,
        show_issues: bool,
    ) -> None:
        try:
            result = scan_folder(directory, self._supported_extensions())
        except OSError as exc:
            QMessageBox.critical(self, "Could Not Open Folder", str(exc))
            return

        self.directory = directory
        self.settings.setValue("last_directory", str(directory))
        self.catalog.set_entries(result.entries)

        row = (
            self.catalog.row_for_image(preferred_image)
            if preferred_image is not None
            else None
        )
        if row is None and result.entries:
            row = 0
        if row is not None:
            self._select_row(row)
        else:
            self.image_list.clearSelection()
            self.preview_loader.clear()
            self.image_view.clear_image("No supported images found")
            self.tag_list.clear()

        self._update_action_states()
        self.statusBar().showMessage(
            f"{directory} - {len(result.entries)} image(s), "
            f"{len(result.issues)} issue(s)"
        )
        if show_issues and result.issues:
            messages = [issue.message for issue in result.issues[:12]]
            if len(result.issues) > 12:
                messages.append(f"...and {len(result.issues) - 12} more issue(s).")
            QMessageBox.warning(
                self, "Folder Scan Issues", "\n".join(messages)
            )

    def _select_row(self, row: int) -> None:
        if not 0 <= row < self.catalog.rowCount():
            return
        index = self.catalog.index(row, 0)
        self.image_list.setCurrentIndex(index)
        self.image_list.selectionModel().select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.image_list.scrollTo(index)

    def _move_selection(self, offset: int) -> None:
        row = self.image_list.currentIndex().row()
        self._select_row(row + offset)

    def _current_entry(self) -> ImageEntry | None:
        return self.catalog.entry(self.image_list.currentIndex().row())

    def _focus_tag_search(self) -> None:
        self.search_input.setFocus()
        self.search_input.selectAll()

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.search_input and event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if key_event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                direction = (
                    -1
                    if key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                    else 1
                )
                self._find_tag(direction)
                return True
        return super().eventFilter(watched, event)

    def _find_tag(self, direction: int = 1) -> None:
        pattern = self.search_input.text().strip()
        if not pattern:
            self.statusBar().showMessage("Enter a tag pattern to search for.", 4000)
            return

        count = self.catalog.rowCount()
        if not count:
            self.statusBar().showMessage("Open a folder before searching tags.", 4000)
            return

        current_row = self.image_list.currentIndex().row()
        current_entry = self.catalog.entry(current_row)
        current_tag_row = self.tag_list.currentRow()
        selected_tag = self.tag_list.currentItem()
        continue_in_current = (
            current_entry is not None
            and 0 <= current_tag_row < len(current_entry.tags)
            and selected_tag is not None
            and selected_tag.text() == current_entry.tags[current_tag_row]
            and tag_matches_pattern(selected_tag.text(), pattern)
        )

        candidates: list[tuple[int, int]] = []

        def append_rows(offsets: range) -> None:
            for offset in offsets:
                row = (current_row + offset) % count
                entry = self.catalog.entry(row)
                if entry is not None:
                    tag_rows = (
                        range(len(entry.tags))
                        if direction > 0
                        else range(len(entry.tags) - 1, -1, -1)
                    )
                    candidates.extend((row, tag_row) for tag_row in tag_rows)

        if continue_in_current:
            if direction > 0:
                candidates.extend(
                    (current_row, tag_row)
                    for tag_row in range(
                        current_tag_row + 1, len(current_entry.tags)
                    )
                )
                append_rows(range(1, count))
                candidates.extend(
                    (current_row, tag_row)
                    for tag_row in range(0, current_tag_row + 1)
                )
            else:
                candidates.extend(
                    (current_row, tag_row)
                    for tag_row in range(current_tag_row - 1, -1, -1)
                )
                append_rows(range(-1, -count, -1))
                candidates.extend(
                    (current_row, tag_row)
                    for tag_row in range(
                        len(current_entry.tags) - 1, current_tag_row - 1, -1
                    )
                )
        else:
            append_rows(
                range(1, count + 1)
                if direction > 0
                else range(-1, -count - 1, -1)
            )

        for row, tag_row in candidates:
            entry = self.catalog.entry(row)
            if entry is None:
                continue
            matched_tag = entry.tags[tag_row]
            if tag_matches_pattern(matched_tag, pattern):
                self._select_row(row)
                self._select_current_tag(matched_tag)
                self.statusBar().showMessage(
                    f'Tag search "{pattern}" matched {matched_tag} in '
                    f"{entry.image_path.name}",
                    4000,
                )
                return

        self.statusBar().showMessage(
            f'No tags match "{pattern}" in the opened folder.', 4000
        )

    def _select_current_tag(self, tag: str) -> None:
        self.tag_list.clearSelection()
        for row in range(self.tag_list.count()):
            item = self.tag_list.item(row)
            if item.text() == tag:
                self.tag_list.setCurrentItem(item)
                item.setSelected(True)
                self.tag_list.scrollToItem(item)
                return

    def _current_image_changed(self, current, _previous) -> None:
        entry = self.catalog.entry(current.row())
        self.tag_list.clear()
        if entry is None:
            self.preview_loader.clear()
            self.image_view.clear_image()
            self._update_action_states()
            return

        self.tag_list.addItems(entry.tags)
        self.image_view.clear_image("Loading image...")
        self.preview_loader.load(entry.image_path)
        details = [
            f"{current.row() + 1}/{self.catalog.rowCount()}",
            entry.image_path.name,
            entry.tag_path.name,
        ]
        if entry.error:
            details.append(entry.error)
        elif entry.warnings:
            details.extend(entry.warnings)
        self.statusBar().showMessage(" | ".join(details))
        self._update_action_states()

    def _preview_loaded(self, image, error: str) -> None:
        if error or image.isNull():
            self.image_view.clear_image(f"Could not display image\n{error}")
        else:
            self.image_view.set_image(image)

    def _requested_from_input(self) -> list[str] | None:
        try:
            return parse_requested_tags(self.tag_input.text())
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 4000)
            self.tag_input.setFocus()
            return None

    def _add_current_tags(self) -> None:
        requested = self._requested_from_input()
        if requested is not None:
            self._apply_current_operation(TagOperation.ADD, requested)

    def _toggle_current_tags(self) -> None:
        requested = self._requested_from_input()
        if requested is not None:
            self._apply_current_operation(TagOperation.TOGGLE, requested)

    def _delete_selected_tags(self) -> None:
        requested = [item.text() for item in self.tag_list.selectedItems()]
        if not requested:
            self.statusBar().showMessage("Select one or more tags to delete.", 4000)
            return
        self._apply_current_operation(TagOperation.DELETE, requested)

    def _selected_tag_text(self) -> str:
        return ", ".join(
            self.tag_list.item(row).text()
            for row in range(self.tag_list.count())
            if self.tag_list.item(row).isSelected()
        )

    def _copy_selected_tags(self) -> None:
        selected = self._selected_tag_text()
        if not selected:
            return
        QGuiApplication.clipboard().setText(selected)
        self.statusBar().showMessage("Copied selected tags.", 3000)

    def _confirm_delete_selected_tags(self) -> None:
        selected_tags = [item.text() for item in self.tag_list.selectedItems()]
        if not selected_tags:
            return
        tags = ", ".join(selected_tags)
        answer = QMessageBox.question(
            self,
            "Delete Selected Tags?",
            f"Delete these tags from the current image?\n\n{tags}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._apply_current_operation(TagOperation.DELETE, selected_tags)

    def _show_tag_context_menu(self, position) -> None:
        item = self.tag_list.itemAt(position)
        if item is not None and not item.isSelected():
            self.tag_list.clearSelection()
            item.setSelected(True)

        copy_action = QAction("Copy Selected Tags", self)
        copy_action.setEnabled(bool(self._selected_tag_text()))
        copy_action.triggered.connect(self._copy_selected_tags)
        delete_action = QAction("Delete Selected Tags", self)
        delete_action.setEnabled(bool(self.tag_list.selectedItems()))
        delete_action.triggered.connect(self._confirm_delete_selected_tags)
        menu = QMenu(self)
        menu.addAction(copy_action)
        menu.addAction(delete_action)
        menu.exec(self.tag_list.viewport().mapToGlobal(position))

    def _apply_current_operation(
        self, operation: TagOperation, requested_tags: list[str]
    ) -> None:
        row = self.image_list.currentIndex().row()
        entry = self.catalog.entry(row)
        if entry is None or not entry.editable:
            return

        result = apply_tag_operation(entry.tags, requested_tags, operation)
        if result == entry.tags:
            self.statusBar().showMessage("No tag changes were needed.", 3000)
            return
        try:
            new_bytes = write_tags_atomic(
                entry.tag_path, result, expected_bytes=entry.source_bytes
            )
        except (OSError, ExternalChangeError) as exc:
            QMessageBox.critical(self, "Could Not Save Tags", str(exc))
            return

        entry.tags = result
        entry.source_bytes = new_bytes
        self.catalog.notify_entry_changed(row)
        self.tag_list.clear()
        self.tag_list.addItems(result)
        self.tag_input.clear()
        self.statusBar().showMessage(f"Saved {entry.tag_path.name}", 3000)

    def _start_traversal(self, operation: TagOperation) -> None:
        editable_entries = [entry for entry in self.catalog.entries if entry.editable]
        if not editable_entries:
            QMessageBox.information(
                self, "No Editable Images", "Open a folder with editable image tags first."
            )
            return

        requested: list[str] = []
        if operation != TagOperation.NORMALIZE:
            text, accepted = QInputDialog.getText(
                self,
                self.folder_tag_actions[operation].text().replace("...", ""),
                "Comma-separated tags:",
            )
            if not accepted:
                return
            try:
                requested = parse_requested_tags(text)
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid Tags", str(exc))
                return

        entries = filter_traversal_entries(editable_entries, operation, requested)
        if not entries:
            message = {
                TagOperation.ADD: "Every editable image already has all specified tags.",
                TagOperation.DELETE: "No editable image has all specified tags.",
                TagOperation.TOGGLE: "No editable images are available for toggling.",
                TagOperation.NORMALIZE: "No editable images are available for normalization.",
            }[operation]
            QMessageBox.information(self, "Nothing to Traverse", message)
            return

        current = self._current_entry()
        dialog = TraversalDialog(entries, operation, requested, self)
        if dialog.exec() == TraversalDialog.DialogCode.Accepted and self.directory:
            self._load_directory(
                self.directory,
                preferred_image=current.image_path if current else None,
                show_issues=False,
            )

    def _actual_size(self) -> None:
        self.fit_action.setChecked(False)
        self.image_view.actual_size()

    def _zoom_in(self) -> None:
        self.fit_action.setChecked(False)
        self.image_view.zoom_in()

    def _zoom_out(self) -> None:
        self.fit_action.setChecked(False)
        self.image_view.zoom_out()

    def _update_action_states(self) -> None:
        count = self.catalog.rowCount()
        row = self.image_list.currentIndex().row()
        has_current = 0 <= row < count
        current = self.catalog.entry(row)
        editable = current is not None and current.editable

        has_directory = self.directory is not None
        self.close_folder_action.setEnabled(has_directory)
        self.rescan_action.setEnabled(has_directory)
        self.search_input.setEnabled(count > 0)
        self.focus_search_action.setEnabled(count > 0)
        self.first_action.setEnabled(has_current and row > 0)
        self.previous_action.setEnabled(has_current and row > 0)
        self.next_action.setEnabled(has_current and row < count - 1)
        self.last_action.setEnabled(has_current and row < count - 1)
        self.tag_input.setEnabled(editable)
        self.tag_list.setEnabled(editable)
        for button in self.inline_buttons:
            button.setEnabled(editable)
        any_editable = any(entry.editable for entry in self.catalog.entries)
        for action in self.folder_tag_actions.values():
            action.setEnabled(any_editable)
        for action in [
            self.fit_action,
            self.actual_size_action,
            self.zoom_in_action,
            self.zoom_out_action,
        ]:
            action.setEnabled(has_current)

    def _restore_settings(self) -> None:
        geometry = self.settings.value("main_geometry")
        if geometry:
            self.restoreGeometry(geometry)
        splitter_state = self.settings.value("splitter_state")
        if splitter_state:
            self.splitter.restoreState(splitter_state)
        fit = cast(bool, self.settings.value("fit_to_window", True, type=bool))
        self.fit_action.setChecked(fit)
        self.image_view.set_fit_to_window(fit)

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings.setValue("main_geometry", self.saveGeometry())
        self.settings.setValue("splitter_state", self.splitter.saveState())
        self.settings.setValue("fit_to_window", self.fit_action.isChecked())
        super().closeEvent(event)
