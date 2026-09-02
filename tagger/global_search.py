from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .domain import ImageEntry, matching_tag_counts
from .tag_library import TagLibrary, attach_tag_completer


class GlobalTagSearchDialog(QDialog):
    def __init__(
        self,
        entries: Sequence[ImageEntry],
        parent: QWidget | None = None,
        *,
        tag_library: TagLibrary | None = None,
    ) -> None:
        super().__init__(parent)
        self.entries = list(entries)
        self.setWindowTitle("Global Tag Search")
        self.resize(520, 480)

        self.pattern_input = QLineEdit()
        self.pattern_input.setPlaceholderText("Exact tag or * wildcard pattern")
        self.pattern_input.setClearButtonEnabled(True)
        self.pattern_input.setToolTip(
            "Matching is case-sensitive. Use * to match any sequence of characters."
        )
        self.pattern_completer = attach_tag_completer(
            self.pattern_input, tag_library
        )
        self.search_button = QPushButton("Search")
        self.pattern_input.returnPressed.connect(self.search)
        self.search_button.clicked.connect(self.search)

        search_layout = QHBoxLayout()
        search_layout.addWidget(self.pattern_input, 1)
        search_layout.addWidget(self.search_button)

        self.count_label = QLabel("Enter a tag pattern to search.")
        self.count_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.results = QTableWidget(0, 2)
        self.results.setHorizontalHeaderLabels(["Tag name", "Tag count"])
        self.results.horizontalHeader().setStretchLastSection(False)
        self.results.horizontalHeader().setSectionResizeMode(
            0, self.results.horizontalHeader().ResizeMode.Stretch
        )
        self.results.horizontalHeader().setSectionResizeMode(
            1, self.results.horizontalHeader().ResizeMode.ResizeToContents
        )
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.results.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(
            self._show_results_context_menu
        )

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(search_layout)
        layout.addWidget(self.count_label)
        layout.addWidget(self.results, 1)
        layout.addWidget(buttons)

        self.pattern_input.setFocus()

    def search(self) -> None:
        pattern = self.pattern_input.text().strip()
        self.results.setRowCount(0)
        if not pattern:
            self.count_label.setText("Enter a tag pattern to search.")
            return

        matches = matching_tag_counts(self.entries, pattern)
        occurrence_count = sum(count for _tag, count in matches)
        self.count_label.setText(
            f"{len(matches)} matching tag(s) across "
            f"{occurrence_count} image occurrence(s)."
        )
        self.results.setRowCount(len(matches))
        for row, (tag, count) in enumerate(matches):
            self.results.setItem(row, 0, QTableWidgetItem(tag))
            count_item = QTableWidgetItem(str(count))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.results.setItem(row, 1, count_item)

    def _selected_tags(self) -> list[str]:
        rows = sorted({index.row() for index in self.results.selectedIndexes()})
        tags: list[str] = []
        for row in rows:
            item = self.results.item(row, 0)
            if item is not None:
                tags.append(item.text())
        return tags

    def _copy_selected_tags(self) -> None:
        tags = self._selected_tags()
        if tags:
            QGuiApplication.clipboard().setText(", ".join(tags))

    def _show_results_context_menu(self, position) -> None:
        index = self.results.indexAt(position)
        if index.isValid() and index.row() not in {
            selected.row() for selected in self.results.selectedIndexes()
        }:
            self.results.clearSelection()
            self.results.selectRow(index.row())
        copy_action = QAction("Copy Selected Tags", self)
        copy_action.setEnabled(bool(self._selected_tags()))
        copy_action.triggered.connect(self._copy_selected_tags)
        menu = QMenu(self)
        menu.addAction(copy_action)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.results.viewport().mapToGlobal(position))
