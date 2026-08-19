from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from typing import Iterable, Sequence


class TagOperation(StrEnum):
    ADD = "add"
    DELETE = "delete"
    TOGGLE = "toggle"
    NORMALIZE = "normalize"


@dataclass
class ImageEntry:
    image_path: Path
    tag_path: Path
    tags: list[str] = field(default_factory=list)
    source_bytes: bytes | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None

    @property
    def editable(self) -> bool:
        return self.error is None and self.source_bytes is not None


@dataclass(frozen=True)
class ScanIssue:
    message: str
    paths: tuple[Path, ...] = ()


@dataclass
class ScanResult:
    entries: list[ImageEntry] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)


def unique_tags(tags: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


def parse_tags(text: str) -> list[str]:
    return unique_tags(text.split(","))


def parse_requested_tags(text: str) -> list[str]:
    tags = parse_tags(text)
    if not tags:
        raise ValueError("Enter at least one tag.")
    return tags


def normalize_tags(tags: Iterable[str]) -> list[str]:
    return sorted(unique_tags(tags))


def serialize_tags(tags: Iterable[str]) -> str:
    return ", ".join(normalize_tags(tags)) + "\n"


def tag_matches_pattern(tag: str, pattern: str) -> bool:
    """Match a complete tag, treating only ``*`` as a wildcard."""
    if not pattern:
        return False
    expression = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(expression, tag, flags=re.DOTALL) is not None


def eligible_tags(
    current_tags: Sequence[str],
    requested_tags: Sequence[str],
    operation: TagOperation,
) -> list[str]:
    current = set(current_tags)
    if operation == TagOperation.ADD:
        return [tag for tag in requested_tags if tag not in current]
    if operation == TagOperation.DELETE:
        return [tag for tag in requested_tags if tag in current]
    if operation == TagOperation.TOGGLE:
        return list(requested_tags)
    return []


def should_traverse_entry(
    current_tags: Sequence[str],
    requested_tags: Sequence[str],
    operation: TagOperation,
) -> bool:
    """Return whether an entry should be shown in a folder traversal.

    Add skips entries that already contain every requested tag. Delete skips
    entries that do not contain every requested tag, so both workflows avoid
    presenting partial/no-op candidates by default.
    """
    if operation not in {TagOperation.ADD, TagOperation.DELETE}:
        return True
    current = set(current_tags)
    requested = set(requested_tags)
    if operation == TagOperation.ADD:
        return not requested.issubset(current)
    return requested.issubset(current)


def filter_traversal_entries(
    entries: Sequence[ImageEntry],
    operation: TagOperation,
    requested_tags: Sequence[str] = (),
) -> list[ImageEntry]:
    requested = unique_tags(requested_tags)
    return [
        entry
        for entry in entries
        if should_traverse_entry(entry.tags, requested, operation)
    ]


def apply_tag_operation(
    current_tags: Sequence[str],
    requested_tags: Sequence[str],
    operation: TagOperation,
) -> list[str]:
    if operation == TagOperation.NORMALIZE:
        return normalize_tags(current_tags)

    requested = unique_tags(requested_tags)
    current = list(current_tags)
    current_set = set(current)

    if operation == TagOperation.ADD:
        result = [*current, *(tag for tag in requested if tag not in current_set)]
    elif operation == TagOperation.DELETE:
        requested_set = set(requested)
        result = [tag for tag in current if tag not in requested_set]
    elif operation == TagOperation.TOGGLE:
        requested_set = set(requested)
        result = [tag for tag in current if tag not in requested_set]
        result.extend(tag for tag in requested if tag not in current_set)
    else:
        raise ValueError(f"Unsupported tag operation: {operation}")

    return normalize_tags(result)


@dataclass(frozen=True)
class TraversalItem:
    image_path: Path
    tag_path: Path
    original_tags: tuple[str, ...]
    source_bytes: bytes

    @classmethod
    def from_entry(cls, entry: ImageEntry) -> TraversalItem:
        if not entry.editable or entry.source_bytes is None:
            raise ValueError(f"Entry is not editable: {entry.image_path}")
        return cls(
            image_path=entry.image_path,
            tag_path=entry.tag_path,
            original_tags=tuple(entry.tags),
            source_bytes=entry.source_bytes,
        )


class TraversalSession:
    def __init__(
        self,
        entries: Sequence[ImageEntry],
        operation: TagOperation,
        requested_tags: Sequence[str] = (),
    ) -> None:
        if operation != TagOperation.NORMALIZE and not requested_tags:
            raise ValueError("This operation requires at least one tag.")

        self.operation = operation
        self.requested_tags = tuple(unique_tags(requested_tags))
        filtered_entries = filter_traversal_entries(
            entries, operation, self.requested_tags
        )
        self.items = [TraversalItem.from_entry(entry) for entry in filtered_entries]
        if not self.items:
            raise ValueError("No images match the requested traversal tags.")
        self.current_index = 0
        self.reviewed: set[int] = set()
        self.selections: dict[int, tuple[str, ...]] = {}
        self.staged: dict[int, tuple[str, ...]] = {}

    @property
    def current_item(self) -> TraversalItem:
        return self.items[self.current_index]

    @property
    def at_first(self) -> bool:
        return self.current_index == 0

    @property
    def at_last(self) -> bool:
        return self.current_index == len(self.items) - 1

    @property
    def has_changes(self) -> bool:
        return bool(self.staged)

    def eligible_for(self, index: int | None = None) -> list[str]:
        item = self.items[self.current_index if index is None else index]
        return eligible_tags(item.original_tags, self.requested_tags, self.operation)

    def selected_for(self, index: int | None = None) -> list[str]:
        target = self.current_index if index is None else index
        if target in self.selections:
            return list(self.selections[target])
        if self.operation in {TagOperation.ADD, TagOperation.DELETE}:
            return []
        return self.eligible_for(target)

    def result_for(
        self,
        selected_tags: Sequence[str],
        extra_tags: Sequence[str] = (),
        index: int | None = None,
    ) -> list[str]:
        target = self.current_index if index is None else index
        item = self.items[target]
        if self.operation == TagOperation.NORMALIZE:
            return normalize_tags(item.original_tags)
        operation_tags = list(selected_tags)
        if self.operation in {TagOperation.ADD, TagOperation.DELETE}:
            operation_tags.extend(extra_tags)
        return apply_tag_operation(item.original_tags, operation_tags, self.operation)

    def apply_current(
        self,
        selected_tags: Sequence[str] = (),
        extra_tags: Sequence[str] = (),
    ) -> list[str]:
        if self.operation == TagOperation.NORMALIZE:
            selected = ()
            extras = ()
        else:
            eligible = set(self.eligible_for())
            selected = tuple(tag for tag in unique_tags(selected_tags) if tag in eligible)
            extras = (
                tuple(unique_tags(extra_tags))
                if self.operation in {TagOperation.ADD, TagOperation.DELETE}
                else ()
            )

        result = self.result_for(selected, extras)
        self.reviewed.add(self.current_index)
        self.selections[self.current_index] = tuple(selected)
        if tuple(result) == self.current_item.original_tags:
            self.staged.pop(self.current_index, None)
        else:
            self.staged[self.current_index] = tuple(result)
        return result

    def skip_current(self) -> None:
        self.reviewed.add(self.current_index)
        self.selections[self.current_index] = ()
        self.staged.pop(self.current_index, None)

    def move_next(self) -> bool:
        if self.at_last:
            return False
        self.current_index += 1
        return True

    def move_back(self) -> bool:
        if self.at_first:
            return False
        self.current_index -= 1
        return True

    def staged_changes(self) -> list[tuple[TraversalItem, list[str]]]:
        return [
            (self.items[index], list(tags))
            for index, tags in sorted(self.staged.items())
        ]

    def all_available_changes(self) -> list[tuple[TraversalItem, list[str]]]:
        changes: list[tuple[TraversalItem, list[str]]] = []
        for index, item in enumerate(self.items):
            result = self.result_for(self.eligible_for(index), index=index)
            if tuple(result) != item.original_tags:
                changes.append((item, result))
        return changes
