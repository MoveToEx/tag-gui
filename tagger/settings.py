from __future__ import annotations

from typing import cast

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QStackedWidget,
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


def create_app_settings() -> QSettings:
    ensure_data_directory()
    return QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: QSettings | None = None,
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
