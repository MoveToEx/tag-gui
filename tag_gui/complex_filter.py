from __future__ import annotations

from collections.abc import Callable, Sequence
import os
from pathlib import Path
from typing import cast, override

from PySide6.QtCore import Qt, QRegularExpression, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .domain import ImageEntry


class PythonSyntaxHighlighter(QSyntaxHighlighter):
    """Small dependency-free Python syntax highlighter for the filter editor."""

    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self._add_rule(
            r"#[^\n]*",
            foreground="#667085",
            italic=True,
        )
        self._add_rule(
            r"\b(and|as|assert|await|break|class|continue|def|del|elif|else|"
            r"except|False|finally|for|from|global|if|import|in|is|lambda|"
            r"None|nonlocal|not|or|pass|raise|return|True|try|while|with|yield)\b",
            foreground="#6941c6",
            bold=True,
        )
        self._add_rule(
            r"\b(len|any|all|sum|min|max|sorted|set|str|int|bool)\b",
            foreground="#175cd3",
        )
        self._add_rule(
            r"\b[0-9]+(?:\.[0-9]+)?\b",
            foreground="#b54708",
        )
        self._add_rule(
            r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')",
            foreground="#027a48",
        )

    def _add_rule(
        self,
        pattern: str,
        *,
        foreground: str,
        bold: bool = False,
        italic: bool = False,
    ) -> None:
        format_ = QTextCharFormat()
        format_.setForeground(QColor(foreground))
        format_.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
        format_.setFontItalic(italic)
        self._rules.append((QRegularExpression(pattern), format_))

    @override
    def highlightBlock(self, text: str) -> None:
        for expression, format_ in self._rules:
            match = expression.globalMatch(text)
            while match.hasNext():
                result = match.next()
                self.setFormat(result.capturedStart(), result.capturedLength(), format_)


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
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(code_buttons)
        layout.addWidget(self.code_input, 1)
        layout.addWidget(self.error_label)
        layout.addWidget(self.result_label)
        layout.addWidget(self.results, 2)
        layout.addWidget(close_buttons)

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
