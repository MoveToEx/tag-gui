from __future__ import annotations

import sys
from collections.abc import Sequence

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main(argv: Sequence[str] | None = None) -> int:
    app = QApplication(list(argv) if argv is not None else sys.argv)
    QCoreApplication.setOrganizationName("tag-gui")
    QCoreApplication.setApplicationName("tag-gui")
    window = MainWindow()
    window.show()
    return app.exec()
