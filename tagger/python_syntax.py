from __future__ import annotations

from typing import override

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat


class PythonSyntaxHighlighter(QSyntaxHighlighter):
    """Small dependency-free Python syntax highlighter."""

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
                self.setFormat(
                    result.capturedStart(), result.capturedLength(), format_
                )
