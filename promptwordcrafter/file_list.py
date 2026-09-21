"""Windowsエクスプローラーへのネイティブドラッグ&ドロップに対応したファイル一覧。"""

from pathlib import Path

from PySide6.QtCore import Qt, QMimeData, QUrl
from PySide6.QtWidgets import QAbstractItemView, QListWidget


class FileListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

        self.setSpacing(0)
        self.setUniformItemSizes(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction

    def mimeData(self, items):
        mime = QMimeData()
        urls = []
        for item in items:
            path_str = item.data(Qt.ItemDataRole.UserRole)
            if path_str:
                urls.append(QUrl.fromLocalFile(str(Path(path_str))))
        mime.setUrls(urls)
        return mime
