from __future__ import annotations

import threading
import zipfile
from pathlib import Path

from PySide6.QtCore import (
    QItemSelectionModel,
    QMimeData,
    QPoint,
    QPointF,
    Qt,
    QUrl,
)
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QGuiApplication, QImage
from PySide6.QtWidgets import QGroupBox, QMessageBox, QTreeWidget

import tagger.main_window as main_window_module
import tagger.archive as archive_module
from tagger.storage import ArchiveResult
from tagger.bulk_operation import BulkOperationDialog
from tagger.domain import ImageEntry, TagOperation
from tagger.complex_filter import ComplexFilterDialog
from tagger.main_window import MainWindow
from tagger.global_search import GlobalTagSearchDialog
from tagger.preview import PreviewLoader
from tagger.review import ReviewDialog
from tagger.settings import (
    JsonSettings,
    PARENTHESES_SETTING,
    PROXY_MODE_SETTING,
    PROXY_SETTING,
    SettingsDialog,
    UNDERSCORES_SETTING,
)
from tagger.tag_library import DownloadTagsDialog, TagLibrary
from tagger.traversal import TraversalDialog


def create_png(path: Path, color: str = "#2f6fed") -> None:
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))


def test_settings_changes_only_take_effect_when_applied(
    qtbot, tmp_path: Path
) -> None:
    settings_path = tmp_path / "settings.json"
    settings = JsonSettings(settings_path)
    settings.setValue(UNDERSCORES_SETTING, False)
    settings.setValue(PARENTHESES_SETTING, False)
    settings.sync()
    library_path = tmp_path / "tags.csv"
    library_path.write_text(
        "name,post_count\nred_hair_(long),100\n", encoding="utf-8"
    )
    tag_library = TagLibrary(library_path)
    dialog = SettingsDialog(settings=settings, tag_library=tag_library)
    qtbot.addWidget(dialog)

    assert not dialog.apply_button.isEnabled()
    assert tag_library.suggestions("red") == ["red_hair_(long)"]

    dialog.underscores_checkbox.setChecked(True)
    assert dialog.apply_button.isEnabled()
    dialog.underscores_checkbox.setChecked(False)
    assert not dialog.apply_button.isEnabled()

    dialog.underscores_checkbox.setChecked(True)
    dialog.parentheses_checkbox.setChecked(True)

    assert settings.value(UNDERSCORES_SETTING, type=bool) is False
    assert settings.value(PARENTHESES_SETTING, type=bool) is False
    persisted = JsonSettings(settings_path)
    assert persisted.value(UNDERSCORES_SETTING, type=bool) is False
    assert persisted.value(PARENTHESES_SETTING, type=bool) is False
    assert tag_library.suggestions("red") == ["red_hair_(long)"]

    dialog.apply_button.click()

    assert not dialog.apply_button.isEnabled()
    assert settings.value(UNDERSCORES_SETTING, type=bool) is True
    assert settings.value(PARENTHESES_SETTING, type=bool) is True
    persisted = JsonSettings(settings_path)
    assert persisted.value(UNDERSCORES_SETTING, type=bool) is True
    assert persisted.value(PARENTHESES_SETTING, type=bool) is True
    assert tag_library.suggestions("red") == [r"red hair \(long\)"]


