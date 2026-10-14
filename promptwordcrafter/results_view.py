"""編集欄の下の「生成結果」: 表示中のテキストから生成した画像・動画のサムネールを横一列に並べる。

ダブルクリック / Enter で関連付けアプリで開く。右クリックで「エクスプローラーで開く」
「ごみ箱へ削除」。他のアプリへドラッグするとファイルとして渡す (コピーのみ)。
"""

import os
import queue
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QMimeData, QObject, QPoint, QRect, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from . import results

THUMB_SIZE = 140
CELL_WIDTH = THUMB_SIZE + 16
CELL_HEIGHT = THUMB_SIZE + 34
PATH_ROLE = Qt.ItemDataRole.UserRole
PLACEHOLDER_COLOR = QColor("#e4e4e4")
OLD_COLOR = QColor("#8a6d00")


class _ThumbnailLoader(QObject):
    """裏のスレッドで 1 枚ずつサムネールを作る。新しい依頼が来たら古い依頼は捨てる。"""

    loaded = Signal(int, str, QImage)  # 依頼番号, パス, 画像

    def __init__(self):
        super().__init__()
        self._queue: "queue.Queue[tuple[int, list[str]]]" = queue.Queue()
        self._ticket = 0
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, paths: list[str]) -> int:
        self._ticket += 1
        self._queue.put((self._ticket, paths))
        return self._ticket

    def _run(self):
        results.init_thread_com()
        while True:
            ticket, paths = self._queue.get()
            for path in paths:
                if ticket != self._ticket:
                    break  # 別のテキストに移った
                try:
                    image = results.make_thumbnail(Path(path), THUMB_SIZE)
                except Exception:  # noqa: BLE001 - 作れないものは灰色のまま
                    image = None
                if image is not None and not image.isNull():
                    try:
                        self.loaded.emit(ticket, path, image)
                    except RuntimeError:
                        return  # ウィンドウが閉じられた


class _ResultList(QListWidget):
    """横一列のサムネール。ホイールで横にスクロールし、ドラッグでファイルを渡す。"""

    open_requested = Signal()
    delete_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.setGridSize(QSize(CELL_WIDTH, CELL_HEIGHT))
        self.setUniformItemSizes(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setFixedHeight(CELL_HEIGHT + self.horizontalScrollBar().sizeHint().height() + 6)

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction  # 移動させると一覧と食い違うのでコピーのみ

    def mimeData(self, items):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(item.data(PATH_ROLE)) for item in items])
        return mime

    def wheelEvent(self, event):
        bar = self.horizontalScrollBar()
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar.setValue(bar.value() - delta)
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.open_requested.emit()
            return
        if event.key() == Qt.Key.Key_Delete:
            if not event.isAutoRepeat():  # 押しっぱなしで続けて消えないように
                self.delete_requested.emit()
            return
        super().keyPressEvent(event)


