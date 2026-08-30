from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .domain import ImageEntry, ScanIssue, ScanResult, parse_tags, serialize_tags


class ExternalChangeError(OSError):
    pass


class BatchPreflightError(OSError):
    pass


@dataclass(frozen=True)
class WriteRequest:
    path: Path
    tags: Sequence[str]
    expected_bytes: bytes | None = None


@dataclass
class BatchCommitResult:
    succeeded: list[Path]
    failures: dict[Path, str]

    @property
    def complete(self) -> bool:
        return not self.failures


@dataclass
class FlattenResult:
    succeeded: list[tuple[Path, Path]]
    failures: dict[Path, str]

    @property
    def complete(self) -> bool:
        return not self.failures


@dataclass
class _ResolvedImage:
    image_path: Path
    tag_path: Path
    create: bool
    warnings: tuple[str, ...]


@dataclass
class _PreparedWrite:
    path: Path
    temp_path: Path


def _path_key(path: Path) -> str:
    return str(path.absolute()).casefold()


def _single_casefold_match(
    files_by_name: dict[str, list[Path]], name: str
) -> tuple[Path | None, str | None]:
    matches = files_by_name.get(name.casefold(), [])
    if len(matches) > 1:
        return None, f"Multiple files match {name!r} by case."
    return (matches[0] if matches else None), None


def scan_folder(directory: Path, supported_extensions: Iterable[str]) -> ScanResult:
    directory = Path(directory)
    files = [path for path in directory.rglob("*") if path.is_file()]
    files_by_directory: dict[Path, dict[str, list[Path]]] = {}
    for path in files:
        files_by_directory.setdefault(path.parent, {}).setdefault(
            path.name.casefold(), []
        ).append(path)

    extensions = {
        extension.casefold()
        if extension.startswith(".")
        else f".{extension.casefold()}"
        for extension in supported_extensions
    }
    images = sorted(
        (path for path in files if path.suffix.casefold() in extensions),
        key=lambda path: (
            str(path.parent.relative_to(directory)).casefold(),
            str(path.parent.relative_to(directory)),
            path.name.casefold(),
            path.name,
        ),
    )

    result = ScanResult()
    images_by_stem: dict[tuple[Path, str], list[Path]] = {}
    for image_path in images:
        images_by_stem.setdefault(
            (image_path.parent, image_path.stem.casefold()), []
        ).append(image_path)

    excluded: set[Path] = set()
    for group in images_by_stem.values():
        if len(group) <= 1:
            continue
        excluded.update(group)
        names = ", ".join(str(path.relative_to(directory)) for path in group)
        result.issues.append(
            ScanIssue(
                f"Excluded duplicate image stems: {names}", tuple(group)
            )
        )

    resolved: list[_ResolvedImage] = []
    for image_path in images:
        if image_path in excluded:
            continue

        files_by_name = files_by_directory[image_path.parent]
        stem_name = f"{image_path.stem}.txt"
        full_name = f"{image_path.name}.txt"
        stem_path, stem_error = _single_casefold_match(files_by_name, stem_name)
        full_path, full_error = _single_casefold_match(files_by_name, full_name)
        if stem_error or full_error:
            result.issues.append(
                ScanIssue(stem_error or full_error or "Ambiguous sidecar.", (image_path,))
            )
            continue

        warnings: list[str] = []
        if stem_path is not None:
            tag_path = stem_path
            create = False
            if full_path is not None and full_path != stem_path:
                warnings.append(
                    f"Using {stem_path.name}; ignored {full_path.name}."
                )
        elif full_path is not None:
            tag_path = full_path
            create = False
        else:
            tag_path = image_path.parent / stem_name
            create = True

        resolved.append(
            _ResolvedImage(
                image_path=image_path,
                tag_path=tag_path,
                create=create,
                warnings=tuple(warnings),
            )
        )

    resolved_by_tag: dict[str, list[_ResolvedImage]] = {}
    for item in resolved:
        resolved_by_tag.setdefault(_path_key(item.tag_path), []).append(item)

    collided: set[Path] = set()
    for group in resolved_by_tag.values():
        if len(group) <= 1:
            continue
        paths = tuple(item.image_path for item in group)
        collided.update(paths)
        result.issues.append(
            ScanIssue(
                "Excluded images that resolve to the same sidecar: "
                + ", ".join(str(path.relative_to(directory)) for path in paths),
                paths,
            )
        )

    for item in resolved:
        if item.image_path in collided:
            continue

        error: str | None = None
        source_bytes: bytes | None = None
        tags: list[str] = []
        if item.create:
            try:
                with item.tag_path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write("\n")
            except FileExistsError:
                pass
            except OSError as exc:
                error = f"Could not create {item.tag_path.name}: {exc}"

        if error is None:
            try:
                source_bytes = item.tag_path.read_bytes()
                tags = parse_tags(source_bytes.decode("utf-8"))
            except UnicodeError as exc:
                error = f"Could not decode {item.tag_path.name} as UTF-8: {exc}"
            except OSError as exc:
                error = f"Could not read {item.tag_path.name}: {exc}"

        result.entries.append(
            ImageEntry(
                image_path=item.image_path,
                tag_path=item.tag_path,
                tags=tags,
                source_bytes=source_bytes,
                warnings=item.warnings,
                error=error,
            )
        )

    return result


