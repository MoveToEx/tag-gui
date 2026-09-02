from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel

from .domain import ImageEntry


class ImageCatalogModel(QStandardItemModel):
    """Hierarchical folder model with a stable flat image order for navigation."""

    EntryRole = int(Qt.ItemDataRole.UserRole) + 1
    GroupRole = int(Qt.ItemDataRole.UserRole) + 2

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entries: list[ImageEntry] = []
        self.groups: list[str] = []
        self._image_items: list[QStandardItem] = []
        self.setHorizontalHeaderLabels(["Images"])

    def set_entries(
        self, entries: list[ImageEntry], root_directory: Path | None = None
    ) -> None:
        self.clear()
        self.setHorizontalHeaderLabels(["Images"])
        self.entries = entries
        self.groups = []
        self._image_items = []

        root_item = self.invisibleRootItem()
        folder_items: dict[Path, QStandardItem] = {Path("."): root_item}
        relative_parents: list[Path] = []

        for entry in entries:
            if root_directory is None:
                relative_parent = Path(entry.image_path.parent.name or ".")
            else:
                relative_parent = entry.image_path.parent.relative_to(root_directory)
            relative_parents.append(relative_parent)
            self.groups.append(
                "Root folder"
                if relative_parent == Path(".")
                else relative_parent.as_posix()
            )

        folders = sorted(
            {
                ancestor
                for relative_parent in relative_parents
                for ancestor in _folder_ancestors(relative_parent)
            },
            key=lambda path: (
                len(path.parts),
                tuple(part.casefold() for part in path.parts),
                path.as_posix(),
            ),
        )
        for relative_folder in folders:
            parent_path = relative_folder.parent
            parent_item = folder_items[parent_path]
            folder_item = QStandardItem(relative_folder.name)
            folder_item.setEditable(False)
            folder_item.setSelectable(False)
            folder_item.setToolTip(relative_folder.as_posix())
            parent_item.appendRow(folder_item)
            folder_items[relative_folder] = folder_item

        image_items: list[QStandardItem | None] = [None] * len(entries)
        for row, entry in enumerate(entries):
            if relative_parents[row] != Path("."):
                continue
            item = QStandardItem()
            item.setEditable(False)
            self._update_image_item(item, entry, self.groups[row])
            root_item.appendRow(item)
            image_items[row] = item
        for row, entry in enumerate(entries):
            if relative_parents[row] == Path("."):
                continue
            item = QStandardItem()
            item.setEditable(False)
            self._update_image_item(item, entry, self.groups[row])
            folder_items[relative_parents[row]].appendRow(item)
            image_items[row] = item
        self._image_items = [
            item for item in image_items if item is not None
        ]

    def entry(self, row: int) -> ImageEntry | None:
        if 0 <= row < len(self.entries):
            return self.entries[row]
        return None

    def entry_for_index(
        self, index: QModelIndex | QPersistentModelIndex
    ) -> ImageEntry | None:
        if not index.isValid():
            return None
        value = index.data(self.EntryRole)
        return value if isinstance(value, ImageEntry) else None

    def row_for_index(
        self, index: QModelIndex | QPersistentModelIndex
    ) -> int | None:
        entry = self.entry_for_index(index)
        if entry is None:
            return None
        return self.row_for_image(entry.image_path)

    def index_for_row(self, row: int) -> QModelIndex:
        if not 0 <= row < len(self._image_items):
            return QModelIndex()
        return self.indexFromItem(self._image_items[row])

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
        entry = self.entry(row)
        if entry is None or not 0 <= row < len(self._image_items):
            return
        self._update_image_item(self._image_items[row], entry, self.groups[row])

    def _update_image_item(
        self, item: QStandardItem, entry: ImageEntry, group: str
    ) -> None:
        suffix = " [read-only]" if not entry.editable else ""
        item.setText(f"{entry.image_path.name}  ({len(entry.tags)}){suffix}")
        details = [str(entry.image_path), f"Tags: {entry.tag_path.name}"]
        details.extend(entry.warnings)
        if entry.error:
            details.append(entry.error)
            item.setForeground(QColor("#b42318"))
        else:
            item.setData(None, Qt.ItemDataRole.ForegroundRole)
        item.setToolTip("\n".join(details))
        item.setData(entry, self.EntryRole)
        item.setData(group, self.GroupRole)


def _folder_ancestors(path: Path) -> list[Path]:
    if path == Path("."):
        return []
    return [Path(*path.parts[:length]) for length in range(1, len(path.parts) + 1)]