class ResultsStrip(QWidget):
    """表示中のテキストから生成したファイルの一覧。"""

    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source: Optional[Path] = None
        self._text = ""
        self._ticket = 0
        self._images: dict[str, QImage] = {}  # パスと更新日時ごとのサムネール
        self._items: dict[str, QListWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)
        self.header = QLabel()
        layout.addWidget(self.header)
        self.list = _ResultList(self)
        layout.addWidget(self.list)

        self._loader = _ThumbnailLoader()
        self._loader.loaded.connect(self._on_loaded, Qt.ConnectionType.QueuedConnection)
        self.list.itemDoubleClicked.connect(lambda item: self._open(Path(item.data(PATH_ROLE))))
        self.list.open_requested.connect(self._open_current)
        self.list.delete_requested.connect(self.delete_selected)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        self.clear()

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------
    def clear(self):
        self.source = None
        self._text = ""
        self._ticket = self._loader.request([])
        self.list.clear()
        self._items.clear()
        self.header.setText("生成結果")

    def show_for(self, source: Optional[Path], text: str):
        """そのテキストの生成結果を出す。同じテキストなら選択とスクロール位置を残す。"""
        if source is None:
            self.clear()
            return
        same = self.source is not None and results._key(self.source) == results._key(source)
        selected = {item.data(PATH_ROLE) for item in self.list.selectedItems()} if same else set()
        scroll = self.list.horizontalScrollBar().value() if same else 0
        self.source = source
        self._text = text

        found = results.find_results(source, text)
        self.list.clear()
        self._items.clear()
        pending = []
        for result in found:
            path = str(result.path)
            item = QListWidgetItem(result.path.name)
            item.setData(PATH_ROLE, path)
            item.setData(PATH_ROLE + 1, result.is_video)
            item.setData(PATH_ROLE + 2, result.old)
            item.setToolTip(self._tooltip(result))
            item.setSizeHint(QSize(CELL_WIDTH, CELL_HEIGHT))
            image = self._images.get(self._cache_key(result.path))
            item.setIcon(self._icon(image, result.is_video, result.old))
            self.list.addItem(item)
            self._items[path] = item
            if path in selected:
                item.setSelected(True)
            if image is None:
                pending.append(path)
        self.list.horizontalScrollBar().setValue(scroll)

        old = sum(1 for r in found if r.old)
        if not found:
            self.header.setText("生成結果: このテキストから生成したファイルはまだありません")
        else:
            note = f" (うち {old} 件は前の本文で生成)" if old else ""
            self.header.setText(f"生成結果 ({len(found)} 件){note}")
        self._ticket = self._loader.request(pending)

    def refresh(self, text: Optional[str] = None):
        if self.source is not None:
            self.show_for(self.source, self._text if text is None else text)

    def _cache_key(self, path: Path) -> str:
        return f"{results._key(path)}|{results._mtime(path)}"

    def _tooltip(self, result: results.ResultFile) -> str:
        lines = [str(result.path)]
        if result.record is not None and result.record.time:
            lines.append(f"生成した日時: {result.record.time}")
            lines.append(f"ワークフロー: {result.record.request.workflow_name}")
        elif result.record is None:
            lines.append("(生成の記録なし。ファイル名から見つけたもの)")
        if result.old:
            lines.append("※ 前の本文で生成したもの (今の本文とは違います)")
        lines.append("ダブルクリックで開く")
        return "\n".join(lines)

    def _on_loaded(self, ticket: int, path: str, image: QImage):
        self._images[self._cache_key(Path(path))] = image
        if ticket != self._ticket:
            return
        item = self._items.get(path)
        if item is not None:
            item.setIcon(self._icon(image, item.data(PATH_ROLE + 1), item.data(PATH_ROLE + 2)))

    def _icon(self, image: Optional[QImage], is_video: bool, old: bool) -> QIcon:
        """サムネールに印を重ねる (動画は左下に ▶、前の本文で作ったものは左上に「旧」)。"""
        canvas = QPixmap(THUMB_SIZE, THUMB_SIZE)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if image is None:
            painter.fillRect(QRect(8, 8, THUMB_SIZE - 16, THUMB_SIZE - 16), PLACEHOLDER_COLOR)
        else:
            x = (THUMB_SIZE - image.width()) // 2
            y = (THUMB_SIZE - image.height()) // 2
            painter.drawImage(x, y, image)
        if is_video:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawEllipse(QRect(6, THUMB_SIZE - 34, 28, 28))
            painter.setBrush(QColor("white"))
            painter.drawPolygon(QPolygon([QPoint(16, THUMB_SIZE - 27), QPoint(16, THUMB_SIZE - 13),
                                          QPoint(27, THUMB_SIZE - 20)]))
        if old:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(OLD_COLOR)
            painter.drawRoundedRect(QRect(4, 4, 26, 20), 4, 4)
            painter.setPen(QColor("white"))
            font = QFont(painter.font())
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRect(4, 4, 26, 20), Qt.AlignmentFlag.AlignCenter, "旧")
        painter.end()
        return QIcon(canvas)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _selected_paths(self) -> list[Path]:
        return [Path(item.data(PATH_ROLE)) for item in self.list.selectedItems()]

    def _open_current(self):
        item = self.list.currentItem()
        if item is not None:
            self._open(Path(item.data(PATH_ROLE)))

    def _open(self, path: Path):
        try:
            os.startfile(str(path))  # 関連付けられたアプリで開く
        except OSError as exc:
            self.status.emit(f"開けませんでした: {path.name} ({exc})")

    def _show_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        paths = self._selected_paths()
        clicked = Path(item.data(PATH_ROLE))
        menu = QMenu(self)
        open_action = menu.addAction("開く")
        open_action.triggered.connect(lambda: self._open(clicked))
        explorer = menu.addAction("エクスプローラーで開く")
        explorer.triggered.connect(lambda: results.show_in_explorer(clicked))
        menu.addSeparator()
        if len(paths) > 1:
            delete = menu.addAction(f"選択中の {len(paths)} 個のファイルを削除 (ごみ箱へ)\tDel")
        else:
            delete = menu.addAction("ファイルを削除 (ごみ箱へ)\tDel")
        delete.triggered.connect(self.delete_selected)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def delete_selected(self):
        """選択中のファイルをごみ箱へ。1 つなら確認なし、複数かごみ箱に入らないときは確認する。"""
        paths = self._selected_paths()
        if not paths:
            return
        recyclable = all(results.can_recycle(p) for p in paths)
        if len(paths) > 1 or not recyclable:
            what = paths[0].name if len(paths) == 1 else f"選択中の {len(paths)} 個のファイル"
            text = f"{what}を削除 (ごみ箱へ) しますか？"
            if not recyclable:
                text = f"{what}を削除しますか？\n\nこの場所のファイルはごみ箱に入らず、完全に消えます。"
            if QMessageBox.question(self, "ファイルを削除", text) != QMessageBox.StandardButton.Yes:
                return
        first_row = min(self.list.row(item) for item in self.list.selectedItems())
        error = results.send_to_recycle_bin(paths)
        gone = [p for p in paths if not p.exists()]
        self.refresh()
        # 消したあとは、同じ位置 (= 次) のものを選ぶ
        if self.list.count():
            row = min(first_row, self.list.count() - 1)
            self.list.setCurrentRow(row)
        if error and len(gone) < len(paths):
            self.status.emit(f"{error}: {len(paths) - len(gone)} 個 / ごみ箱へ移動 {len(gone)} 個")
        elif len(gone) == 1:
            self.status.emit(f"ごみ箱へ移動しました: {gone[0].name}")
        else:
            self.status.emit(f"{len(gone)} 個のファイルをごみ箱へ移動しました")
