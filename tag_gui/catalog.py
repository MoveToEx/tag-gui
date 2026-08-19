from __future__ import annotations

from pathlib import Path
from typing import override

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QColor

from .domain import ImageEntry


class ImageCatalogModel(QAbstractListModel):
    EntryRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entries: list[ImageEntry] = []

    @override
    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:
        return 0 if parent.isValid() else len(self.entries)

    @override
    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            suffix = " [read-only]" if not entry.editable else ""
            return f"{entry.image_path.name}  ({len(entry.tags)}){suffix}"
        if role == Qt.ItemDataRole.ToolTipRole:
            details = [
                str(entry.image_path),
                f"Tags: {entry.tag_path.name}",
            ]
            details.extend(entry.warnings)
            if entry.error:
                details.append(entry.error)
            return "\n".join(details)
        if role == Qt.ItemDataRole.ForegroundRole and entry.error:
            return QColor("#b42318")
        if role == self.EntryRole:
            return entry
        return None

    def set_entries(self, entries: list[ImageEntry]) -> None:
        self.beginResetModel()
        self.entries = entries
        self.endResetModel()

    def entry(self, row: int) -> ImageEntry | None:
        if 0 <= row < len(self.entries):
            return self.entries[row]
        return None

    def row_for_image(self, image_path: Path) -> int | None:
        target = str(image_path.absolute()).casefold()
        for row, entry in enumerate(self.entries):
            if str(entry.image_path.absolute()).casefold() == target:
                return row
        return None

    def notify_entry_changed(self, row: int) -> None:
        if not 0 <= row < len(self.entries):
            return
        index = self.index(row, 0)
        self.dataChanged.emit(
            index,
            index,
            [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ToolTipRole,
                self.EntryRole,
            ],
        )
