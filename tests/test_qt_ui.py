from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QGuiApplication, QImage
from PySide6.QtWidgets import QMessageBox

from tag_gui.domain import ImageEntry, TagOperation
from tag_gui.main_window import MainWindow
from tag_gui.preview import PreviewLoader
from tag_gui.traversal import TraversalDialog


def create_png(path: Path, color: str = "#2f6fed") -> None:
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))


def test_main_window_loads_folder_and_edits_current_tags(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "sample.png")
    (tmp_path / "sample.txt").write_text("dog, cat\n", encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)

    window._load_directory(tmp_path, show_issues=False)

    assert window.catalog.rowCount() == 1
    assert window.image_list.currentIndex().row() == 0
    assert [window.tag_list.item(i).text() for i in range(window.tag_list.count())] == [
        "dog",
        "cat",
    ]

    window.tag_input.setText("bird")
    window._add_current_tags()
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == (
        "bird, cat, dog\n"
    )


def test_navigation_actions_stop_at_boundaries(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "a.png")
    create_png(tmp_path / "b.png")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)

    assert not window.previous_action.isEnabled()
    assert window.next_action.isEnabled()
    window.next_action.trigger()
    assert window.image_list.currentIndex().row() == 1
    assert not window.next_action.isEnabled()
    assert window.previous_action.isEnabled()