def test_settings_ok_applies_and_cancel_discards(qtbot, tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings = JsonSettings(settings_path)
    dialog = SettingsDialog(settings=settings)
    qtbot.addWidget(dialog)

    assert dialog.ok_button.text() == "OK"
    assert dialog.cancel_button.text() == "Cancel"
    dialog.underscores_checkbox.setChecked(True)
    dialog.ok_button.click()

    assert dialog.result() == SettingsDialog.DialogCode.Accepted
    assert settings.value(UNDERSCORES_SETTING, type=bool) is True
    persisted = JsonSettings(settings_path)
    assert persisted.value(UNDERSCORES_SETTING, type=bool) is True

    cancel_dialog = SettingsDialog(settings=settings)
    qtbot.addWidget(cancel_dialog)
    cancel_dialog.underscores_checkbox.setChecked(False)
    cancel_dialog.cancel_button.click()

    assert cancel_dialog.result() == SettingsDialog.DialogCode.Rejected
    assert settings.value(UNDERSCORES_SETTING, type=bool) is True
    persisted = JsonSettings(settings_path)
    assert persisted.value(UNDERSCORES_SETTING, type=bool) is True


def test_tag_library_settings_separate_transformations_and_downloads(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    library_path = tmp_path / "tags.csv"
    library_path.write_text(
        "name,post_count\nred_hair,100\nblue_eyes,80\n",
        encoding="utf-8",
    )
    tag_library = TagLibrary(library_path)
    dialog = SettingsDialog(
        settings=JsonSettings(tmp_path / "settings.json"),
        tag_library=tag_library,
    )
    qtbot.addWidget(dialog)

    assert dialog.transformation_group.title() == "Transformation"
    assert dialog.library_group.title() == "Library"
    assert "2 tags" in dialog.library_info_label.text()
    assert f"{library_path.stat().st_size} B" in dialog.library_info_label.text()
    assert str(library_path) in dialog.library_info_label.text()
    assert dialog.delete_library_button.isEnabled()
    assert dialog.transformation_group.isAncestorOf(
        dialog.underscores_checkbox
    )
    assert dialog.transformation_group.isAncestorOf(
        dialog.parentheses_checkbox
    )
    for checkbox in (
        dialog.underscores_checkbox,
        dialog.parentheses_checkbox,
    ):
        assert "width: 12px" in checkbox.styleSheet()
        assert "height: 12px" in checkbox.styleSheet()
    assert not isinstance(dialog.tag_library_page, DownloadTagsDialog)
    assert dialog.tag_library_dialog is None

    dialog.manage_tag_library_button.click()

    manager = dialog.tag_library_dialog
    assert isinstance(manager, DownloadTagsDialog)
    assert manager.isVisible()
    assert manager.destination == library_path
    assert not hasattr(manager, "existing_library_label")
    assert not hasattr(manager, "delete_button")
    manager.reject()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    dialog.delete_library_button.click()

    assert not library_path.exists()
    assert "No local Danbooru tag library" in dialog.library_info_label.text()
    assert not dialog.delete_library_button.isEnabled()
    assert tag_library.suggestions("red") == []


def test_settings_tree_stages_proxy_until_applied(qtbot, tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings = JsonSettings(settings_path)
    settings.setValue(PROXY_MODE_SETTING, "custom")
    settings.setValue(PROXY_SETTING, "http://old-proxy:8080")
    settings.sync()
    dialog = SettingsDialog(settings=settings)
    qtbot.addWidget(dialog)

    assert isinstance(dialog.navigation_tree, QTreeWidget)
    top_level_items = [
        dialog.navigation_tree.topLevelItem(index)
        for index in range(dialog.navigation_tree.topLevelItemCount())
    ]
    assert [
        item.text(0) for item in top_level_items if item is not None
    ] == ["Models", "Tag Library", "Network"]
    assert dialog.network_item.childCount() == 1
    assert dialog.network_item.child(0).text(0) == "Proxy"
    assert dialog.network_item.isExpanded()
    assert not hasattr(dialog.tag_library_page, "proxy_input")

    dialog.manage_tag_library_button.click()
    manager = dialog.tag_library_dialog
    assert manager is not None
    assert manager.proxy == "http://old-proxy:8080"
    manager.reject()

    dialog.navigation_tree.setCurrentItem(dialog.proxy_item)
    assert dialog.pages.currentWidget() is dialog.proxy_page
    assert dialog.proxy_server_group.title() == "Proxy server"
    assert dialog.custom_proxy_radio.isChecked()
    assert dialog.proxy_input.isEnabled()
    assert dialog.proxy_input.text() == "http://old-proxy:8080"

    dialog.proxy_input.setText("  http://new-proxy:3128  ")

    assert dialog.apply_button.isEnabled()
    assert settings.value(PROXY_SETTING, type=str) == "http://old-proxy:8080"
    persisted = JsonSettings(settings_path)
    assert persisted.value(PROXY_SETTING, type=str) == "http://old-proxy:8080"

    dialog.apply_button.click()

    assert not dialog.apply_button.isEnabled()
    assert settings.value(PROXY_MODE_SETTING, type=str) == "custom"
    assert settings.value(PROXY_SETTING, type=str) == "http://new-proxy:3128"
    persisted = JsonSettings(settings_path)
    assert persisted.value(PROXY_SETTING, type=str) == "http://new-proxy:3128"

    dialog.manage_tag_library_button.click()
    manager = dialog.tag_library_dialog
    assert manager is not None
    assert manager.proxy == "http://new-proxy:3128"
    manager.reject()


def test_proxy_url_without_mode_defaults_to_no_proxy(
    qtbot, tmp_path: Path
) -> None:
    settings = JsonSettings(tmp_path / "settings.json")
    settings.setValue(PROXY_SETTING, "http://old-proxy:8080")
    dialog = SettingsDialog(settings=settings)
    qtbot.addWidget(dialog)

    assert dialog.no_proxy_radio.isChecked()
    assert not dialog.proxy_input.isEnabled()

    dialog.manage_tag_library_button.click()
    manager = dialog.tag_library_dialog
    assert manager is not None
    assert manager.proxy == ""
    manager.reject()


def test_proxy_page_supports_none_system_and_custom_modes(
    qtbot, tmp_path: Path
) -> None:
    settings_path = tmp_path / "settings.json"
    settings = JsonSettings(settings_path)
    dialog = SettingsDialog(settings=settings)
    qtbot.addWidget(dialog)

    assert dialog.no_proxy_radio.text() == "No proxy"
    assert dialog.system_proxy_radio.text() == "Use system proxy"
    assert dialog.custom_proxy_radio.text() == "Custom proxy"
    assert dialog.no_proxy_radio.isChecked()
    assert not dialog.proxy_input.isEnabled()

    dialog.system_proxy_radio.click()

    assert dialog.system_proxy_radio.isChecked()
    assert not dialog.proxy_input.isEnabled()
    dialog.apply_button.click()
    assert settings.value(PROXY_MODE_SETTING, type=str) == "system"

    dialog.manage_tag_library_button.click()
    manager = dialog.tag_library_dialog
    assert manager is not None
    assert manager.proxy is None
    manager.reject()

    dialog.custom_proxy_radio.click()
    dialog.proxy_input.setText("http://localhost:7890")

    assert dialog.custom_proxy_radio.isChecked()
    assert dialog.proxy_input.isEnabled()
    dialog.apply_button.click()
    assert settings.value(PROXY_MODE_SETTING, type=str) == "custom"

    dialog.manage_tag_library_button.click()
    manager = dialog.tag_library_dialog
    assert manager is not None
    assert manager.proxy == "http://localhost:7890"
    manager.reject()

    dialog.no_proxy_radio.click()

    assert dialog.no_proxy_radio.isChecked()
    assert not dialog.proxy_input.isEnabled()
    dialog.apply_button.click()
    assert settings.value(PROXY_MODE_SETTING, type=str) == "none"

    dialog.manage_tag_library_button.click()
    manager = dialog.tag_library_dialog
    assert manager is not None
    assert manager.proxy == ""
    manager.reject()


def test_main_window_loads_folder_and_edits_current_tags(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "sample.png")
    (tmp_path / "sample.txt").write_text("dog, cat\n", encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)

    window._load_directory(tmp_path, show_issues=False)

    assert window.catalog.rowCount() == 1
    assert window.add_tag_button.text() == "+"
    assert window.add_tag_button.width() == 34
    assert window.image_list.currentIndex().row() == 0
    assert [window.tag_list.item(i).text() for i in range(window.tag_list.count())] == [
        "dog",
        "cat",
    ]
    assert window.windowTitle() == f"{tmp_path.name} - Image Tagger"
    assert window.bulk_operation_action.isEnabled()

    qtbot.waitUntil(lambda: "32 × 24 px" in window.image_info_label.text())
    image_info = window.image_info_label.text()
    assert "sample.png" in image_info
    assert "PNG" in image_info
    assert image_info.endswith(" B")

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


def test_archive_action_compresses_open_folder_without_hierarchy(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    destination = tmp_path / "archive.zip"
    nested.mkdir(parents=True)
    create_png(nested / "sample.png")
    (nested / "sample.txt").write_bytes(b"cat\n")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(source, show_issues=False)
    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getSaveFileName",
        lambda *_args: (str(destination), "Zip archives (*.zip)"),
    )

    assert window.archive_action.text() == "Archive..."
    assert window.archive_action.isEnabled()
    window.archive_action.trigger()

    assert window._archive_dialog is not None
    assert window._archive_dialog.isVisible()
    qtbot.waitUntil(lambda: destination.exists())
    qtbot.waitUntil(lambda: window._archive_dialog is None)
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == ["sample.png", "sample.txt"]
        assert archive.read("sample.txt") == b"cat\n"
    assert "Archived 1 image/tag pair" in window.statusBar().currentMessage()


def test_archive_progress_window_shows_current_file(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    create_png(source / "sample.png")
    (source / "sample.txt").write_bytes(b"cat\n")
    destination = tmp_path / "archive.zip"
    release_worker = threading.Event()

    def fake_archive_entries(entries, archive_path, progress):
        progress(1, 2, "sample.png")
        release_worker.wait(timeout=5)
        progress(2, 2, "sample.txt")
        return ArchiveResult([("sample.png", "sample.txt")])

    monkeypatch.setattr(archive_module, "archive_entries", fake_archive_entries)
    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getSaveFileName",
        lambda *_args: (str(destination), "Zip archives (*.zip)"),
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(source, show_issues=False)

    window.archive_action.trigger()
    try:
        qtbot.waitUntil(
            lambda: window._archive_dialog is not None
            and window._archive_dialog.current_file_label.text()
            == "Archiving: sample.png"
        )
        assert window._archive_dialog is not None
        assert window._archive_dialog.progress_bar.value() == 1
        assert window._archive_dialog.progress_bar.maximum() == 2
        assert not window.archive_action.isEnabled()
    finally:
        release_worker.set()

    qtbot.waitUntil(lambda: window._archive_dialog is None)


def test_image_context_delete_moves_image_and_tag_to_trash(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    for name in ["first", "second"]:
        create_png(tmp_path / f"{name}.png")
        (tmp_path / f"{name}.txt").write_text("cat\n", encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    moved: list[Path] = []

    def fake_move_to_trash(path: Path) -> bool:
        moved.append(path)
        path.unlink()
        return True

    monkeypatch.setattr(main_window_module, "move_to_trash", fake_move_to_trash)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    assert (
        window.image_list.contextMenuPolicy()
        == Qt.ContextMenuPolicy.CustomContextMenu
    )
    window._delete_current_image_and_tag()

    assert moved == [tmp_path / "first.png", tmp_path / "first.txt"]
    assert not (tmp_path / "first.png").exists()
    assert not (tmp_path / "first.txt").exists()
    assert window.catalog.image_count == 1
    current = window._current_entry()
    assert current is not None
    assert current.image_path == tmp_path / "second.png"


def test_move_to_trash_uses_qfile_instance_api(monkeypatch, tmp_path: Path) -> None:
    opened: list[str] = []

    class FakeQFile:
        def __init__(self, path: str) -> None:
            opened.append(path)

        def moveToTrash(self) -> bool:
            return True

    monkeypatch.setattr(main_window_module, "QFile", FakeQFile)
    path = tmp_path / "sample.png"

    assert main_window_module.move_to_trash(path)
    assert opened == [str(path)]


def test_image_context_delete_can_be_cancelled(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    image_path = tmp_path / "sample.png"
    tag_path = tmp_path / "sample.txt"
    create_png(image_path)
    tag_path.write_text("cat\n", encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Cancel,
    )
    monkeypatch.setattr(
        main_window_module,
        "move_to_trash",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("cancelled deletion must not move files")
        ),
    )

    window._delete_current_image_and_tag()

    assert image_path.exists()
    assert tag_path.exists()


def test_image_list_groups_images_by_subfolder(qtbot, tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    deep = nested / "deep"
    nested.mkdir()
    deep.mkdir()
    create_png(tmp_path / "root.png")
    create_png(nested / "child.png")
    create_png(deep / "grandchild.png")
    window = MainWindow()
    qtbot.addWidget(window)

    window._load_directory(tmp_path, show_issues=False)

    assert window.catalog.rowCount() == 2
    nested_index = window.catalog.index(0, 0)
    assert window.catalog.data(nested_index) == "nested"
    assert window.catalog.entry_for_index(nested_index) is None
    assert window.catalog.rowCount(nested_index) == 2
    assert window.catalog.data(window.catalog.index(0, 0, nested_index)) == "deep"
    assert window.catalog.data(
        window.catalog.index(1, 0, nested_index)
    ).startswith("child.png")
    assert window.catalog.data(window.catalog.index(1, 0)).startswith("root.png")
    assert window.catalog.group_for_row(0) == "Root folder"
    assert window.catalog.group_for_row(1) == "nested"
    assert window.catalog.group_for_row(2) == "nested/deep"

    window.image_list.collapse(nested_index)
    assert not window.image_list.isExpanded(nested_index)
    window.image_list.expand(nested_index)
    assert window.image_list.isExpanded(nested_index)
    window.next_action.trigger()
    current = window._current_entry()
    assert current is not None
    assert current.image_path == nested / "child.png"
    window.next_action.trigger()
    current = window._current_entry()
    assert current is not None
    assert current.image_path == deep / "grandchild.png"
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
    assert window.image_info_label.text() == "No image selected"
    assert window.windowTitle() == "Image Tagger"
    assert window.statusBar().currentMessage() == ""
    assert not window.close_folder_action.isEnabled()
    assert not window.rescan_action.isEnabled()
    assert not window.archive_action.isEnabled()
    assert not window.search_input.isEnabled()
    assert not window.tag_input.isEnabled()
    assert not window.bulk_operation_action.isEnabled()


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


def test_global_tag_search_shows_matching_tag_counts(qtbot, tmp_path: Path) -> None:
    entries = [
        ImageEntry(tmp_path / "one.png", tmp_path / "one.txt", ["cat", "dog"]),
        ImageEntry(
            tmp_path / "two.png",
            tmp_path / "two.txt",
            ["cat", "cathedral"],
        ),
    ]
    dialog = GlobalTagSearchDialog(entries)
    qtbot.addWidget(dialog)

    dialog.pattern_input.setText("cat*")
    dialog.search()

    tag_header = dialog.results.horizontalHeaderItem(0)
    count_header = dialog.results.horizontalHeaderItem(1)
    assert tag_header is not None
    assert count_header is not None
    assert tag_header.text() == "Tag name"
    assert count_header.text() == "Tag count"
    assert dialog.count_label.text() == (
        "2 matching tag(s) across 3 image occurrence(s)."
    )
    assert dialog.results.rowCount() == 2
    values: list[tuple[str, str]] = []
    for row in range(dialog.results.rowCount()):
        tag_item = dialog.results.item(row, 0)
        count_item = dialog.results.item(row, 1)
        assert tag_item is not None
        assert count_item is not None
        values.append((tag_item.text(), count_item.text()))
    assert values == [("cat", "2"), ("cathedral", "1")]

    dialog.pattern_input.setText("dog")
    dialog.search()

    tag_header = dialog.results.horizontalHeaderItem(0)
    count_header = dialog.results.horizontalHeaderItem(1)
    assert tag_header is not None
    assert count_header is not None
    assert tag_header.text() == "Tag name"
    assert count_header.text() == "Tag count"


def test_global_tag_search_copies_selected_table_rows(qtbot, tmp_path: Path) -> None:
    entries = [
        ImageEntry(tmp_path / "one.png", tmp_path / "one.txt", ["cat", "dog"]),
        ImageEntry(tmp_path / "two.png", tmp_path / "two.txt", ["cat", "bird"]),
    ]
    dialog = GlobalTagSearchDialog(entries)
    qtbot.addWidget(dialog)
    dialog.pattern_input.setText("*")
    dialog.search()

    dialog.results.clearSelection()
    for row in (0, 2):
        dialog.results.selectionModel().select(
            dialog.results.model().index(row, 0),
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )
    dialog._copy_selected_tags()

    assert QGuiApplication.clipboard().text() == "bird, dog"


def test_global_search_action_requires_an_open_folder(qtbot, tmp_path: Path) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert not window.global_search_action.isEnabled()

    create_png(tmp_path / "sample.png")
    window._load_directory(tmp_path, show_issues=False)

    assert window.global_search_action.isEnabled()


def test_complex_filter_is_modeless_and_keeps_main_window_available(
    qtbot, tmp_path: Path
) -> None:
    create_png(tmp_path / "sample.png")
    (tmp_path / "sample.txt").write_text("cat\n", encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)

    window._open_complex_filter()
    dialog = window._complex_filter_dialog
    assert dialog is not None
    assert not dialog.isModal()
    assert dialog.windowModality() == Qt.WindowModality.NonModal
    assert window.image_list.isEnabled()
    window._select_row(0)
    assert window._current_entry() is not None

    window._open_complex_filter()
    assert window._complex_filter_dialog is dialog


def test_complex_filter_double_click_selects_image_in_main_window(
    qtbot, tmp_path: Path
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    for name, folder in [("first.png", tmp_path), ("second.png", nested)]:
        create_png(folder / name)
        (folder / f"{Path(name).stem}.txt").write_text(
            "cat\n", encoding="utf-8"
        )

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    window._open_complex_filter()
    dialog = window._complex_filter_dialog
    assert dialog is not None

    dialog.run_filter()
    result_item = dialog.results.item(1, 0)
    assert result_item is not None
    dialog.results.itemDoubleClicked.emit(result_item)

    current = window._current_entry()
    assert current is not None
    assert current.image_path == nested / "second.png"


def test_normalize_applies_to_all_images_after_confirmation(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    for name, tags in {
        "first": "zebra, apple\n",
        "second": "night, bird\n",
    }.items():
        create_png(tmp_path / f"{name}.png")
        (tmp_path / f"{name}.txt").write_text(
            tags, encoding="utf-8", newline="\n"
        )

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    window._select_row(1)
    assert TagOperation.NORMALIZE not in window.folder_tag_actions
    assert window.normalize_action.isEnabled()
    confirmations: list[str] = []

    def cancel(_parent, _title, message, *_args):
        confirmations.append(message)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", cancel)
    window.normalize_action.trigger()

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == (
        "zebra, apple\n"
    )
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == (
        "night, bird\n"
    )
    assert "all 2 editable image(s)" in confirmations[0]
    assert "2 sidecar file(s) will change" in confirmations[0]

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    window.normalize_action.trigger()

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == (
        "apple, zebra\n"
    )
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == (
        "bird, night\n"
    )
    assert window.image_list.currentIndex().row() == 1
    assert window.statusBar().currentMessage() == "Normalized tags in 2 file(s)."


def test_dropped_folder_replaces_open_folder(qtbot, tmp_path: Path) -> None:
    first_folder = tmp_path / "first"
    second_folder = tmp_path / "second"
    first_folder.mkdir()
    second_folder.mkdir()
    create_png(first_folder / "first.png")
    create_png(second_folder / "second.png")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(first_folder))])
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
    assert window.directory == first_folder
    assert window.catalog.rowCount() == 1

    window.tag_input.setText("stale tag")
    window.search_input.setText("stale search")
    replacement_mime_data = QMimeData()
    replacement_mime_data.setUrls([QUrl.fromLocalFile(str(second_folder))])
    second_drag = QDragEnterEvent(
        QPoint(20, 20),
        Qt.DropAction.CopyAction,
        replacement_mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(second_drag)
    assert second_drag.isAccepted()

    second_drop = QDropEvent(
        QPointF(20, 20),
        Qt.DropAction.CopyAction,
        replacement_mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(second_drop)

    assert second_drop.isAccepted()
    assert window.directory == second_folder
    assert window.catalog.rowCount() == 1
    assert window.catalog.entries[0].image_path == second_folder / "second.png"
    assert window.tag_input.text() == ""
    assert window.search_input.text() == ""


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


def test_main_tag_deletion_requires_confirmation_from_button_and_context_menu(
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
    confirmations: list[tuple[str, str]] = []
    answers = iter(
        [
            QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        ]
    )

    def confirm(_parent, title, message, *_args):
        confirmations.append((title, message))
        return next(answers)

    monkeypatch.setattr(QMessageBox, "question", confirm)

    window.delete_tag_button.click()
    assert tag_path.read_text(encoding="utf-8") == "dog, cat, bird\n"

    context_menu = window._create_tag_context_menu()
    assert context_menu.actions()[1].text() == "Delete Selected Tags"
    context_menu.actions()[1].trigger()

    assert tag_path.read_text(encoding="utf-8") == "cat\n"
    assert len(confirmations) == 2
    assert all(title == "Delete Selected Tags?" for title, _ in confirmations)
    assert all("2 selected tag(s)" in message for _, message in confirmations)
    assert all("dog, bird" in message for _, message in confirmations)
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
    assert tag_path.read_bytes() == b"cat\n"
    assert dialog.finish_button.isEnabled()

    dialog._finish()
    assert tag_path.read_bytes() == b"cat, dog\n"


def test_toggle_traversal_checks_existing_tags_and_hides_apply_all(
    qtbot, tmp_path: Path
) -> None:
    image_path = tmp_path / "sample.png"
    tag_path = tmp_path / "sample.txt"
    create_png(image_path)
    tag_path.write_bytes(b"cat, bird\n")
    entry = ImageEntry(image_path, tag_path, ["cat", "bird"], b"cat, bird\n")

    dialog = TraversalDialog([entry], TagOperation.TOGGLE, ["cat", "dog"])
    qtbot.addWidget(dialog)

    checked = [
        dialog.choices.item(row).text()
        for row in range(dialog.choices.count())
        if dialog.choices.item(row).checkState() == Qt.CheckState.Checked
    ]
    assert checked == ["cat"]
    assert not dialog.apply_all_button.isVisible()
    assert not dialog.apply_all_button.isEnabled()


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
    assert dialog.session.staged[0] == ("cat", "night")
    qtbot.keyClick(dialog.choices, Qt.Key.Key_Left)
    assert dialog.session.current_index == 0
    assert dialog.choices.item(1).checkState() == Qt.CheckState.Checked
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


def test_traversal_temporary_input_is_consumed_or_cleared_on_navigation(
    qtbot, tmp_path: Path
) -> None:
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
    assert dialog.temporary_add_button.isEnabled()
    assert dialog.result_tags.toPlainText() == "cat"
    dialog.temporary_add_button.click()
    assert dialog.temporary_input.text() == ""
    assert dialog.result_tags.toPlainText() == "cat, image_only, temporary"
    assert dialog.session.extra_tags_for() == ["temporary", "image_only"]
    dialog.choices.item(0).setCheckState(Qt.CheckState.Checked)
    dialog.temporary_input.setText("discard_me")
    dialog._next()

    assert dialog.session.current_index == 1
    assert dialog.temporary_input.text() == ""
    assert dialog.session.staged[0] == (
        "base",
        "cat",
        "image_only",
        "temporary",
    )
    dialog._back()
    assert dialog.temporary_input.text() == ""
    assert dialog.choices.item(0).checkState() == Qt.CheckState.Checked
    assert dialog.session.extra_tags_for() == ["temporary", "image_only"]


def test_traversal_finish_early_commits_applied_and_skips_remaining(
    qtbot, tmp_path: Path
) -> None:
    entries: list[ImageEntry] = []
    for name in ["first", "second", "third"]:
        image_path = tmp_path / f"{name}.png"
        tag_path = tmp_path / f"{name}.txt"
        create_png(image_path)
        tag_path.write_bytes(b"cat\n")
        entries.append(ImageEntry(image_path, tag_path, ["cat"], b"cat\n"))

    dialog = TraversalDialog(entries, TagOperation.ADD, ["dog"])
    qtbot.addWidget(dialog)
    assert dialog.finish_button.isEnabled()
    assert dialog.next_button.text() == "Next"

    dialog.choices.item(0).setCheckState(Qt.CheckState.Checked)
    assert dialog.session.current_index == 0
    dialog._next()
    assert dialog.session.current_index == 1
    assert dialog.finish_button.isEnabled()
    dialog._finish()

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "cat, dog\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "cat\n"
    assert (tmp_path / "third.txt").read_text(encoding="utf-8") == "cat\n"


def test_traversal_folder_tree_combines_checked_subtrees(
    qtbot, tmp_path: Path
) -> None:
    nested = tmp_path / "nested"
    deep = nested / "deep"
    other = tmp_path / "other"
    deep.mkdir(parents=True)
    other.mkdir()
    entries: list[ImageEntry] = []
    for image_path in [
        tmp_path / "root.png",
        nested / "child.png",
        deep / "grandchild.png",
        other / "other.png",
    ]:
        create_png(image_path)
        tag_path = image_path.with_suffix(".txt")
        tag_path.write_text("cat\n", encoding="utf-8")
        entries.append(ImageEntry(image_path, tag_path, ["cat"], b"cat\n"))

    dialog = TraversalDialog(
        entries,
        TagOperation.ADD,
        ["dog"],
        root_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.folder_setup.isVisibleTo(dialog)
    assert not dialog.traversal_widget.isVisibleTo(dialog)
    assert dialog.tag_input.text() == "dog"
    assert dialog.focusWidget() is dialog.tag_input
    assert dialog.tag_input.selectedText() == "dog"
    root_item = dialog.folder_tree.topLevelItem(0)
    assert root_item is not None
    assert root_item.text(0) == tmp_path.name
    assert root_item.childCount() == 3
    nested_item = None
    for index in range(root_item.childCount()):
        child = root_item.child(index)
        if child is not None and child.text(0) == "nested":
            nested_item = child
            break
    assert nested_item is not None
    assert nested_item.childCount() == 2
    deep_item = nested_item.child(0)
    assert deep_item is not None
    assert deep_item.text(0) == "deep"
    child_image = nested_item.child(1)
    assert child_image is not None
    assert child_image.text(0) == "child.png"
    other_item = None
    for index in range(root_item.childCount()):
        child = root_item.child(index)
        if child is not None and child.text(0) == "other":
            other_item = child
            break
    assert other_item is not None

    assert root_item.checkState(0) == Qt.CheckState.Checked
    assert nested_item.checkState(0) == Qt.CheckState.Checked
    assert deep_item.checkState(0) == Qt.CheckState.Checked
    root_item.setCheckState(0, Qt.CheckState.Unchecked)
    nested_item.setCheckState(0, Qt.CheckState.Checked)
    assert deep_item.checkState(0) == Qt.CheckState.Checked
    assert child_image.checkState(0) == Qt.CheckState.Checked
    child_image.setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog.folder_selection_label.text() == (
        "1 matching image(s) will be included."
    )
    child_image.setCheckState(0, Qt.CheckState.Checked)
    assert dialog.folder_selection_label.text() == (
        "2 matching image(s) will be included."
    )
    other_item.setCheckState(0, Qt.CheckState.Checked)
    assert root_item.checkState(0) == Qt.CheckState.PartiallyChecked
    assert dialog.folder_selection_label.text() == (
        "3 matching image(s) will be included."
    )
    dialog.start_button.click()

    assert dialog._started
    assert not dialog.folder_setup.isVisibleTo(dialog)
    assert dialog.traversal_widget.isVisibleTo(dialog)
    assert [item.image_path for item in dialog.session.items] == [
        nested / "child.png",
        deep / "grandchild.png",
        other / "other.png",
    ]


def test_review_keyboard_shortcuts_keep_delete_and_navigate(
    qtbot, tmp_path: Path
) -> None:
    entries: list[ImageEntry] = []
    for name, tags in {"first": ["cat", "dog"], "second": ["bird"]}.items():
        image_path = tmp_path / f"{name}.png"
        tag_path = tmp_path / f"{name}.txt"
        create_png(image_path)
        source = (", ".join(tags) + "\n").encode()
        tag_path.write_bytes(source)
        entries.append(ImageEntry(image_path, tag_path, tags, source))

    dialog = ReviewDialog(entries)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.right_splitter.orientation() == Qt.Orientation.Vertical
    assert dialog.right_splitter.widget(0) is dialog.tag_panel
    assert dialog.right_splitter.widget(1) is dialog.controls_panel
    assert dialog.tag_label.minimumHeight() == dialog.tag_label.maximumHeight()
    assert dialog.tag_label.height() >= dialog.tag_label.fontMetrics().lineSpacing() * 3
    assert dialog.tag_status_list.minimumHeight() == 150
    assert dialog.tag_status_list.maximumHeight() == 150
    decision_group = dialog.keep_button.parentWidget()
    navigation_group = dialog.back_button.parentWidget()
    session_group = dialog.finish_button.parentWidget()
    assert isinstance(decision_group, QGroupBox)
    assert isinstance(navigation_group, QGroupBox)
    assert isinstance(session_group, QGroupBox)
    assert decision_group.title() == "Tag decision"
    assert navigation_group.title() == "Navigation"
    assert session_group.title() == "Review session"
    assert dialog.session.current_tag == "cat"
    assert [dialog.tag_status_list.item(row).text() for row in range(dialog.tag_status_list.count())] == [
        "[pending] cat",
        "[pending] dog",
    ]
    assert "#b42318" in dialog.delete_button.styleSheet()
    assert "Reviewed tags 0 of 3" in dialog.progress_label.text()
    qtbot.keyClick(dialog, Qt.Key.Key_Return)
    assert dialog.session.current_tag == "dog"
    assert [dialog.tag_status_list.item(row).text() for row in range(dialog.tag_status_list.count())] == [
        "[kept] cat",
        "[pending] dog",
    ]
    assert "Reviewed tags 1 of 3" in dialog.progress_label.text()
    qtbot.keyClick(dialog, Qt.Key.Key_Space)
    assert dialog.session.current_index == 1
    assert dialog.session.current_tag == "bird"
    qtbot.keyClick(dialog, Qt.Key.Key_Left)
    assert dialog.session.current_index == 0
    assert dialog.session.current_tag == "dog"
    qtbot.keyClick(dialog, Qt.Key.Key_Right)
    assert dialog.session.current_index == 1
    assert dialog.session.current_tag == "bird"
    assert dialog.session.working_tags[0] == ["cat"]


def test_review_extra_tag_input_accepts_spaces(qtbot, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    tag_path = tmp_path / "sample.txt"
    create_png(image_path)
    tag_path.write_bytes(b"cat\n")
    dialog = ReviewDialog(
        [ImageEntry(image_path, tag_path, ["cat"], b"cat\n")]
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.temporary_input.setFocus()

    qtbot.keyClicks(dialog.temporary_input, "two words")

    assert dialog.temporary_input.text() == "two words"
    assert dialog.session.current_tags == ["cat"]


def test_review_discard_button_closes_without_writing(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    image_path = tmp_path / "sample.png"
    tag_path = tmp_path / "sample.txt"
    create_png(image_path)
    tag_path.write_bytes(b"cat\n")
    entry = ImageEntry(image_path, tag_path, ["cat"], b"cat\n")
    dialog = ReviewDialog([entry])
    qtbot.addWidget(dialog)

    dialog._delete()
    assert dialog.session.has_changes
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Discard,
    )
    dialog.discard_button.click()

    assert dialog.result() == ReviewDialog.DialogCode.Rejected
    assert tag_path.read_bytes() == b"cat\n"


def test_complex_filter_executes_check_and_shows_matching_images(
    qtbot, tmp_path: Path
) -> None:
    entries: list[ImageEntry] = []
    for name, tags in {"cat.png": ["cat"], "dog.png": ["dog"]}.items():
        image_path = tmp_path / name
        tag_path = tmp_path / f"{Path(name).stem}.txt"
        create_png(image_path)
        source = (", ".join(tags) + "\n").encode()
        tag_path.write_bytes(source)
        entries.append(ImageEntry(image_path, tag_path, tags, source))

    dialog = ComplexFilterDialog(entries)
    qtbot.addWidget(dialog)
    assert dialog.code_input.tabStopDistance() == (
        dialog.code_input.fontMetrics().horizontalAdvance(" ") * 4
    )
    dialog.code_input.setPlainText(
        "def check(fn: str, tags: set[str]) -> bool:\n"
        "    return fn.endswith('.png') and 'cat' in tags\n"
    )
    dialog.run_filter()

    assert dialog.results.rowCount() == 1
    image_item = dialog.results.item(0, 0)
    assert image_item is not None
    assert image_item.text() == "cat.png"
    assert dialog.result_label.text() == "1 matching image(s) out of 2."
    assert dialog.error_label.isHidden()


def test_complex_filter_reports_missing_check_function(qtbot) -> None:
    dialog = ComplexFilterDialog([])
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.code_input.setPlainText("value = True")
    dialog.run_filter()

    assert dialog.results.rowCount() == 0
    assert dialog.error_label.isVisible()
    assert "must define check" in dialog.error_label.text()


def test_bulk_operation_folder_selection_cascades(qtbot, tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    create_png(tmp_path / "root.png")
    create_png(nested / "child.png")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)

    dialog = BulkOperationDialog(
        window.catalog.entries,
        root_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    assert dialog.pages.count() == 3
    assert dialog.pages.currentWidget() is dialog.selection_page
    assert dialog.code_input.tabStopDistance() == (
        dialog.code_input.fontMetrics().horizontalAdvance(" ") * 4
    )
    root_item = dialog.folder_tree.topLevelItem(0)
    assert root_item is not None
    assert len(dialog._checked_entries()) == 2

    nested_item = next(
        root_item.child(index)
        for index in range(root_item.childCount())
        if root_item.child(index).text(0) == "nested"
    )
    nested_item.setCheckState(0, Qt.CheckState.Unchecked)

    assert [entry.image_path.name for entry in dialog._checked_entries()] == [
        "root.png"
    ]
    assert root_item.checkState(0) == Qt.CheckState.PartiallyChecked
    assert dialog.folder_selection_label.text() == "1 image(s) selected."


def test_bulk_operation_runs_code_and_skips_unchanged_images(
    qtbot, tmp_path: Path
) -> None:
    for name, tags in {"cat.png": "cat\n", "dog.png": "dog\n"}.items():
        create_png(tmp_path / name)
        (tmp_path / f"{Path(name).stem}.txt").write_text(tags, encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    dialog = BulkOperationDialog(
        window.catalog.entries,
        root_directory=tmp_path,
    )
    qtbot.addWidget(dialog)

    dialog._show_code_page()
    dialog.code_input.setPlainText(
        "def process(fn: str, tags: set[str]) -> set[str]:\n"
        "    return tags | {'new'} if fn == 'cat.png' else tags\n"
    )
    dialog._run_code()

    assert dialog.pages.currentWidget() is dialog.approval_page
    assert len(dialog._changes) == 1
    assert dialog.current_change.entry.image_path.name == "cat.png"
    assert dialog.original_tags_input.toPlainText() == "cat"
    assert dialog.new_tags_input.toPlainText() == "cat, new"
    assert dialog.original_tags_input.maximumHeight() == 100
    assert dialog.new_tags_input.maximumHeight() == 100
    assert dialog.changes_text.toPlainText() == "[+] new"


def test_bulk_operation_reports_invalid_process_return(qtbot, tmp_path: Path) -> None:
    create_png(tmp_path / "sample.png")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    dialog = BulkOperationDialog(
        window.catalog.entries,
        root_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog._show_code_page()
    dialog.code_input.setPlainText(
        "def process(fn: str, tags: set[str]) -> set[str]:\n"
        "    return ['not', 'a', 'set']\n"
    )

    dialog._run_code()

    assert dialog.pages.currentWidget() is dialog.code_page
    assert dialog.code_error_label.isVisibleTo(dialog)
    assert "must return set[str]" in dialog.code_error_label.text()


def test_bulk_operation_confirms_and_skips_with_shortcuts(
    qtbot, tmp_path: Path
) -> None:
    for name, tags in {"first.png": "cat\n", "second.png": "dog\n"}.items():
        create_png(tmp_path / name)
        (tmp_path / f"{Path(name).stem}.txt").write_text(tags, encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    dialog = BulkOperationDialog(
        window.catalog.entries,
        root_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog._show_code_page()
    dialog.code_input.setPlainText(
        "def process(fn: str, tags: set[str]) -> set[str]:\n"
        "    return tags | {'processed'}\n"
    )
    dialog._run_code()
    dialog.new_tags_input.setPlainText("cat, edited")

    qtbot.keyClick(dialog.confirm_button, Qt.Key.Key_Return)
    assert dialog.current_change.entry.image_path.name == "second.png"
    qtbot.keyClick(dialog.confirm_button, Qt.Key.Key_Space)

    assert dialog.result() == BulkOperationDialog.DialogCode.Accepted
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == (
        "cat, edited\n"
    )
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "dog\n"


def test_bulk_operation_discard_returns_to_code_without_writing(
    qtbot, tmp_path: Path
) -> None:
    for name in ["first", "second"]:
        create_png(tmp_path / f"{name}.png")
        (tmp_path / f"{name}.txt").write_text("cat\n", encoding="utf-8")
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_directory(tmp_path, show_issues=False)
    dialog = BulkOperationDialog(
        window.catalog.entries,
        root_directory=tmp_path,
    )
    qtbot.addWidget(dialog)
    dialog._show_code_page()
    dialog.code_input.setPlainText(
        "def process(fn: str, tags: set[str]) -> set[str]:\n"
        "    return tags | {'processed'}\n"
    )
    dialog._run_code()
    dialog._confirm_current()
    assert dialog._approved

    dialog.discard_button.click()

    assert dialog.pages.currentWidget() is dialog.code_page
    assert not dialog._approved
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "cat\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "cat\n"


def test_review_temporary_tags_are_kept_and_consumed(
    qtbot, tmp_path: Path
) -> None:
    image_path = tmp_path / "sample.png"
    tag_path = tmp_path / "sample.txt"
    create_png(image_path)
    tag_path.write_bytes(b"cat\n")
    entry = ImageEntry(image_path, tag_path, ["cat"], b"cat\n")
    dialog = ReviewDialog([entry])
    qtbot.addWidget(dialog)

    dialog.temporary_input.setText("new, cat")
    assert dialog.temporary_add_button.isEnabled()
    dialog.temporary_add_button.click()

    assert dialog.temporary_input.text() == ""
    assert dialog.session.current_tags == ["cat", "new"]
    assert "new" in dialog.session.reviewed_tags[0]
    assert "[kept] new" in [
        dialog.tag_status_list.item(row).text()
        for row in range(dialog.tag_status_list.count())
    ]
    assert tag_path.read_bytes() == b"cat\n"


def test_review_finish_confirms_total_tag_deletions(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    image_path = tmp_path / "sample.png"
    tag_path = tmp_path / "sample.txt"
    create_png(image_path)
    tag_path.write_bytes(b"cat, dog\n")
    entry = ImageEntry(image_path, tag_path, ["cat", "dog"], b"cat, dog\n")
    dialog = ReviewDialog([entry])
    qtbot.addWidget(dialog)
    dialog._delete()

    prompts: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, message, *_args: (
            prompts.append(message) or QMessageBox.StandardButton.Cancel
        ),
    )
    dialog._finish()

    assert "delete 1 tag(s)" in prompts[0]
    assert tag_path.read_bytes() == b"cat, dog\n"
