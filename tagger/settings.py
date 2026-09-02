from __future__ import annotations

import base64
import json
import os
import tempfile
from typing import cast

from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QStyle,
    QStackedWidget,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .ai_tagger import (
    ModelManagementDialog,
    ai_dependencies_available,
    missing_ai_dependencies,
)
from .paths import SETTINGS_PATH, ensure_data_directory
from .tag_library import DownloadTagsDialog, TagLibrary


UNDERSCORES_SETTING = "tag_library/transform_underscores_to_spaces"
PARENTHESES_SETTING = "tag_library/escape_parentheses"


def _stabilize_checkbox(checkbox: QCheckBox) -> None:
    indicator_size = checkbox.style().pixelMetric(
        QStyle.PixelMetric.PM_IndicatorWidth, None, checkbox
    )
    checkbox.setStyleSheet(
        "QCheckBox::indicator { "
        f"width: {indicator_size}px; height: {indicator_size}px; "
        "}"
    )
    checkbox.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
    )
    checkbox.setFixedHeight(checkbox.sizeHint().height())


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
        self.setWindowTitle("Settings")
        self.resize(760, 520)

        self.tabs = QListWidget()
        self.tabs.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.tabs.setFixedWidth(140)
        self.pages = QStackedWidget()
        self.navigation_list = self.tabs
        self.stacked_widget = self.pages

        self.models_page = self._create_models_page()
        self.tag_library_page = self._create_tag_library_page()
        self.pages.addWidget(self.models_page)
        self.pages.addWidget(self.tag_library_page)
        self.tabs.addItems(["Models", "Tag Library"])
        self.tabs.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.tabs.setCurrentRow(0)

        content = QHBoxLayout()
        content.addWidget(self.tabs)
        content.addWidget(self.pages, 1)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(content)
        layout.addLayout(buttons)

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

        page = ModelManagementDialog(self)
        # The old management dialog is used as a page; Settings owns closing.
        page.setWindowFlags(Qt.WindowType.Widget)
        page.close_button.hide()
        return page

    def _create_tag_library_page(self) -> QWidget:
        if self.tag_library is None:
            page = DownloadTagsDialog(self)
        else:
            page = DownloadTagsDialog(
                self, destination=self.tag_library.csv_path
            )
        page.setWindowFlags(Qt.WindowType.Widget)
        page.close_button.hide()

        self.underscores_checkbox = QCheckBox(
            "Transform underscores into spaces"
        )
        self.parentheses_checkbox = QCheckBox(
            r"Add '\' before parentheses"
        )
        _stabilize_checkbox(self.underscores_checkbox)
        _stabilize_checkbox(self.parentheses_checkbox)
        self.underscores_checkbox.setChecked(
            cast(
                bool,
                self.settings.value(UNDERSCORES_SETTING, False, type=bool),
            )
        )
        self.parentheses_checkbox.setChecked(
            cast(
                bool,
                self.settings.value(PARENTHESES_SETTING, False, type=bool),
            )
        )
        self.underscores_checkbox.toggled.connect(
            self._transform_options_changed
        )
        self.parentheses_checkbox.toggled.connect(
            self._transform_options_changed
        )

        options = QWidget()
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(0, 8, 0, 0)
        options_layout.addWidget(QLabel("Tag transformation"))
        options_layout.addWidget(self.underscores_checkbox)
        options_layout.addWidget(self.parentheses_checkbox)

        # Insert options immediately before the action buttons in the page.
        page_layout = cast(QVBoxLayout, page.layout())
        if page_layout is None:
            raise RuntimeError("Tag library page has no layout")
        page_layout.insertWidget(page_layout.count() - 1, options)
        page.library_changed.connect(self._library_changed)
        return page

    def _transform_options_changed(self, _checked: bool) -> None:
        underscores = self.underscores_checkbox.isChecked()
        parentheses = self.parentheses_checkbox.isChecked()
        self.settings.setValue(UNDERSCORES_SETTING, underscores)
        self.settings.setValue(PARENTHESES_SETTING, parentheses)
        self.settings.sync()
        if self.tag_library is not None:
            self.tag_library.set_transform_options(
                underscores_to_spaces=underscores,
                escape_parentheses=parentheses,
            )

    def _library_changed(self, _path: str) -> None:
        if self.tag_library is not None:
            self.tag_library.reload_danbooru()

    def _busy(self) -> bool:
        return any(
            getattr(page, "_thread", None) is not None
            for page in (self.models_page, self.tag_library_page)
        )

    def accept(self) -> None:
        if not self._busy():
            super().accept()

    def reject(self) -> None:
        if not self._busy():
            super().reject()
