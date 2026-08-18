from __future__ import annotations

from pathlib import Path

import pytest

from tag_gui.storage import (
    BatchPreflightError,
    ExternalChangeError,
    WriteRequest,
    scan_folder,
    write_tags_atomic,
    write_tags_batch,
)


IMAGE_EXTENSIONS = {".jpg", ".png"}


def touch_image(path: Path) -> None:
    path.write_bytes(b"not decoded by scanner")


def test_scan_creates_stem_sidecar_and_ignores_nested_images(tmp_path: Path) -> None:
    touch_image(tmp_path / "one.JPG")
    nested = tmp_path / "nested"
    nested.mkdir()
    touch_image(nested / "two.jpg")

    result = scan_folder(tmp_path, IMAGE_EXTENSIONS)

    assert [entry.image_path.name for entry in result.entries] == ["one.JPG"]
    assert result.entries[0].tag_path.name == "one.txt"
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "\n"


def test_scan_prefers_stem_then_falls_back_to_full_name(tmp_path: Path) -> None:
    touch_image(tmp_path / "a.jpg")
    (tmp_path / "a.txt").write_text("stem", encoding="utf-8")
    (tmp_path / "a.jpg.txt").write_text("full", encoding="utf-8")
    touch_image(tmp_path / "b.png")
    (tmp_path / "b.png.txt").write_text("full-only", encoding="utf-8")

    result = scan_folder(tmp_path, IMAGE_EXTENSIONS)
    entries = {entry.image_path.name: entry for entry in result.entries}

    assert entries["a.jpg"].tag_path.name == "a.txt"
    assert entries["a.jpg"].tags == ["stem"]
    assert "ignored" in entries["a.jpg"].warnings[0]
    assert entries["b.png"].tag_path.name == "b.png.txt"


def test_scan_excludes_duplicate_stems(tmp_path: Path) -> None:
    touch_image(tmp_path / "same.jpg")
    touch_image(tmp_path / "same.png")

    result = scan_folder(tmp_path, IMAGE_EXTENSIONS)

    assert result.entries == []
    assert "duplicate image stems" in result.issues[0].message
    assert not (tmp_path / "same.txt").exists()


def test_scan_excludes_cross_form_sidecar_collision(tmp_path: Path) -> None:
    touch_image(tmp_path / "foo.jpg")
    touch_image(tmp_path / "foo.jpg.png")
    (tmp_path / "foo.jpg.txt").write_text("shared", encoding="utf-8")

    result = scan_folder(tmp_path, IMAGE_EXTENSIONS)

    assert result.entries == []
    assert any("same sidecar" in issue.message for issue in result.issues)


def test_invalid_utf8_sidecar_is_visible_but_read_only(tmp_path: Path) -> None:
    touch_image(tmp_path / "bad.jpg")
    (tmp_path / "bad.txt").write_bytes(b"\xff")

    entry = scan_folder(tmp_path, IMAGE_EXTENSIONS).entries[0]

    assert not entry.editable
    assert "UTF-8" in (entry.error or "")


def test_atomic_write_detects_external_change(tmp_path: Path) -> None:
    path = tmp_path / "tags.txt"
    path.write_bytes(b"cat\n")

    with pytest.raises(ExternalChangeError):
        write_tags_atomic(path, ["dog"], expected_bytes=b"old\n")

    assert path.read_bytes() == b"cat\n"


def test_batch_preflight_failure_writes_nothing(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"a\n")
    second.write_bytes(b"changed\n")
    requests = [
        WriteRequest(first, ["x"], b"a\n"),
        WriteRequest(second, ["y"], b"b\n"),
    ]

    with pytest.raises(BatchPreflightError):
        write_tags_batch(requests)

    assert first.read_bytes() == b"a\n"
    assert second.read_bytes() == b"changed\n"
