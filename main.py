"""Точка входа: python main.py [проект.lbrn.json | рисунок.svg | рисунок.dxf]"""
import os
import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from laserburn import __version__
from laserburn.core import Document
from laserburn.gui import APP_NAME, MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("LaserBurnAnalogue")
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    # Имя .desktop-файла из install.sh — по нему оболочка находит иконку окна
    app.setDesktopFileName("laserburn")
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "laserburn", "resources", "icon.png")))
    win = MainWindow()
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if path.endswith(".json"):
            win.file_path = path
            win._set_document(Document.load(path))
        else:
            win.import_path(path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
