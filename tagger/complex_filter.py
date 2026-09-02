from __future__ import annotations

from collections.abc import Callable, Sequence
import os
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .domain import ImageEntry
from .python_syntax import PythonSyntaxHighlighter


DEFAULT_FILTER_CODE = '''def check(fn: str, tags: set[str]) -> bool:
    return True
'''


class ComplexFilterDialog(QDialog):
    image_activated = Signal(Path)

    def __init__(
        self,
        entries: Sequence[ImageEntry],
        parent=None,
        *,
        root_directory: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.entries = list(entries)
        self.root_directory = root_directory
        self.matches: list[ImageEntry] = []
        self.setWindowTitle("Complex Image Filter")
        self.resize(850, 680)

        self.code_input = QPlainTextEdit()
        self.code_input.setPlainText(DEFAULT_FILTER_CODE)
        self.code_input.setPlaceholderText(
            "Define check(fn: str, tags: set[str]) -> bool here."
        )
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.code_input.setFont(font)
        self.code_input.setTabStopDistance(
            self.code_input.fontMetrics().horizontalAdvance(" ") * 4
        )
        self.highlighter = PythonSyntaxHighlighter(self.code_input.document())

        self.run_button = QPushButton()
        self.run_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.run_button.setToolTip("Run filter")
        self.run_button.setAccessibleName("Run filter")
        self.run_button.clicked.connect(self.run_filter)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("QLabel { color: #b42318; }")
        self.error_label.hide()
        self.result_label = QLabel("Run the filter to see matching images.")
        self.result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.results = QTableWidget(0, 1)
        self.results.setHorizontalHeaderLabels(["Image"])
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.results.itemDoubleClicked.connect(self._activate_result)
        self.results.horizontalHeader().setStretchLastSection(True)
        self.results.horizontalHeader().setSectionResizeMode(
            0, self.results.horizontalHeader().ResizeMode.Stretch
        )

        code_buttons = QHBoxLayout()
        code_buttons.addStretch(1)
        code_buttons.addWidget(self.run_button)

        code_panel = QWidget()
        code_layout = QVBoxLayout(code_panel)
        code_layout.addLayout(code_buttons)
        code_layout.addWidget(self.code_input, 1)
        code_layout.addWidget(self.error_label)

        result_panel = QWidget()
        result_layout = QVBoxLayout(result_panel)
        result_layout.addWidget(self.result_label)
        result_layout.addWidget(self.results, 1)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(code_panel)
        self.splitter.addWidget(result_panel)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([560, 290])

        layout = QVBoxLayout(self)
        layout.addWidget(self.splitter)

    def run_filter(self) -> None:
        self.error_label.clear()
        self.error_label.hide()
        try:
            code = compile(self.code_input.toPlainText(), "<complex-filter>", "exec")
            namespace: dict[str, object] = {}
            exec(code, namespace)
        except Exception as exc:
            self._show_error(f"Could not compile or execute filter code: {exc}")
            return

        check = namespace.get("check")
        if not callable(check):
            self._show_error(
                "The filter code must define check(fn: str, tags: set[str]) -> bool."
            )
            return
        check_function = cast(Callable[[str, set[str]], object], check)

        matches: list[ImageEntry] = []
        for entry in self.entries:
            try:
                matched = check_function(entry.image_path.name, set(entry.tags))
            except Exception as exc:
                self._show_error(
                    f"check() failed for {entry.image_path.name}: {exc}"
                )
                return
            if not isinstance(matched, bool):
                self._show_error(
                    f"check() must return bool; it returned {type(matched).__name__} "
                    f"for {entry.image_path.name}."
                )
                return
            if matched:
                matches.append(entry)

        self.matches = matches
        self.result_label.setText(
            f"{len(matches)} matching image(s) out of {len(self.entries)}."
        )
        self.results.setRowCount(len(matches))
        for row, entry in enumerate(matches):
            self.results.setItem(
                row, 0, QTableWidgetItem(self._relative_path(entry.image_path))
            )

    def _activate_result(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if 0 <= row < len(self.matches):
            self.image_activated.emit(self.matches[row].image_path)

    def _relative_path(self, path: Path) -> str:
        root = self.root_directory
        if root is None and self.entries:
            try:
                root = Path(
                    os.path.commonpath(
                        [str(entry.image_path.parent) for entry in self.entries]
                    )
                )
            except ValueError:
                root = None
        if root is not None:
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                pass
        return path.name

    def _show_error(self, message: str) -> None:
        self.matches = []
        self.results.setRowCount(0)
        self.result_label.setText("Filter failed.")
        self.error_label.setText(message)
        self.error_label.show()
