from __future__ import annotations

import base64
import json
import os
import tempfile
from typing import cast

from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .ai_tagger import (
    ModelManagementDialog,
    ai_dependencies_available,
    missing_ai_dependencies,
)
from .paths import SETTINGS_PATH, ensure_data_directory
from .tag_library import (
    DEFAULT_TAG_LIBRARY_PATH,
    DownloadTagsDialog,
    TagLibrary,
    get_tag_library_file_info,
)


UNDERSCORES_SETTING = "tag_library/transform_underscores_to_spaces"
PARENTHESES_SETTING = "tag_library/escape_parentheses"
PROXY_SETTING = "network/http_proxy"
PROXY_MODE_SETTING = "network/proxy_mode"
NO_PROXY = "none"
SYSTEM_PROXY = "system"
CUSTOM_PROXY = "custom"
PROXY_MODES = {NO_PROXY, SYSTEM_PROXY, CUSTOM_PROXY}


def _stabilize_checkbox(checkbox: QCheckBox) -> None:
    checkbox.setStyleSheet(
        "QCheckBox::indicator { width: 12px; height: 12px; }"
    )
    checkbox.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
    )
    checkbox.setFixedHeight(checkbox.sizeHint().height())


def _format_byte_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class JsonSettings:
    """Small settings store backed by a JSON object."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._values: dict[str, object] = {}
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        if isinstance(data, dict):
            self._values = data

    def value(
        self,
        key: str,
        default: object = None,
        *,
        type: type | None = None,
    ) -> object:
        value = _decode_value(self._values.get(key, default))
        if type is None or value is None:
            return value
        if type is bool:
            if isinstance(value, str):
                return value.casefold() in {"1", "true", "yes", "on"}
            return bool(value)
        try:
            return type(value)
        except (TypeError, ValueError):
            return default

    def setValue(self, key: str, value: object) -> None:
        self._values[key] = _encode_value(value)

    def sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(
                file_descriptor, "w", encoding="utf-8", newline="\n"
            ) as stream:
                json.dump(self._values, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def fileName(self) -> str:
        return str(self.path)


def _proxy_preferences(settings: JsonSettings) -> tuple[str, str]:
    proxy_value = settings.value(PROXY_SETTING, "", type=str)
    proxy = proxy_value.strip() if isinstance(proxy_value, str) else ""
    mode_value = settings.value(PROXY_MODE_SETTING, None, type=str)
    mode = (
        mode_value
        if isinstance(mode_value, str) and mode_value in PROXY_MODES
        else NO_PROXY
    )
    return mode, proxy


def _resolved_proxy(mode: str, proxy: str) -> str | None:
    if mode == SYSTEM_PROXY:
        return None
    if mode == CUSTOM_PROXY:
        return proxy.strip()
    return ""


def get_download_proxy(settings: JsonSettings) -> str | None:
    """Return None for system discovery, empty for direct, or a proxy URL."""
    mode, proxy = _proxy_preferences(settings)
    return _resolved_proxy(mode, proxy)


def _encode_value(value: object) -> object:
    if isinstance(value, QByteArray):
        return {
            "__type__": "QByteArray",
            "value": base64.b64encode(value.data()).decode("ascii"),
        }
    if isinstance(value, dict):
        return {str(key): _encode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode_value(item) for item in value]
    return value


def _decode_value(value: object) -> object:
    if isinstance(value, dict):
        if value.get("__type__") == "QByteArray":
            encoded = value.get("value", "")
            if isinstance(encoded, str):
                return QByteArray.fromBase64(encoded.encode("ascii"))
        return {key: _decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    return value


def create_app_settings() -> JsonSettings:
    ensure_data_directory()
    return JsonSettings(SETTINGS_PATH)


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: JsonSettings | None = None,
        tag_library: TagLibrary | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings or create_app_settings()
        self.tag_library = tag_library
        self.tag_library_path = (
            tag_library.csv_path
            if tag_library is not None
            else DEFAULT_TAG_LIBRARY_PATH
        )
        self._applied_transform_options = (
            cast(
                bool,
                self.settings.value(UNDERSCORES_SETTING, False, type=bool),
            ),
            cast(
                bool,
                self.settings.value(PARENTHESES_SETTING, False, type=bool),
            ),
        )
        (
            self._applied_proxy_mode,
            self._applied_proxy_url,
        ) = _proxy_preferences(self.settings)
        self.setWindowTitle("Settings")
        self.resize(760, 520)

        self.navigation_tree = QTreeWidget()
        self.navigation_tree.setHeaderHidden(True)
        self.navigation_tree.setRootIsDecorated(True)
        self.navigation_tree.setSelectionMode(
            QTreeWidget.SelectionMode.SingleSelection
        )
        self.navigation_tree.setFixedWidth(160)
        self.tabs = self.navigation_tree
        self.pages = QStackedWidget()
        self.navigation_list = self.navigation_tree
        self.stacked_widget = self.pages

        self.models_page = self._create_models_page()
        self.tag_library_dialog: DownloadTagsDialog | None = None
        self.tag_library_page = self._create_tag_library_page()
        self.proxy_page = self._create_proxy_page()
        self.pages.addWidget(self.models_page)
        self.pages.addWidget(self.tag_library_page)
        self.pages.addWidget(self.proxy_page)

        self.models_item = self._add_navigation_item(
            "Models", self.models_page
        )
        self.tag_library_item = self._add_navigation_item(
            "Tag Library", self.tag_library_page
        )
        self.network_item = QTreeWidgetItem(["Network"])
        self.network_item.setFlags(
            self.network_item.flags() & ~Qt.ItemFlag.ItemIsSelectable
        )
        self.navigation_tree.addTopLevelItem(self.network_item)
        self.proxy_item = self._add_navigation_item(
            "Proxy", self.proxy_page, parent=self.network_item
        )
        self.network_item.setExpanded(True)
        self.navigation_tree.currentItemChanged.connect(
            self._navigation_changed
        )
        self.navigation_tree.setCurrentItem(self.models_item)

        content = QHBoxLayout()
        content.addWidget(self.navigation_tree)
        content.addWidget(self.pages, 1)

        self.ok_button = QPushButton("OK")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self._apply_and_accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.apply_button)

        layout = QVBoxLayout(self)
        layout.addLayout(content)
        layout.addLayout(buttons)

    def _add_navigation_item(
        self,
        label: str,
        page: QWidget,
        *,
        parent: QTreeWidgetItem | None = None,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label])
        item.setData(0, Qt.ItemDataRole.UserRole, self.pages.indexOf(page))
        if parent is None:
            self.navigation_tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _navigation_changed(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if current is None:
            return
        page_index = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(page_index, int):
            self.pages.setCurrentIndex(page_index)

    def _create_models_page(self) -> QWidget:
        if not ai_dependencies_available():
            page = QWidget()
            layout = QVBoxLayout(page)
            missing = ", ".join(missing_ai_dependencies())
            layout.addWidget(
                QLabel(
                    "AI tagging model management requires these dependencies: "
                    + missing
                )
            )
            layout.addStretch(1)
            return page

        page = ModelManagementDialog(
            self,
            proxy=_resolved_proxy(
                self._applied_proxy_mode, self._applied_proxy_url
            ),
        )
        # The old management dialog is used as a page; Settings owns closing.
        page.setWindowFlags(Qt.WindowType.Widget)
        page.close_button.hide()
        return page

    def _create_tag_library_page(self) -> QWidget:
        page = QWidget()
        self.library_group = QGroupBox("Library")
        self.library_info_label = QLabel()
        self.library_info_label.setWordWrap(True)
        self.library_info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.delete_library_button = QPushButton("Delete Tag Library")
        self.delete_library_button.setStyleSheet(
            "QPushButton { color: #b42318; } "
            "QPushButton:disabled { color: #d0d5dd; }"
        )
        self.delete_library_button.clicked.connect(self._delete_tag_library)
        self.manage_tag_library_button = QPushButton("Manage Tag Library...")
        self.manage_tag_library_button.clicked.connect(
            self._open_tag_library_dialog
        )

        library_buttons = QHBoxLayout()
        library_buttons.addWidget(self.delete_library_button)
        library_buttons.addStretch(1)
        library_buttons.addWidget(self.manage_tag_library_button)
        library_layout = QVBoxLayout(self.library_group)
        library_layout.addWidget(self.library_info_label)
        library_layout.addLayout(library_buttons)

        self.underscores_checkbox = QCheckBox(
            "Transform underscores into spaces"
        )
        self.parentheses_checkbox = QCheckBox(
            r"Add '\' before parentheses"
        )
        _stabilize_checkbox(self.underscores_checkbox)
        _stabilize_checkbox(self.parentheses_checkbox)
        underscores, parentheses = self._applied_transform_options
        self.underscores_checkbox.setChecked(underscores)
        self.parentheses_checkbox.setChecked(parentheses)
        self.underscores_checkbox.toggled.connect(
            self._transform_options_changed
        )
        self.parentheses_checkbox.toggled.connect(
            self._transform_options_changed
        )

        self.transformation_group = QGroupBox("Transformation")
        transformation_layout = QVBoxLayout(self.transformation_group)
        transformation_layout.addWidget(self.underscores_checkbox)
        transformation_layout.addWidget(self.parentheses_checkbox)

        page_layout = QVBoxLayout(page)
        page_layout.addWidget(self.library_group)
        page_layout.addWidget(self.transformation_group)
        page_layout.addStretch(1)
        self._refresh_library_info()
        return page

    def _refresh_library_info(self) -> None:
        try:
            info = get_tag_library_file_info(self.tag_library_path)
        except FileNotFoundError:
            self.library_info_label.setText(
                "No local Danbooru tag library is installed.\n"
                f"{self.tag_library_path}"
            )
            exists = False
        except (OSError, ValueError) as exc:
            self.library_info_label.setText(
                "The local Danbooru tag library could not be read.\n"
                f"{self.tag_library_path}\n{exc}"
            )
            exists = True
        else:
            modified = info.modified_at.strftime("%Y-%m-%d %H:%M")
            self.library_info_label.setText(
                f"{info.tag_count:,} tags, "
                f"{_format_byte_size(info.file_size)}, updated {modified}.\n"
                f"{self.tag_library_path}"
            )
            exists = True
        self.delete_library_button.setEnabled(exists)

    def _delete_tag_library(self) -> None:
        if not self.tag_library_path.exists():
            self._refresh_library_info()
            return
        answer = QMessageBox.question(
            self,
            "Delete Tag Library?",
            f"Delete the local tag library?\n\n{self.tag_library_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.tag_library_path.unlink()
        except OSError as exc:
            QMessageBox.critical(self, "Could Not Delete Tag Library", str(exc))
            return
        self._library_changed(str(self.tag_library_path))

    def _open_tag_library_dialog(self) -> None:
        if self.tag_library_dialog is not None:
            self.tag_library_dialog.raise_()
            self.tag_library_dialog.activateWindow()
            return

        proxy = _resolved_proxy(
            self._applied_proxy_mode, self._applied_proxy_url
        )
        dialog = DownloadTagsDialog(
            self,
            destination=self.tag_library_path,
            proxy=proxy,
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.library_changed.connect(self._library_changed)
        dialog.finished.connect(self._tag_library_dialog_finished)
        self.tag_library_dialog = dialog
        dialog.open()

    def _tag_library_dialog_finished(self, _result: int) -> None:
        self.tag_library_dialog = None

    def _create_proxy_page(self) -> QWidget:
        page = QWidget()
        self.proxy_server_group = QGroupBox("Proxy server")
        self.proxy_mode_group = QButtonGroup(page)
        self.no_proxy_radio = QRadioButton("No proxy")
        self.system_proxy_radio = QRadioButton("Use system proxy")
        self.custom_proxy_radio = QRadioButton("Custom proxy")
        for radio in (
            self.no_proxy_radio,
            self.system_proxy_radio,
            self.custom_proxy_radio,
        ):
            self.proxy_mode_group.addButton(radio)

        self.proxy_input = QLineEdit(self._applied_proxy_url)
        self.proxy_input.setPlaceholderText("http://127.0.0.1:7890")
        self.proxy_input.setClearButtonEnabled(True)
        selected_radio = {
            NO_PROXY: self.no_proxy_radio,
            SYSTEM_PROXY: self.system_proxy_radio,
            CUSTOM_PROXY: self.custom_proxy_radio,
        }[self._applied_proxy_mode]
        selected_radio.setChecked(True)
        self.proxy_input.setEnabled(self.custom_proxy_radio.isChecked())

        custom_proxy_row = QHBoxLayout()
        custom_proxy_row.setContentsMargins(0, 0, 0, 0)
        custom_proxy_row.addWidget(self.custom_proxy_radio)
        custom_proxy_row.addWidget(self.proxy_input, 1)
        group_layout = QVBoxLayout(self.proxy_server_group)
        group_layout.addWidget(self.no_proxy_radio)
        group_layout.addWidget(self.system_proxy_radio)
        group_layout.addLayout(custom_proxy_row)
        page_layout = QVBoxLayout(page)
        page_layout.addWidget(self.proxy_server_group)
        page_layout.addStretch(1)

        self.no_proxy_radio.toggled.connect(self._proxy_selection_changed)
        self.system_proxy_radio.toggled.connect(self._proxy_selection_changed)
        self.custom_proxy_radio.toggled.connect(self._proxy_selection_changed)
        self.proxy_input.textChanged.connect(self._settings_changed)
        return page

    def _proxy_selection_changed(self, _checked: bool) -> None:
        self.proxy_input.setEnabled(self.custom_proxy_radio.isChecked())
        self._settings_changed()

    def _transform_options_changed(self, _checked: bool) -> None:
        self._settings_changed()

    def _settings_changed(self, *_args: object) -> None:
        self.apply_button.setEnabled(
            self._transform_options() != self._applied_transform_options
            or self._proxy_preferences()
            != (self._applied_proxy_mode, self._applied_proxy_url)
        )

    def _transform_options(self) -> tuple[bool, bool]:
        return (
            self.underscores_checkbox.isChecked(),
            self.parentheses_checkbox.isChecked(),
        )

    def _proxy_preferences(self) -> tuple[str, str]:
        if self.system_proxy_radio.isChecked():
            mode = SYSTEM_PROXY
        elif self.custom_proxy_radio.isChecked():
            mode = CUSTOM_PROXY
        else:
            mode = NO_PROXY
        return mode, self.proxy_input.text().strip()

    def _apply(self) -> None:
        underscores = self.underscores_checkbox.isChecked()
        parentheses = self.parentheses_checkbox.isChecked()
        proxy_mode, proxy_url = self._proxy_preferences()
        proxy = _resolved_proxy(proxy_mode, proxy_url)
        self.settings.setValue(UNDERSCORES_SETTING, underscores)
        self.settings.setValue(PARENTHESES_SETTING, parentheses)
        self.settings.setValue(PROXY_MODE_SETTING, proxy_mode)
        self.settings.setValue(PROXY_SETTING, proxy_url)
        self.settings.sync()
        if self.tag_library is not None:
            self.tag_library.set_transform_options(
                underscores_to_spaces=underscores,
                escape_parentheses=parentheses,
            )
        if self.tag_library_dialog is not None:
            self.tag_library_dialog.set_proxy(proxy)
        if isinstance(self.models_page, ModelManagementDialog):
            self.models_page.set_proxy(proxy)
        self._applied_transform_options = (underscores, parentheses)
        self._applied_proxy_mode = proxy_mode
        self._applied_proxy_url = proxy_url
        self.apply_button.setEnabled(False)

    def _apply_and_accept(self) -> None:
        if self._busy():
            return
        self._apply()
        super().accept()

    def _library_changed(self, _path: str) -> None:
        if self.tag_library is not None:
            self.tag_library.reload_danbooru()
        self._refresh_library_info()

    def _busy(self) -> bool:
        pages = [self.models_page]
        if self.tag_library_dialog is not None:
            pages.append(self.tag_library_dialog)
        return any(
            getattr(page, "_thread", None) is not None for page in pages
        )

    def accept(self) -> None:
        if not self._busy():
            super().accept()

    def reject(self) -> None:
        if not self._busy():
            super().reject()
