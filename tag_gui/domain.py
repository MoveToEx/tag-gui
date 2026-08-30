from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from collections import Counter
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


def matching_tag_counts(
    entries: Iterable[ImageEntry], pattern: str
) -> list[tuple[str, int]]:
    if not pattern:
        return []
    counts = Counter(
        tag
        for entry in entries
        for tag in entry.tags
        if tag_matches_pattern(tag, pattern)
    )
    return sorted(counts.items(), key=lambda item: (item[0].casefold(), item[0]))


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
    entries that contain none of the requested tags, so both workflows avoid
    presenting no-op candidates by default.
    """
    if operation == TagOperation.NORMALIZE:
        raise ValueError("Normalization is not a traversal operation.")
    if operation == TagOperation.TOGGLE:
        return True
    current = set(current_tags)
    requested = set(requested_tags)
    if operation == TagOperation.ADD:
        return not requested.issubset(current)
    return not current.isdisjoint(requested)


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
        if operation == TagOperation.NORMALIZE:
            raise ValueError("Normalization is not a traversal operation.")
        if not requested_tags:
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
        self.extra_tags: dict[int, tuple[str, ...]] = {}
        self.applied_inputs: dict[
            int, tuple[tuple[str, ...], tuple[str, ...]]
        ] = {}
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
        if self.operation == TagOperation.TOGGLE:
            item = self.items[target]
            current = set(item.original_tags)
            return [tag for tag in self.requested_tags if tag in current]
        return self.eligible_for(target)

    def extra_tags_for(self, index: int | None = None) -> list[str]:
        target = self.current_index if index is None else index
        return list(self.extra_tags.get(target, ()))

    def set_ephemeral(
        self,
        selected_tags: Sequence[str],
        extra_tags: Sequence[str] = (),
        index: int | None = None,
    ) -> None:
        target = self.current_index if index is None else index
        eligible = set(self.eligible_for(target))
        selected = tuple(
            tag for tag in unique_tags(selected_tags) if tag in eligible
        )
        extras = tuple(
            unique_tags(extra_tags)
            if self.operation in {TagOperation.ADD, TagOperation.DELETE}
            else ()
        )
        self.selections[target] = selected
        self.extra_tags[target] = extras
        applied = self.applied_inputs.get(target)
        if applied is None:
            return
        if applied == (selected, extras):
            result = self.result_for(selected, extras, target)
            self.reviewed.add(target)
            if tuple(result) == self.items[target].original_tags:
                self.staged.pop(target, None)
            else:
                self.staged[target] = tuple(result)
        else:
            self.reviewed.discard(target)
            self.staged.pop(target, None)

    def result_for(
        self,
        selected_tags: Sequence[str],
        extra_tags: Sequence[str] = (),
        index: int | None = None,
    ) -> list[str]:
        target = self.current_index if index is None else index
        item = self.items[target]
        operation_tags = list(selected_tags)
        if self.operation in {TagOperation.ADD, TagOperation.DELETE}:
            operation_tags.extend(extra_tags)
        return apply_tag_operation(item.original_tags, operation_tags, self.operation)

    def apply_current(
        self,
        selected_tags: Sequence[str] = (),
        extra_tags: Sequence[str] = (),
    ) -> list[str]:
        eligible = set(self.eligible_for())
        selected = tuple(tag for tag in unique_tags(selected_tags) if tag in eligible)
        extras = (
            tuple(unique_tags(extra_tags))
            if self.operation in {TagOperation.ADD, TagOperation.DELETE}
            else ()
        )

        result = self.result_for(selected, extras)
        self.set_ephemeral(selected, extras)
        self.applied_inputs[self.current_index] = (selected, extras)
        self.reviewed.add(self.current_index)
        if tuple(result) == self.current_item.original_tags:
            self.staged.pop(self.current_index, None)
        else:
            self.staged[self.current_index] = tuple(result)
        return result

    def skip_current(self) -> None:
        self.set_ephemeral((), ())
        self.applied_inputs[self.current_index] = ((), ())
        self.reviewed.add(self.current_index)
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


@dataclass(frozen=True)
class ReviewItem:
    image_path: Path
    tag_path: Path
    original_tags: tuple[str, ...]
    source_bytes: bytes

    @classmethod
    def from_entry(cls, entry: ImageEntry) -> "ReviewItem":
        if not entry.editable or entry.source_bytes is None:
            raise ValueError(f"Entry is not editable: {entry.image_path}")
        return cls(
            image_path=entry.image_path,
            tag_path=entry.tag_path,
            original_tags=tuple(entry.tags),
            source_bytes=entry.source_bytes,
        )


class ReviewSession:
    """In-memory review state; sidecars are written only at completion."""

    def __init__(self, entries: Sequence[ImageEntry]) -> None:
        self.items = [
            ReviewItem.from_entry(entry)
            for entry in entries
            if entry.editable and entry.tags
        ]
        self.current_index = 0
        self.current_tag_index = 0
        self.completed = not self.items
        self.working_tags: dict[int, list[str]] = {
            index: list(item.original_tags) for index, item in enumerate(self.items)
        }
        self.reviewed_tags: dict[int, set[str]] = {
            index: set() for index in range(len(self.items))
        }

    @property
    def current_item(self) -> ReviewItem:
        return self.items[self.current_index]

    @property
    def current_tags(self) -> list[str]:
        if not self.items:
            return []
        return list(self.working_tags[self.current_index])

    @property
    def current_tag(self) -> str:
        if not self.items:
            return ""
        tags = self.working_tags[self.current_index]
        return tags[self.current_tag_index] if tags else ""

    @property
    def finished(self) -> bool:
        return self.completed

    @property
    def total_tag_count(self) -> int:
        return sum(len(item.original_tags) for item in self.items)

    @property
    def reviewed_tag_count(self) -> int:
        return sum(
            sum(tag in self.reviewed_tags[index] for tag in item.original_tags)
            for index, item in enumerate(self.items)
        )

    @property
    def deleted_tag_count(self) -> int:
        return sum(
            1
            for index, item in enumerate(self.items)
            for tag in item.original_tags
            if tag not in self.working_tags[index]
        )

    @property
    def at_first(self) -> bool:
        return self.current_index == 0 and self.current_tag_index == 0

    @property
    def at_last(self) -> bool:
        if not self.items or self.completed:
            return True
        return self._next_position() is None

    @property
    def has_changes(self) -> bool:
        return any(
            tuple(self.working_tags[index]) != item.original_tags
            for index, item in enumerate(self.items)
        )

    def keep_current(self) -> None:
        if self.completed:
            return
        self.reviewed_tags[self.current_index].add(self.current_tag)
        if not self._advance():
            self.completed = True

    def delete_current(self) -> None:
        if self.completed:
            return
        tags = self.working_tags[self.current_index]
        if not tags:
            if not self._advance():
                self.completed = True
            return
        removed = tags[self.current_tag_index]
        self.reviewed_tags[self.current_index].add(removed)
        del tags[self.current_tag_index]
        self.current_tag_index -= 1
        if not self._advance():
            self.completed = True

    def add_kept_tags(self, tags: Sequence[str]) -> list[str]:
        if self.completed or not self.items:
            return []
        current = self.working_tags[self.current_index]
        additions = [tag for tag in unique_tags(tags) if tag not in current]
        current.extend(additions)
        self.reviewed_tags[self.current_index].update(additions)
        return additions

    def move_back(self) -> bool:
        if self.completed:
            self.completed = False
            previous = self.current_index - 1
            while previous >= 0:
                if self.working_tags[previous]:
                    self.current_index = previous
                    self.current_tag_index = len(self.current_tags) - 1
                    return True
                previous -= 1
            self.completed = True
            return False
        if self.current_tag_index > 0:
            self.current_tag_index -= 1
            return True
        previous = self.current_index - 1
        while previous >= 0:
            if self.working_tags[previous]:
                self.current_index = previous
                self.current_tag_index = len(self.current_tags) - 1
                return True
            previous -= 1
        return False

    def _advance(self) -> bool:
        position = self._next_position()
        if position is None:
            return False
        self.current_index, self.current_tag_index = position
        return True

    def _next_position(self) -> tuple[int, int] | None:
        if not self.items:
            return None
        tags = self.working_tags[self.current_index]
        for tag_index in range(self.current_tag_index + 1, len(tags)):
            if tags[tag_index] not in self.reviewed_tags[self.current_index]:
                return self.current_index, tag_index
        for image_index in range(self.current_index + 1, len(self.items)):
            for tag_index, tag in enumerate(self.working_tags[image_index]):
                if tag not in self.reviewed_tags[image_index]:
                    return image_index, tag_index
        return None

    def staged_changes(self) -> list[tuple[ReviewItem, list[str]]]:
        return [
            (item, list(self.working_tags[index]))
            for index, item in enumerate(self.items)
            if tuple(self.working_tags[index]) != item.original_tags
        ]