def _copy_exclusive(source: Path, destination: Path) -> None:
    created = False
    try:
        with source.open("rb") as source_stream, destination.open("xb") as target_stream:
            created = True
            shutil.copyfileobj(source_stream, target_stream)
        shutil.copystat(source, destination)
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise


def flatten_entries(
    entries: Sequence[ImageEntry], destination: Path
) -> FlattenResult:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    occupied_names = {
        child.name.casefold() for child in destination.iterdir()
    }
    succeeded: list[tuple[Path, Path]] = []
    failures: dict[Path, str] = {}

    for entry in entries:
        if not entry.image_path.is_file():
            failures[entry.image_path] = "Image file does not exist."
            continue
        if not entry.tag_path.is_file():
            failures[entry.image_path] = (
                f"Tag file does not exist: {entry.tag_path.name}"
            )
            continue

        index = 0
        while True:
            output_stem = (
                entry.image_path.stem
                if index == 0
                else f"{entry.image_path.stem}_{index}"
            )
            image_name = f"{output_stem}{entry.image_path.suffix}"
            tag_name = f"{output_stem}.txt"
            keys = {image_name.casefold(), tag_name.casefold()}
            if keys.isdisjoint(occupied_names):
                break
            index += 1

        image_target = destination / image_name
        tag_target = destination / tag_name
        image_created = False
        tag_created = False
        try:
            _copy_exclusive(entry.image_path, image_target)
            image_created = True
            _copy_exclusive(entry.tag_path, tag_target)
            tag_created = True
        except OSError as exc:
            if tag_created:
                tag_target.unlink(missing_ok=True)
            if image_created:
                image_target.unlink(missing_ok=True)
            failures[entry.image_path] = str(exc)
            continue

        occupied_names.update(keys)
        succeeded.append((image_target, tag_target))

    return FlattenResult(succeeded=succeeded, failures=failures)


def _check_expected_bytes(path: Path, expected_bytes: bytes | None) -> None:
    if expected_bytes is None:
        return
    try:
        current = path.read_bytes()
    except OSError as exc:
        raise ExternalChangeError(f"Could not verify {path}: {exc}") from exc
    if current != expected_bytes:
        raise ExternalChangeError(f"{path.name} changed outside the application.")


def _prepare_write(request: WriteRequest) -> _PreparedWrite:
    _check_expected_bytes(request.path, request.expected_bytes)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{request.path.name}.",
            suffix=".tmp",
            dir=request.path.parent,
            delete=False,
        ) as stream:
            stream.write(serialize_tags(request.tags).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            temp_path = Path(stream.name)

        if request.path.exists():
            shutil.copymode(request.path, temp_path)
        return _PreparedWrite(path=request.path, temp_path=temp_path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def write_tags_atomic(
    path: Path, tags: Sequence[str], expected_bytes: bytes | None = None
) -> bytes:
    request = WriteRequest(path=Path(path), tags=tags, expected_bytes=expected_bytes)
    prepared = _prepare_write(request)
    try:
        os.replace(prepared.temp_path, prepared.path)
    finally:
        prepared.temp_path.unlink(missing_ok=True)
    return serialize_tags(tags).encode("utf-8")


def write_tags_batch(requests: Sequence[WriteRequest]) -> BatchCommitResult:
    paths = [_path_key(request.path) for request in requests]
    if len(paths) != len(set(paths)):
        raise BatchPreflightError("The batch contains duplicate sidecar paths.")

    prepared: list[_PreparedWrite] = []
    try:
        for request in requests:
            prepared.append(_prepare_write(request))
    except Exception as exc:
        for item in prepared:
            item.temp_path.unlink(missing_ok=True)
        if isinstance(exc, ExternalChangeError):
            raise BatchPreflightError(str(exc)) from exc
        raise BatchPreflightError(f"Could not prepare tag updates: {exc}") from exc

    succeeded: list[Path] = []
    failures: dict[Path, str] = {}
    for item in prepared:
        try:
            os.replace(item.temp_path, item.path)
            succeeded.append(item.path)
        except OSError as exc:
            failures[item.path] = str(exc)
        finally:
            item.temp_path.unlink(missing_ok=True)

    return BatchCommitResult(succeeded=succeeded, failures=failures)
