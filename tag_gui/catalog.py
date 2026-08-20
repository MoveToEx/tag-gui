from __future__ import annotations

from pathlib import Path
from typing import override

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem

from .domain import ImageEntry


class ImageCatalogModel(QAbstractListModel):
    EntryRole = int(Qt.ItemDataRole.UserRole) + 1
    GroupRole = int(Qt.ItemDataRole.UserRole) + 2

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entries: list[ImageEntry] = []
        self.groups: list[str] = []

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
            details = [str(entry.image_path), f"Tags: {entry.tag_path.name}"]
            details.extend(entry.warnings)
            if entry.error:
                details.append(entry.error)
            return "\n".join(details)
        if role == Qt.ItemDataRole.ForegroundRole and entry.error:
            return QColor("#b42318")
        if role == self.EntryRole:
            return entry
        if role == self.GroupRole:
            return self.groups[index.row()]
        return None

    def set_entries(
        self, entries: list[ImageEntry], root_directory: Path | None = None
    ) -> None:
        self.beginResetModel()
        self.entries = entries
        self.groups = []
        for entry in entries:
            if root_directory is None:
                group = entry.image_path.parent.name or "Root folder"
            else:
                relative_parent = entry.image_path.parent.relative_to(root_directory)
                group = (
                    "Root folder"
                    if relative_parent == Path(".")
                    else relative_parent.as_posix()
                )
            self.groups.append(group)
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

    def group_for_row(self, row: int) -> str | None:
        if 0 <= row < len(self.groups):
            return self.groups[row]
        return None

    @property
    def image_count(self) -> int:
        return len(self.entries)

    def image_position(self, row: int) -> int | None:
        return row + 1 if self.entry(row) is not None else None

    def first_image_row(self) -> int | None:
        return 0 if self.entries else None

    def last_image_row(self) -> int | None:
        return len(self.entries) - 1 if self.entries else None

    def next_image_row(self, row: int) -> int | None:
        return row + 1 if row + 1 < len(self.entries) else None

    def previous_image_row(self, row: int) -> int | None:
        return row - 1 if 0 < row < len(self.entries) else None

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


class GroupedImageDelegate(QStyledItemDelegate):
    header_height = 26

    @override
    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QSize:
        size = super().sizeHint(option, index)
        if self._is_group_start(index):
            size.setHeight(size.height() + self.header_height)
        return size

    @override
    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        if self._is_group_start(index):
            header_rect = QRect(
                option.rect.left(),
                option.rect.top(),
                option.rect.width(),
                self.header_height,
            )
            painter.save()
            painter.fillRect(header_rect, QColor("#eef2f6"))
            font = QFont(option.font)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#344054"))
            painter.drawText(
                header_rect.adjusted(8, 0, -8, 0),
                Qt.AlignmentFlag.AlignVCenter,
                str(index.data(ImageCatalogModel.GroupRole)),
            )
            painter.restore()
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.adjusted(0, self.header_height, 0, 0)
        super().paint(painter, option, index)

    def _is_group_start(
        self, index: QModelIndex | QPersistentModelIndex
    ) -> bool:
        if index.row() == 0:
            return True
        model = index.model()
        if model is None:
            return False
        previous = model.index(index.row() - 1, index.column())
        return bool(
            index.data(ImageCatalogModel.GroupRole)
            != previous.data(ImageCatalogModel.GroupRole)
        )