def test_image_list_groups_images_by_subfolder(qtbot, tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    create_png(tmp_path / "root.png")
    create_png(nested / "child.png")
    window = MainWindow()
    qtbot.addWidget(window)

    window._load_directory(tmp_path, show_issues=False)

    assert window.catalog.rowCount() == 2
    assert window.catalog.data(window.catalog.index(0, 0)).startswith("root.png")
    assert window.catalog.data(window.catalog.index(1, 0)).startswith("child.png")
    assert window.catalog.group_for_row(0) == "Root folder"
    assert window.catalog.group_for_row(1) == "nested"

    window.next_action.trigger()
    assert window.image_list.currentIndex().row() == 1
    assert not window.next_action.isEnabled()


def test_close_folder_empties_program_state(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "sample.png")
    (tmp_path / "sample.txt").write_text("dog, cat\n", encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    window.tag_input.setText("bird")
    window.search_input.setText("cat")

    assert window.close_folder_action.isEnabled()
    window.close_folder_action.trigger()

    assert window.directory is None
    assert window.catalog.rowCount() == 0
    assert not window.image_list.currentIndex().isValid()
    assert window.tag_list.count() == 0
    assert window.tag_input.text() == ""
    assert window.search_input.text() == ""
    assert window.image_view._pixmap is None
    assert window.image_view._label.text() == "Open a folder to begin"
    assert window.statusBar().currentMessage() == ""
    assert not window.close_folder_action.isEnabled()
    assert not window.rescan_action.isEnabled()
    assert not window.search_input.isEnabled()
    assert not window.tag_input.isEnabled()


def test_close_folder_allows_another_folder_drop(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "sample.png")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    window.close_folder()

    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    drag_event = QDragEnterEvent(
        QPoint(20, 20),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    window.dragEnterEvent(drag_event)

    assert drag_event.isAccepted()


def test_toolbar_tag_search_cycles_and_supports_wildcards(qtbot, tmp_path: Path) -> None:
    for name, tags in {
        "a.png": "cat\n",
        "b.png": "dog\n",
        "c.png": "cat, night_scene\n",
    }.items():
        create_png(tmp_path / name)
        (tmp_path / f"{Path(name).stem}.txt").write_text(tags, encoding="utf-8")

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    window.show()
    qtbot.waitExposed(window)

    window.search_input.setText("cat")
    qtbot.keyClick(window.search_input, Qt.Key.Key_Return)
    assert window.image_list.currentIndex().row() == 2
    assert [item.text() for item in window.tag_list.selectedItems()] == ["cat"]
    qtbot.keyClick(window.search_input, Qt.Key.Key_Return)
    assert window.image_list.currentIndex().row() == 0
    assert [item.text() for item in window.tag_list.selectedItems()] == ["cat"]

    window.search_input.setText("night*")
    qtbot.keyClick(window.search_input, Qt.Key.Key_Return)
    assert window.image_list.currentIndex().row() == 2
    assert [item.text() for item in window.tag_list.selectedItems()] == [
        "night_scene"
    ]


def test_toolbar_tag_search_finds_remaining_matches_in_current_file(
    qtbot, tmp_path: Path
) -> None:
    for name, tags in {
        "a.png": "\n",
        "b.png": "match_one, match_two\n",
        "c.png": "match_three\n",
    }.items():
        create_png(tmp_path / name)
        (tmp_path / f"{Path(name).stem}.txt").write_text(tags, encoding="utf-8")

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    window.show()
    qtbot.waitExposed(window)

    window.search_input.setText("match_*")
    qtbot.keyClick(window.search_input, Qt.Key.Key_Return)
    assert window.image_list.currentIndex().row() == 1
    assert [item.text() for item in window.tag_list.selectedItems()] == ["match_one"]

    qtbot.keyClick(window.search_input, Qt.Key.Key_Return)
    assert window.image_list.currentIndex().row() == 1
    assert [item.text() for item in window.tag_list.selectedItems()] == ["match_two"]

    qtbot.keyClick(window.search_input, Qt.Key.Key_Return)
    assert window.image_list.currentIndex().row() == 2
    assert [item.text() for item in window.tag_list.selectedItems()] == ["match_three"]


def test_toolbar_tag_search_moves_backwards_with_shift_enter(
    qtbot, tmp_path: Path
) -> None:
    for name, tags in {
        "a.png": "match_one\n",
        "b.png": "match_two, match_three\n",
        "c.png": "match_four\n",
    }.items():
        create_png(tmp_path / name)
        (tmp_path / f"{Path(name).stem}.txt").write_text(tags, encoding="utf-8")

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    window.show()
    qtbot.waitExposed(window)
    window.search_input.setText("match_*")

    qtbot.keyClick(
        window.search_input,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ShiftModifier,
    )
    assert window.image_list.currentIndex().row() == 2
    assert [item.text() for item in window.tag_list.selectedItems()] == ["match_four"]

    qtbot.keyClick(
        window.search_input,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ShiftModifier,
    )
    assert window.image_list.currentIndex().row() == 1
    assert [item.text() for item in window.tag_list.selectedItems()] == ["match_three"]

    qtbot.keyClick(
        window.search_input,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ShiftModifier,
    )
    assert window.image_list.currentIndex().row() == 1
    assert [item.text() for item in window.tag_list.selectedItems()] == ["match_two"]


def test_empty_main_window_accepts_one_dropped_folder(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "sample.png")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    window = MainWindow()
    qtbot.addWidget(window)

    drag_event = QDragEnterEvent(
        QPoint(20, 20),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(drag_event)
    assert drag_event.isAccepted()

    drop_event = QDropEvent(
        QPointF(20, 20),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(drop_event)
    assert drop_event.isAccepted()
    assert window.directory == tmp_path
    assert window.catalog.rowCount() == 1

    second_drag = QDragEnterEvent(
        QPoint(20, 20),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(second_drag)
    assert not second_drag.isAccepted()


def test_tag_context_copy_uses_comma_space_separator(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "sample.png")
    (tmp_path / "sample.txt").write_text("dog, cat, bird\n", encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)

    window.tag_list.item(0).setSelected(True)
    window.tag_list.item(2).setSelected(True)
    assert window.tag_list.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu

    window._copy_selected_tags()

    assert QGuiApplication.clipboard().text() == "dog, bird"


def test_tag_context_delete_does_not_require_confirmation(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    create_png(tmp_path / "sample.png")
    tag_path = tmp_path / "sample.txt"
    tag_path.write_text("dog, cat, bird\n", encoding="utf-8", newline="\n")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)

    window.tag_list.item(0).setSelected(True)
    window.tag_list.item(2).setSelected(True)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("context-menu tag deletion should not confirm")
        ),
    )
    window._delete_selected_tags()

    assert tag_path.read_text(encoding="utf-8") == "cat\n"
    assert [item.text() for item in window.tag_list.selectedItems()] == []


def test_preview_loader_ignores_stale_generation(qtbot) -> None:
    loader = PreviewLoader()
    received: list[str] = []
    loader.loaded.connect(lambda _image, error: received.append(error))
    loader._generation = 2

    loader._on_finished(1, QImage(), "stale")
    loader._on_finished(2, QImage(), "current")

    assert received == ["current"]


def test_traversal_does_not_write_until_finish(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "sample.png")
    tag_path = tmp_path / "sample.txt"
    tag_path.write_bytes(b"cat\n")
    entry = ImageEntry(
        image_path=tmp_path / "sample.png",
        tag_path=tag_path,
        tags=["cat"],
        source_bytes=b"cat\n",
    )
    dialog = TraversalDialog([entry], TagOperation.ADD, ["dog"])
    qtbot.addWidget(dialog)

    assert dialog.choices.item(0).checkState() == Qt.CheckState.Unchecked
    dialog.choices.item(0).setCheckState(Qt.CheckState.Checked)
    dialog._apply()
    assert tag_path.read_bytes() == b"cat\n"
    assert dialog.finish_button.isEnabled()

    dialog._finish()
    assert tag_path.read_bytes() == b"cat, dog\n"


def test_traversal_apply_all_requires_confirmation_and_commits_all_options(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    entries: list[ImageEntry] = []
    for name, tags in {"first": "cat\n", "second": "bird\n"}.items():
        image_path = tmp_path / f"{name}.png"
        tag_path = tmp_path / f"{name}.txt"
        create_png(image_path)
        tag_path.write_bytes(tags.encode())
        entries.append(
            ImageEntry(
                image_path=image_path,
                tag_path=tag_path,
                tags=tags.strip().split(", "),
                source_bytes=tags.encode(),
            )
        )

    dialog = TraversalDialog(entries, TagOperation.ADD, ["dog", "night"])
    qtbot.addWidget(dialog)
    confirmations: list[str] = []

    def cancel(_parent, _title, message, *_args):
        confirmations.append(message)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", cancel)
    dialog.apply_all_button.click()

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "cat\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "bird\n"
    assert "2 sidecar file(s) will change" in confirmations[0]

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    dialog.apply_all_button.click()

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == (
        "cat, dog, night\n"
    )
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == (
        "bird, dog, night\n"
    )
    assert dialog.commit_result is not None
    assert dialog.commit_result.complete


def test_traversal_keyboard_navigation_selects_toggles_and_moves(qtbot, tmp_path: Path) -> None:
    first_image = tmp_path / "first.png"
    second_image = tmp_path / "second.png"
    create_png(first_image)
    create_png(second_image)
    first_tag = tmp_path / "first.txt"
    second_tag = tmp_path / "second.txt"
    first_tag.write_bytes(b"cat\n")
    second_tag.write_bytes(b"dog\n")
    entries = [
        ImageEntry(first_image, first_tag, ["cat"], b"cat\n"),
        ImageEntry(second_image, second_tag, ["dog"], b"dog\n"),
    ]
    dialog = TraversalDialog(entries, TagOperation.ADD, ["bird", "night"])
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.choices.currentRow() == 0
    qtbot.keyClick(dialog.choices, Qt.Key.Key_Down)
    assert dialog.choices.currentRow() == 1
    qtbot.keyClick(dialog.choices, Qt.Key.Key_Up)
    assert dialog.choices.currentRow() == 0
    qtbot.keyClick(dialog.choices, Qt.Key.Key_Down)
    qtbot.keyClick(dialog.choices, Qt.Key.Key_Space)
    assert dialog.choices.item(1).checkState() == Qt.CheckState.Checked

    qtbot.keyClick(dialog.choices, Qt.Key.Key_Return)
    assert dialog.session.current_index == 1
    qtbot.keyClick(dialog.choices, Qt.Key.Key_Left)
    assert dialog.session.current_index == 0
    qtbot.keyClick(dialog.choices, Qt.Key.Key_Right)
    assert dialog.session.current_index == 1


def test_traversal_a_shortcut_toggles_all_options(qtbot, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    tag_path = tmp_path / "sample.txt"
    create_png(image_path)
    tag_path.write_bytes(b"cat\n")
    entry = ImageEntry(image_path, tag_path, ["cat"], b"cat\n")
    dialog = TraversalDialog(
        entries=[entry],
        operation=TagOperation.ADD,
        requested_tags=["dog", "bird"],
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert [
        dialog.choices.item(row).checkState()
        for row in range(dialog.choices.count())
    ] == [Qt.CheckState.Unchecked, Qt.CheckState.Unchecked]
    qtbot.keyClick(dialog.choices, Qt.Key.Key_A)
    assert all(
        dialog.choices.item(row).checkState() == Qt.CheckState.Checked
        for row in range(dialog.choices.count())
    )
    qtbot.keyClick(dialog.choices, Qt.Key.Key_A)
    assert all(
        dialog.choices.item(row).checkState() == Qt.CheckState.Unchecked
        for row in range(dialog.choices.count())
    )


def test_traversal_temporary_extra_tags_clear_on_image_switch(qtbot, tmp_path: Path) -> None:
    entries: list[ImageEntry] = []
    for name in ["first", "second"]:
        image_path = tmp_path / f"{name}.png"
        tag_path = tmp_path / f"{name}.txt"
        create_png(image_path)
        tag_path.write_bytes(b"cat\n")
        entries.append(ImageEntry(image_path, tag_path, ["cat"], b"cat\n"))

    dialog = TraversalDialog(entries, TagOperation.ADD, ["base"])
    qtbot.addWidget(dialog)
    assert not dialog.temporary_input.isHidden()

    dialog.temporary_input.setText("temporary, image_only")
    assert dialog.apply_button.isEnabled()
    assert dialog.result_tags.toPlainText() == "cat, image_only, temporary"
    dialog._apply()

    assert dialog.session.current_index == 1
    assert dialog.temporary_input.text() == ""
    assert dialog.session.staged[0] == ("cat", "image_only", "temporary")
