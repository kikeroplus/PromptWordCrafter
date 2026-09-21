import sys

from PySide6.QtWidgets import QApplication

from .icon import make_icon
from .main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(make_icon())
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
