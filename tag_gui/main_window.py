from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import cast, override

from PySide6.QtCore import (
    QEvent,
    QFile,
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
    QImage,
    QImageReader,
    QKeyEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QSplitter,
    QSizePolicy,
    QStyle,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .archive import ArchiveProgressDialog
from .ai_tagger import (
    AITaggingDialog,
    ModelManagementDialog,
    ai_dependencies_available,
    missing_ai_dependencies,
)
from .bulk_operation import BulkOperationDialog
from .catalog import ImageCatalogModel
from .complex_filter import ComplexFilterDialog
from .domain import (
    ImageEntry,
    TagOperation,
    apply_tag_operation,
    normalize_tags,
    parse_requested_tags,
    tag_matches_pattern,
)
from .global_search import GlobalTagSearchDialog
from .preview import ImageView, PreviewLoader
from .review import ReviewDialog
from .storage import (
    BatchPreflightError,
    ExternalChangeError,
    WriteRequest,
    scan_folder,
    write_tags_atomic,
    write_tags_batch,
)
from .tag_library import (
    DownloadTagsDialog,
    TagLibrary,
    attach_tag_completer,
)
from .traversal import TraversalDialog


def move_to_trash(path: Path) -> bool:
    trash_file = QFile(str(path))
    move = cast(Callable[[], bool], trash_file.moveToTrash)
    return move()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Image Tagger")
        self.resize(1200, 760)
        self.setAcceptDrops(True)
        self.settings = QSettings()
        self.directory: Path | None = None
        self._complex_filter_dialog: ComplexFilterDialog | None = None
        self._archive_dialog: ArchiveProgressDialog | None = None
        self._archive_destination: Path | None = None
        self.tag_library = TagLibrary(parent=self)

        self.catalog = ImageCatalogModel(self)
        self.image_list = QTreeView()
        self.image_list.setModel(self.catalog)
        self.image_list.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        self.image_list.setMinimumWidth(220)
        self.image_list.setHeaderHidden(True)
        self.image_list.setUniformRowHeights(True)
        self.image_list.setAnimated(True)
        self.image_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.image_list.customContextMenuRequested.connect(
            self._show_image_context_menu
        )
        self.image_list.selectionModel().currentChanged.connect(
            self._current_image_changed
        )

        self.image_view = ImageView()
        self.preview_loader = PreviewLoader(self)
        self.preview_loader.loaded.connect(self._preview_loaded)
        self.image_info_label = QLabel("No image selected")
        self.image_info_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.image_info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.image_info_label.setContentsMargins(10, 6, 10, 6)
        self.image_info_label.setStyleSheet(
            "QLabel { background: #e4e7ec; color: #344054; "
            "border-top: 1px solid #d0d5dd; }"
        )

        image_panel = QWidget()
        image_layout = QVBoxLayout(image_panel)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(0)
        image_layout.addWidget(self.image_view, 1)
        image_layout.addWidget(self.image_info_label)

        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.tag_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tag_list.customContextMenuRequested.connect(
            self._show_tag_context_menu
        )
        self.tag_input = QLineEdit()
        self.tag_input.setPlaceholderText("Comma-separated tags")
        self.tag_input.setClearButtonEnabled(True)
        self.tag_input.returnPressed.connect(self._add_current_tags)
        self.tag_completer = attach_tag_completer(
            self.tag_input, self.tag_library
        )

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search tags (* wildcard)")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(180)
        self.search_input.setMaximumWidth(300)
        self.search_input.setToolTip(
            "Search tags in the opened folder. Use * as a wildcard. "
            "Press Enter for the next match or Shift+Enter for the previous match."
        )
        self.search_completer = attach_tag_completer(
            self.search_input, self.tag_library
        )
        self.search_input.installEventFilter(self)

        self.add_tag_button = QPushButton("+")
        self.add_tag_button.setToolTip("Add these tags to the current image")
        self.add_tag_button.setFixedWidth(34)
        self.delete_tag_button = QPushButton("Delete Selected")
        self.add_tag_button.clicked.connect(self._add_current_tags)
        self.delete_tag_button.clicked.connect(self._delete_selected_tags)
        self.inline_buttons = [self.add_tag_button, self.delete_tag_button]

        input_buttons = QHBoxLayout()
        input_buttons.setContentsMargins(0, 0, 0, 0)
        input_buttons.addWidget(self.tag_input, 1)
        input_buttons.addWidget(self.add_tag_button)

        tag_panel = QWidget()
        tag_layout = QVBoxLayout(tag_panel)
        tag_layout.setContentsMargins(8, 0, 0, 0)
        tag_layout.addWidget(QLabel("Current tags"))
        tag_layout.addWidget(self.tag_list, 1)
        tag_layout.addLayout(input_buttons)
        tag_layout.addWidget(self.delete_tag_button)
        tag_panel.setMinimumWidth(280)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.image_list)
        self.splitter.addWidget(image_panel)
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

        self.archive_action = QAction("Archive...", self)
        self.archive_action.triggered.connect(self._archive_folder)

        self.exit_action = QAction("Exit", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)

        self.global_search_action = QAction("Global Tag Search...", self)
        self.global_search_action.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self.global_search_action.triggered.connect(self._open_global_search)

        self.review_action = QAction("Review Tags...", self)
        self.review_action.setShortcut(QKeySequence("Ctrl+R"))
        self.review_action.triggered.connect(self._open_review)

        self.complex_filter_action = QAction("Complex Filter...", self)
        self.complex_filter_action.setShortcut(QKeySequence("Ctrl+Alt+F"))
        self.complex_filter_action.triggered.connect(self._open_complex_filter)

        self.bulk_operation_action = QAction("Bulk Operation...", self)
        self.bulk_operation_action.triggered.connect(self._open_bulk_operation)

        self.download_tags_action = QAction("Manage Tag Library...", self)
        self.download_tags_action.triggered.connect(self._download_tags)

        self.model_management_action = QAction("Manage Tagging Model...", self)
        self.model_management_action.triggered.connect(
            self._open_model_management
        )

        self.ai_tagging_action = QAction("AI Tagging...", self)
        self.ai_tagging_action.triggered.connect(self._open_ai_tagging)
        missing = missing_ai_dependencies()
        if missing:
            message = "Install the ai-tagger dependency group: " + ", ".join(missing)
            self.model_management_action.setToolTip(message)
            self.ai_tagging_action.setToolTip(message)

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
        self.first_action.triggered.connect(
            lambda: self._select_optional_row(self.catalog.first_image_row())
        )
        self.previous_action.triggered.connect(lambda: self._move_selection(-1))
        self.next_action.triggered.connect(lambda: self._move_selection(1))
        self.last_action.triggered.connect(
            lambda: self._select_optional_row(self.catalog.last_image_row())
        )

        self.folder_tag_actions: dict[TagOperation, QAction] = {}
        labels = {
            TagOperation.ADD: "Add Tags...",
            TagOperation.DELETE: "Delete Tags...",
            TagOperation.TOGGLE: "Toggle Tags...",
        }
        for operation, label in labels.items():
            action = QAction(label, self)
            action.triggered.connect(
                lambda checked=False, op=operation: self._start_traversal(op)
            )
            self.folder_tag_actions[operation] = action

        self.normalize_action = QAction("Normalize All Tags...", self)
        self.normalize_action.triggered.connect(self._normalize_all_tags)

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
        file_menu.addAction(self.archive_action)
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
        tags_menu.addAction(self.global_search_action)
        tags_menu.addAction(self.review_action)
        tags_menu.addAction(self.complex_filter_action)
        tags_menu.addAction(self.bulk_operation_action)
        tags_menu.addSeparator()
        tags_menu.addAction(self.ai_tagging_action)
        tags_menu.addSeparator()
        for action in self.folder_tag_actions.values():
            tags_menu.addAction(action)
        tags_menu.addSeparator()
        tags_menu.addAction(self.normalize_action)

        resources_menu = self.menuBar().addMenu("&Resources")
        resources_menu.addAction(self.download_tags_action)
        resources_menu.addAction(self.model_management_action)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.fit_action)
        view_menu.addAction(self.actual_size_action)
        view_menu.addSeparator()
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)

        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
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
        self.tag_library.clear_folder_tags()
        self.image_view.clear_image("Open a folder to begin")
        self._set_image_info(None)
        self.tag_list.clear()
        self.tag_input.clear()
        self.search_input.clear()
        self.statusBar().clearMessage()
        self._update_window_title()
        self._update_action_states()

    def _dropped_directory(self, event) -> Path | None:
        if not event.mimeData().hasUrls():
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
        self.close_folder()
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

    def _archive_folder(self) -> None:
        if not self.catalog.entries:
            return
        directory = self.directory
        start = (
            directory / f"{directory.name}.zip"
            if directory is not None
            else Path("images.zip")
        )
        selected, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Archive Image and Tag Pairs",
            str(start),
            "Zip archives (*.zip)",
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.suffix.casefold() != ".zip":
            destination = Path(f"{destination}.zip")
        dialog = ArchiveProgressDialog(
            list(self.catalog.entries), destination, self
        )
        dialog.completed.connect(self._archive_completed)
        dialog.failed.connect(self._archive_failed)
        self._archive_dialog = dialog
        self._archive_destination = destination
        self._update_action_states()
        dialog.show()
        dialog.start()

    def _archive_completed(self, archived_count: int) -> None:
        destination = self._archive_destination
        self._archive_dialog = None
        self._archive_destination = None
        self._update_action_states()
        self.statusBar().showMessage(
            f"Archived {archived_count} image/tag pair(s) to {destination}.",
            5000,
        )

    def _archive_failed(self, message: str) -> None:
        self._archive_dialog = None
        self._archive_destination = None
        self._update_action_states()
        QMessageBox.critical(self, "Could Not Create Archive", message)

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
        self._update_window_title()
        self.settings.setValue("last_directory", str(directory))
        self.catalog.set_entries(result.entries, directory)
        self.tag_library.set_folder_entries(result.entries)
        self.image_list.expandAll()

        row = (
            self.catalog.row_for_image(preferred_image)
            if preferred_image is not None
            else None
        )
        if row is None and result.entries:
            row = self.catalog.first_image_row()
        if row is not None:
            self._select_row(row)
        else:
            self.image_list.clearSelection()
            self.preview_loader.clear()
            self.image_view.clear_image("No supported images found")
            self._set_image_info(None)
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
        if self.catalog.entry(row) is None:
            return
        index = self.catalog.index_for_row(row)
        if not index.isValid():
            return
        parent = index.parent()
        while parent.isValid():
            self.image_list.expand(parent)
            parent = parent.parent()
        self.image_list.setCurrentIndex(index)
        self.image_list.selectionModel().select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.image_list.scrollTo(index)

    def _select_optional_row(self, row: int | None) -> None:
        if row is not None:
            self._select_row(row)

    def _move_selection(self, offset: int) -> None:
        row = self.catalog.row_for_index(self.image_list.currentIndex())
        if row is None:
            return
        target = (
            self.catalog.next_image_row(row)
            if offset > 0
            else self.catalog.previous_image_row(row)
        )
        self._select_optional_row(target)

    def _current_entry(self) -> ImageEntry | None:
        return self.catalog.entry_for_index(self.image_list.currentIndex())

    def _show_image_context_menu(self, position) -> None:
        index = self.image_list.indexAt(position)
        entry = self.catalog.entry_for_index(index)
        if entry is None:
            return

        self.image_list.setCurrentIndex(index)
        self.image_list.selectionModel().select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        delete_action = QAction("Delete Image and Tag...", self)
        delete_action.triggered.connect(self._delete_current_image_and_tag)
        menu = QMenu(self)
        menu.addAction(delete_action)
        menu.exec(self.image_list.viewport().mapToGlobal(position))

    def _delete_current_image_and_tag(self) -> None:
        row = self.catalog.row_for_index(self.image_list.currentIndex())
        entry = self.catalog.entry(row) if row is not None else None
        if row is None or entry is None or self.directory is None:
            return

        answer = QMessageBox.question(
            self,
            "Delete Image and Tag?",
            "Move both files to the Trash?\n\n"
            f"Image: {entry.image_path.name}\n"
            f"Tag: {entry.tag_path.name}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        preferred = self.catalog.entry(row + 1)
        if preferred is None:
            preferred = self.catalog.entry(row - 1)
        preferred_path = preferred.image_path if preferred is not None else None

        self.preview_loader.clear()
        self.preview_loader.wait_for_done()
        moved: list[Path] = []
        failures: list[str] = []
        image_moved = move_to_trash(entry.image_path)
        if image_moved:
            moved.append(entry.image_path)
            if entry.tag_path.exists():
                tag_moved = move_to_trash(entry.tag_path)
                if tag_moved:
                    moved.append(entry.tag_path)
                else:
                    failures.append(f"Could not move {entry.tag_path.name} to Trash.")
        else:
            failures.append(f"Could not move {entry.image_path.name} to Trash.")

        if moved:
            self._load_directory(
                self.directory,
                preferred_image=preferred_path,
                show_issues=False,
            )
        if failures:
            QMessageBox.warning(
                self,
                "Could Not Delete All Files",
                "\n".join(failures),
            )
        elif moved:
            self.statusBar().showMessage(
                f"Moved {entry.image_path.name} and {entry.tag_path.name} to Trash.",
                4000,
            )

    def _focus_tag_search(self) -> None:
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _open_global_search(self) -> None:
        if not self.catalog.entries:
            return
        GlobalTagSearchDialog(
            self.catalog.entries,
            self,
            tag_library=self.tag_library,
        ).exec()

    def _open_review(self) -> None:
        if not self.catalog.entries or self.directory is None:
            return
        dialog = ReviewDialog(
            self.catalog.entries,
            self,
            root_directory=self.directory,
            tag_library=self.tag_library,
        )
        if dialog.exec() == ReviewDialog.DialogCode.Accepted and dialog.commit_result:
            current = self._current_entry()
            self._load_directory(
                self.directory,
                preferred_image=current.image_path if current else None,
                show_issues=False,
            )

    def _open_complex_filter(self) -> None:
        if not self.catalog.entries:
            return
        if self._complex_filter_dialog is not None:
            self._complex_filter_dialog.show()
            self._complex_filter_dialog.raise_()
            self._complex_filter_dialog.activateWindow()
            return

        dialog = ComplexFilterDialog(
            self.catalog.entries,
            self,
            root_directory=self.directory,
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowFlag(Qt.WindowType.Window, True)
        dialog.setModal(False)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.image_activated.connect(self._select_image_from_complex_filter)
        dialog.destroyed.connect(self._complex_filter_destroyed)
        self._complex_filter_dialog = dialog
        dialog.show()

    def _open_bulk_operation(self) -> None:
        if self.directory is None:
            return
        editable_entries = [
            entry for entry in self.catalog.entries if entry.editable
        ]
        if not editable_entries:
            return
        current = self._current_entry()
        dialog = BulkOperationDialog(
            editable_entries,
            self,
            root_directory=self.directory,
            tag_library=self.tag_library,
        )
        if (
            dialog.exec() == BulkOperationDialog.DialogCode.Accepted
            and self.directory is not None
        ):
            self._load_directory(
                self.directory,
                preferred_image=current.image_path if current else None,
                show_issues=False,
            )

    def _download_tags(self) -> None:
        dialog = DownloadTagsDialog(self)
        dialog.library_changed.connect(
            lambda _path: self.tag_library.reload_danbooru()
        )
        dialog.exec()

    def _open_model_management(self) -> None:
        if ai_dependencies_available():
            ModelManagementDialog(self).exec()

    def _open_ai_tagging(self) -> None:
        if not ai_dependencies_available() or self.directory is None:
            return
        editable_entries = [
            entry for entry in self.catalog.entries if entry.editable
        ]
        if not editable_entries:
            return
        current = self._current_entry()
        dialog = AITaggingDialog(
            editable_entries,
            self,
            root_directory=self.directory,
        )
        if dialog.exec() == AITaggingDialog.DialogCode.Accepted:
            self._load_directory(
                self.directory,
                preferred_image=current.image_path if current else None,
                show_issues=False,
            )

    def _complex_filter_destroyed(self, _object: QObject) -> None:
        self._complex_filter_dialog = None

    def _select_image_from_complex_filter(self, image_path: Path) -> None:
        row = self.catalog.row_for_image(image_path)
        self._select_optional_row(row)

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

        count = self.catalog.image_count
        if not count:
            self.statusBar().showMessage("Open a folder before searching tags.", 4000)
            return

        current_row = self.catalog.row_for_index(self.image_list.currentIndex())
        if current_row is None:
            current_row = -1
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
        entry = self.catalog.entry_for_index(current)
        self.tag_list.clear()
        if entry is None:
            self.preview_loader.clear()
            self.image_view.clear_image()
            self._set_image_info(None)
            self._update_action_states()
            return

        row = self.catalog.row_for_index(current)
        if row is None:
            return
        self.tag_list.addItems(entry.tags)
        self.image_view.clear_image("Loading image...")
        self._set_image_info(entry)
        self.preview_loader.load(entry.image_path)
        details = [
            f"{self.catalog.image_position(row)}/{self.catalog.image_count}",
            entry.image_path.name,
            entry.tag_path.name,
        ]
        if entry.error:
            details.append(entry.error)
        elif entry.warnings:
            details.extend(entry.warnings)
        self.statusBar().showMessage(" | ".join(details))
        self._update_action_states()

    def _preview_loaded(self, image: QImage, error: str) -> None:
        entry = self._current_entry()
        if error or image.isNull():
            self.image_view.clear_image(f"Could not display image\n{error}")
            self._set_image_info(entry, error=error or "Could not read image")
        else:
            self.image_view.set_image(image)
            self._set_image_info(entry, image)

    def _update_window_title(self) -> None:
        title = "Image Tagger"
        if self.directory is not None:
            folder_name = self.directory.name or str(self.directory)
            title = f"{folder_name} - {title}"
        self.setWindowTitle(title)

    def _set_image_info(
        self,
        entry: ImageEntry | None,
        image: QImage | None = None,
        *,
        error: str = "",
    ) -> None:
        if entry is None:
            self.image_info_label.setText("No image selected")
            self.image_info_label.setToolTip("")
            return

        display_path = entry.image_path.name
        if self.directory is not None:
            try:
                display_path = str(entry.image_path.relative_to(self.directory))
            except ValueError:
                pass

        details = [display_path]
        if image is not None and not image.isNull():
            details.append(f"{image.width()} × {image.height()} px")
        elif error:
            details.append("Dimensions unavailable")
        else:
            details.append("Loading dimensions...")

        image_format = entry.image_path.suffix.removeprefix(".").upper()
        if image_format:
            details.append(image_format)
        try:
            details.append(self._format_file_size(entry.image_path.stat().st_size))
        except OSError:
            details.append("Size unavailable")
        if error:
            details.append(error)

        self.image_info_label.setText(" | ".join(details))
        self.image_info_label.setToolTip(str(entry.image_path))

    @staticmethod
    def _format_file_size(size: int) -> str:
        value = float(size)
        units = ("B", "KB", "MB", "GB", "TB")
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

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

    def _delete_selected_tags(self) -> None:
        requested = [item.text() for item in self.tag_list.selectedItems()]
        if not requested:
            self.statusBar().showMessage("Select one or more tags to delete.", 4000)
            return
        entry = self._current_entry()
        if entry is None or not entry.editable:
            return
        answer = QMessageBox.question(
            self,
            "Delete Selected Tags?",
            f"Delete {len(requested)} selected tag(s) from "
            f"{entry.image_path.name}?\n\n{', '.join(requested)}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
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

    def _show_tag_context_menu(self, position) -> None:
        item = self.tag_list.itemAt(position)
        if item is not None and not item.isSelected():
            self.tag_list.clearSelection()
            item.setSelected(True)

        menu = self._create_tag_context_menu()
        menu.exec(self.tag_list.viewport().mapToGlobal(position))

    def _create_tag_context_menu(self) -> QMenu:
        copy_action = QAction("Copy Selected Tags", self)
        copy_action.setEnabled(bool(self._selected_tag_text()))
        copy_action.triggered.connect(self._copy_selected_tags)
        delete_action = QAction("Delete Selected Tags", self)
        delete_action.setEnabled(bool(self.tag_list.selectedItems()))
        delete_action.triggered.connect(self._delete_selected_tags)
        menu = QMenu(self)
        menu.addAction(copy_action)
        menu.addAction(delete_action)
        return menu

    def _apply_current_operation(
        self, operation: TagOperation, requested_tags: list[str]
    ) -> None:
        row = self.catalog.row_for_index(self.image_list.currentIndex())
        entry = self.catalog.entry(row) if row is not None else None
        if row is None or entry is None or not entry.editable:
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
        if operation == TagOperation.NORMALIZE:
            raise ValueError("Normalization is not a traversal operation.")
        editable_entries = [entry for entry in self.catalog.entries if entry.editable]
        if not editable_entries:
            QMessageBox.information(
                self, "No Editable Images", "Open a folder with editable image tags first."
            )
            return

        current = self._current_entry()
        dialog = TraversalDialog(
            editable_entries,
            operation,
            parent=self,
            root_directory=self.directory,
            tag_library=self.tag_library,
        )
        if dialog.exec() == TraversalDialog.DialogCode.Accepted and self.directory:
            self._load_directory(
                self.directory,
                preferred_image=current.image_path if current else None,
                show_issues=False,
            )

    def _normalize_all_tags(self) -> None:
        editable_entries = [entry for entry in self.catalog.entries if entry.editable]
        if not editable_entries:
            QMessageBox.information(
                self,
                "No Editable Images",
                "Open a folder with editable image tags first.",
            )
            return

        changes = [
            (entry, normalized)
            for entry in editable_entries
            if (normalized := normalize_tags(entry.tags)) != entry.tags
        ]
        if not changes:
            QMessageBox.information(
                self,
                "Tags Already Normalized",
                "All editable image tags are already normalized.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Normalize All Tags?",
            "This will normalize tags on all "
            f"{len(editable_entries)} editable image(s) in the open folder.\n\n"
            f"{len(changes)} sidecar file(s) will change.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        requests = [
            WriteRequest(
                path=entry.tag_path,
                tags=normalized,
                expected_bytes=entry.source_bytes,
            )
            for entry, normalized in changes
        ]
        try:
            result = write_tags_batch(requests)
        except BatchPreflightError as exc:
            QMessageBox.critical(self, "Could Not Normalize Tags", str(exc))
            return

        current = self._current_entry()
        if self.directory is not None:
            self._load_directory(
                self.directory,
                preferred_image=current.image_path if current else None,
                show_issues=False,
            )
        if result.failures:
            details = "\n".join(
                f"{path.name}: {message}"
                for path, message in result.failures.items()
            )
            QMessageBox.warning(
                self,
                "Some Files Were Not Normalized",
                f"Normalized {len(result.succeeded)} file(s).\n\n{details}",
            )
        else:
            self.statusBar().showMessage(
                f"Normalized tags in {len(result.succeeded)} file(s).", 4000
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
        count = self.catalog.image_count
        row = self.catalog.row_for_index(self.image_list.currentIndex())
        has_current = row is not None
        current = self.catalog.entry(row) if row is not None else None
        editable = current is not None and current.editable

        has_directory = self.directory is not None
        self.close_folder_action.setEnabled(has_directory)
        self.rescan_action.setEnabled(has_directory)
        self.archive_action.setEnabled(
            count > 0 and self._archive_dialog is None
        )
        self.search_input.setEnabled(count > 0)
        self.global_search_action.setEnabled(count > 0)
        self.review_action.setEnabled(count > 0 and any(entry.editable and entry.tags for entry in self.catalog.entries))
        self.complex_filter_action.setEnabled(count > 0)
        has_previous = (
            row is not None and self.catalog.previous_image_row(row) is not None
        )
        has_next = row is not None and self.catalog.next_image_row(row) is not None
        self.first_action.setEnabled(has_previous)
        self.previous_action.setEnabled(has_previous)
        self.next_action.setEnabled(has_next)
        self.last_action.setEnabled(has_next)
        self.tag_input.setEnabled(editable)
        self.tag_list.setEnabled(editable)
        for button in self.inline_buttons:
            button.setEnabled(editable)
        any_editable = any(entry.editable for entry in self.catalog.entries)
        self.bulk_operation_action.setEnabled(any_editable)
        ai_available = ai_dependencies_available()
        self.model_management_action.setEnabled(ai_available)
        self.ai_tagging_action.setEnabled(ai_available and any_editable)
        for action in self.folder_tag_actions.values():
            action.setEnabled(any_editable)
        self.normalize_action.setEnabled(any_editable)
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
