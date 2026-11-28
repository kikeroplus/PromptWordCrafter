"""編集欄の下の「生成結果」: 表示中のテキストから生成した画像・動画のサムネールを横一列に並べる。

カーソルを乗せると一覧の上に大きく表示 (PrismLab と同じ。離れる・押す・ホイール・キーで消える。動画は音なしで再生)。
ダブルクリック / Enter で関連付けアプリで開く。右クリックで「生成時の設定」
「エクスプローラーで開く」「PrismLab に送る」「ごみ箱へ削除」。他のアプリへドラッグするとファイルとして渡す (コピーのみ)。
"""

import os
import queue
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QImage, QPainter, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import prismlab_link, results

THUMB_SIZE = 140
CELL_WIDTH = THUMB_SIZE + 16
CELL_HEIGHT = THUMB_SIZE + 34
PATH_ROLE = Qt.ItemDataRole.UserRole
PLACEHOLDER_COLOR = QColor("#e4e4e4")
OLD_COLOR = QColor("#8a6d00")
PRISMLAB_COLOR = QColor("#e0a000")  # PrismLab で作ったものの印 (PrismLab の強調色)


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


class _Preview(QWidget):
    """サムネールにカーソルを乗せると、一覧の上に大きく出す小窓 (PrismLab の量産ラインの「結果」と同じ出し方)。

    乗せて少し待つと出し、離れる・押す・ホイール・キー・右クリック・ほかのウィンドウに移ると消す。
    出ているあいだに隣へ移ったら、すぐ替える。動画は音なし・繰り返しで再生する。
    窓はフォーカスを取らず、マウスも素通りさせる (一覧の操作を邪魔しない)。
    """

    MARGIN = 6
    GAP = 8  # 画面の端・一覧との間
    DELAY_MS = 300
    CACHE = 8  # 読んだ大きな絵を覚えておく数 (隣と行き来しても読み直さない)

    def __init__(self, parent):
        # ToolTip の窓 (別の窓として出る。親を付けるのは、アプリを閉じたあとに消されて落ちないように)
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("_Preview { background: #202020; border: 1px solid #555; }"
                           "QLabel { color: #e0e0e0; }")
        self.path: Optional[str] = None  # 出している (出そうとしている) ファイル
        self._pending: Optional[tuple] = None  # 待っているあいだの (パス, 動画か, 一覧の範囲, 説明)
        self._cache: dict = {}  # (パス, 更新日時, 幅, 高さ) → (絵, 元の大きさ)
        self._player = None  # 動画を再生するもの (初めて動画に乗せたときに作る)
        self._sink = None
        self._playing = ""
        self._size = QSize()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, self.MARGIN)
        layout.setSpacing(4)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.image)
        self.caption = QLabel()
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.caption)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DELAY_MS)
        self._timer.timeout.connect(self._show_pending)

    def hover(self, path: str, is_video: bool, anchor: QRect, note: str):
        """path に乗った。出ていなければ少し待ってから、出ていればすぐ出す。"""
        if path == self.path:
            return
        self.path = path
        self._pending = (path, is_video, anchor, note)
        if self.isVisible():
            self._show_pending()
        else:
            self._timer.start()

    def hide(self):
        self._timer.stop()
        self.path = None
        self._pending = None
        self._stop_video()
        super().hide()

    def forget(self, paths):
        """消した・作り直したファイルの絵を忘れる。"""
        keys = {str(p) for p in paths}
        for key in [k for k in self._cache if k[0] in keys]:
            del self._cache[key]

    def _show_pending(self):
        if self._pending is None:
            return
        path, is_video, anchor, note = self._pending
        parent = self.parentWidget()
        if parent is None or not parent.isVisible() or not parent.window().isActiveWindow():
            return
        if not self._show_file(path, is_video, anchor, note):
            self.hide()

    def _show_file(self, path: str, is_video: bool, anchor: QRect, note: str) -> bool:
        """path を anchor (一覧の画面上の範囲) の上に出す。上が狭ければ画面の中央に出す。"""
        screen = QGuiApplication.screenAt(anchor.center()) or self.screen()
        avail = screen.availableGeometry().adjusted(self.GAP, self.GAP, -self.GAP, -self.GAP)
        space = QRect(avail.left(), avail.top(), avail.width(), anchor.top() - self.GAP - avail.top())
        if space.height() < 320:
            space = avail
        self.caption.setText(note)
        chrome_h = self.MARGIN * 2 + 4 + self.caption.sizeHint().height()
        width = space.width() - self.MARGIN * 2
        height = space.height() - chrome_h
        ratio = screen.devicePixelRatio()
        key = (path, results._mtime(Path(path)), int(width * ratio), int(height * ratio))
        loaded = self._cache.get(key)
        if loaded is None:
            loaded = results.load_preview(Path(path), key[2], key[3])
            if loaded[0] is None or loaded[0].isNull():
                return False
            self._cache[key] = loaded
            while len(self._cache) > self.CACHE:
                del self._cache[next(iter(self._cache))]
        image, original = loaded
        if not (is_video and self._playing == path):  # 再生が始まっていれば、コマのほうを出したままにする
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(ratio)
            self._size = QSize(round(image.width() / ratio), round(image.height() / ratio))
            if is_video:  # 動画は、出せるいちばん大きな大きさに合わせる (コマはあとで同じ大きさに描く)
                self._size = self._size.scaled(QSize(width, height), Qt.AspectRatioMode.KeepAspectRatio)
                pixmap = pixmap.scaled(self._size * ratio, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                pixmap.setDevicePixelRatio(ratio)
            self.image.setPixmap(pixmap)
        name = Path(path).name
        if is_video:
            head = f"▶ 動画 · {name}"
        elif original is not None:
            head = f"{name}    {original.width()} × {original.height()}"
        else:
            head = name
        self.caption.setText(head + (f"\n{note}" if note else ""))
        self.adjustSize()
        size = self.sizeHint()
        x = min(max(anchor.center().x() - size.width() // 2, space.left()), space.right() - size.width() + 1)
        if space == avail:
            y = avail.top() + (avail.height() - size.height()) // 2
        else:
            y = space.bottom() - size.height() + 1
        self.setGeometry(x, y, size.width(), size.height())
        self.show()
        self.raise_()
        if is_video:
            self._play(path)
        else:
            self._stop_video()
        return True

    # ---- 動画 (PrismLab と同じく、コマは QVideoSink で受けて同じ欄に描く) ----
    def _play(self, path: str):
        if self._playing == path:
            return
        if self._player is None:
            from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
            self._player = QMediaPlayer(self)  # 音の出口を付けない = 音なし
            self._sink = QVideoSink(self)
            self._player.setVideoOutput(self._sink)
            self._player.setLoops(QMediaPlayer.Loops.Infinite)
            self._sink.videoFrameChanged.connect(self._frame)
        self._playing = path
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _stop_video(self):
        if self._player is not None and self._playing:
            self._playing = ""
            self._player.stop()
            self._player.setSource(QUrl())  # ファイルを手放す (削除・移動できるように)

    def _frame(self, frame):
        if not self._playing or self._playing != self.path or not self.isVisible() or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap.fromImage(image).scaled(self._size * ratio, Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(ratio)
        self.image.setPixmap(pixmap)


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
    info_requested = Signal(object)  # そのファイルを作ったときの記録 (comfy.GenRecord)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source: Optional[Path] = None
        self._text = ""
        self._ticket = 0
        self._images: dict[str, QImage] = {}  # パスと更新日時ごとのサムネール
        self._items: dict[str, QListWidgetItem] = {}
        self._records: dict = {}  # パス → 生成の記録 (記録のないものは入れない)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel()
        header_row.addWidget(self.header, 1)
        self.refresh_button = QPushButton("更新")
        self.refresh_button.setToolTip("フォルダの中を見直して、生成結果の一覧を作り直します (他のアプリで足した・消したファイルも反映)")
        self.refresh_button.clicked.connect(lambda: self.refresh())
        header_row.addWidget(self.refresh_button)
        layout.addLayout(header_row)
        self.list = _ResultList(self)
        layout.addWidget(self.list)

        self._loader = _ThumbnailLoader()
        self._loader.loaded.connect(self._on_loaded, Qt.ConnectionType.QueuedConnection)
        self.preview = _Preview(self)
        # 乗せると大きく出す (PrismLab と同じ。2026-11-28 利用者の希望で、クリックで出すのをやめた)
        self.list.viewport().setMouseTracking(True)
        self.list.viewport().installEventFilter(self)
        self.list.installEventFilter(self)
        self.list.itemDoubleClicked.connect(self._on_double_clicked)
        self.list.open_requested.connect(self._open_current)
        self.list.delete_requested.connect(self.delete_selected)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        self.clear()

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------
    def clear(self):
        self.preview.hide()
        self.source = None
        self._text = ""
        self._ticket = self._loader.request([])
        self.list.clear()
        self._items.clear()
        self._records.clear()
        self.header.setText("生成結果")
        self.refresh_button.setEnabled(False)

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
        self.refresh_button.setEnabled(True)

        found = results.find_results(source, text)
        self.preview.hide()  # 並びが変わるので、出している拡大は消す
        self.list.clear()
        self._items.clear()
        self._records = {str(r.path): r.record for r in found if r.record is not None}
        pending = []
        for result in found:
            path = str(result.path)
            item = QListWidgetItem(result.path.name)
            item.setData(PATH_ROLE, path)
            item.setData(PATH_ROLE + 1, result.is_video)
            item.setData(PATH_ROLE + 2, result.old)
            item.setData(PATH_ROLE + 3, result.prismlab)
            item.setData(PATH_ROLE + 4, self._note(result))  # 拡大の下に出す説明 (ツールチップは拡大と重ねない)
            item.setSizeHint(QSize(CELL_WIDTH, CELL_HEIGHT))
            image = self._images.get(self._cache_key(result.path))
            item.setIcon(self._icon(image, result.is_video, result.old, result.prismlab))
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

    def _note(self, result: results.ResultFile) -> str:
        lines = [str(result.path.parent)]
        if result.prismlab and result.record is not None:
            req = result.record.request
            lines.append(f"PrismLab: {req.recipe or '(レシピ名なし)'}" + (f" ・ ステップ {req.step}" if req.step else ""))
        if result.record is not None and result.record.time:
            lines.append(f"生成した日時: {result.record.time}")
            lines.append(f"ワークフロー: {result.record.request.workflow_name}")
            if result.record.request.enhancer is not None:
                lines.append(f"エンハンサー: {result.record.request.enhancer_name or '(名前なし)'}")
        elif result.record is None:
            lines.append("(生成の記録なし。ファイル名から見つけたもの)")
        if result.old:
            lines.append("※ 前の本文で生成したもの (今の本文とは違います)")
        lines.append("ダブルクリックで開く / 右クリックで生成時の設定")
        return "\n".join(lines)

    def _on_loaded(self, ticket: int, path: str, image: QImage):
        self._images[self._cache_key(Path(path))] = image
        if ticket != self._ticket:
            return
        item = self._items.get(path)
        if item is not None:
            item.setIcon(self._icon(image, item.data(PATH_ROLE + 1), item.data(PATH_ROLE + 2),
                                    bool(item.data(PATH_ROLE + 3))))

    def _icon(self, image: Optional[QImage], is_video: bool, old: bool, prismlab: bool = False) -> QIcon:
        """サムネールに印を重ねる (動画は左下に ▶、前の本文で作ったものは左上に「旧」、PrismLab で作ったものは右上に「PL」)。"""
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
        if prismlab:
            badge = QRect(THUMB_SIZE - 32, 4, 28, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(PRISMLAB_COLOR)
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("black"))
            font = QFont(painter.font())
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "PL")
        painter.end()
        return QIcon(canvas)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _selected_paths(self) -> list[Path]:
        return [Path(item.data(PATH_ROLE)) for item in self.list.selectedItems()]

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.MouseMove and obj is self.list.viewport():
            if not event.buttons():  # ドラッグ中・押しているあいだは出さない
                self._hovered(event.position().toPoint())
        elif kind in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
                      QEvent.Type.Wheel, QEvent.Type.KeyPress, QEvent.Type.Hide, QEvent.Type.WindowDeactivate,
                      QEvent.Type.ContextMenu):
            self.preview.hide()
        return super().eventFilter(obj, event)

    def _hovered(self, pos: QPoint):
        item = self.list.itemAt(pos)
        path = item.data(PATH_ROLE) if item is not None else None
        if not path or not Path(path).exists():
            self.preview.hide()
            return
        anchor = QRect(self.list.mapToGlobal(QPoint(0, 0)), self.list.size())
        rect = self.list.visualItemRect(item)
        anchor.moveLeft(self.list.viewport().mapToGlobal(rect.center()).x() - anchor.width() // 2)
        self.preview.hover(path, bool(item.data(PATH_ROLE + 1)), anchor, item.data(PATH_ROLE + 4) or "")

    def _on_double_clicked(self, item: QListWidgetItem):
        self._open(Path(item.data(PATH_ROLE)))

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
        record = self._records.get(str(clicked))
        info = menu.addAction("生成時の設定...")
        info.setEnabled(record is not None)
        if record is None:
            info.setToolTip("生成の記録がありません (ファイル名から見つけたもの)")
        info.triggered.connect(lambda: self.info_requested.emit(record))
        menu.setToolTipsVisible(True)
        explorer = menu.addAction("エクスプローラーで開く")
        explorer.triggered.connect(lambda: results.show_in_explorer(clicked))
        menu.addSeparator()
        self._add_prismlab_menu(menu)
        menu.addSeparator()
        if len(paths) > 1:
            delete = menu.addAction(f"選択中の {len(paths)} 個のファイルを削除 (ごみ箱へ)\tDel")
        else:
            delete = menu.addAction("ファイルを削除 (ごみ箱へ)\tDel")
        delete.triggered.connect(self.delete_selected)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    # PrismLab に送る (PrismLab の spec 5 章。J-Rate の「PrismLab に送る」と同じ項目) ----------------
    def _selected_images(self) -> list[Path]:
        """選んでいる画像 (動画は送らない)。帯は新しい順なので、作った順 (左から右の逆) に並べる。"""
        items = sorted(self.list.selectedItems(), key=self.list.row, reverse=True)
        return [Path(item.data(PATH_ROLE)) for item in items if not item.data(PATH_ROLE + 1)]

    def _add_prismlab_menu(self, menu: QMenu) -> None:
        images = self._selected_images()
        unit = f" ({len(images)} 枚)" if len(images) > 1 else ""
        sub = QMenu("PrismLab に送る" + unit, menu)  # 親を付けて作る (関数を抜けても消えないように)
        menu.addMenu(sub)
        sub.setToolTipsVisible(True)
        sub.setEnabled(bool(images))
        if not images:
            sub.setTitle("PrismLab に送る (画像がありません)")
            return
        names = prismlab_link.recipe_names()
        if names:
            head = sub.addAction(f"量産の入口に{unit}:")
            head.setEnabled(False)
            for index, name in enumerate(names):
                action = sub.addAction("    " + (name or "(名前なし)").replace("&", "&&"))
                action.setToolTip("選んだ画像を、このレシピの入口 (選択) にします。始めるのは PrismLab で。")
                action.triggered.connect(lambda _c=False, n=name, i=index: self._send(
                    lambda: prismlab_link.send_to_recipe(images, n, i),
                    f"{len(images)} 枚を PrismLab のレシピ「{n}」の入口に送りました。"))
        else:
            none = sub.addAction("(PrismLab のレシピがありません)")
            none.setEnabled(False)
        sub.addSeparator()
        scenes = sub.addAction("物語: 新しいシーンにする" + unit)
        scenes.setToolTip("PrismLab で開いている物語の最後に、1 枚ずつシーンチェンジのシーンとして足します。")
        scenes.triggered.connect(lambda: self._send(lambda: prismlab_link.send_to_story(images, "scenes"),
                                                    f"{len(images)} 枚を PrismLab の物語に新しいシーンとして送りました。"))
        batch = sub.addAction("物語: バッチに足す..." + unit)
        batch.setToolTip("PrismLab の「物語のバッチ」の設定の窓を出し、1 枚ごとに 1 件のバッチの最初の画像にします。"
                         "待ち行列に足すと、前のものが終わってから順に作ります。")
        batch.triggered.connect(lambda: self._send(lambda: prismlab_link.send_to_story(images, "batch"),
                                                   f"{len(images)} 枚を PrismLab の物語のバッチに送りました"
                                                   " (PrismLab の窓で待ち行列に足します)。"))
        if len(images) != 1:
            return
        for slot, label, what in (("source", "物語: 選んでいるシーンの元画像に", "元画像"),
                                  ("reference", "物語: 選んでいるシーンのリファレンス (顔) に", "リファレンス (顔)"),
                                  ("last", "物語: 選んでいるシーンのラストフレームに", "ラストフレーム")):
            action = sub.addAction(label)
            action.triggered.connect(lambda _c=False, s=slot, w=what: self._send(
                lambda: prismlab_link.send_to_story(images, s),
                f"{images[0].name} を PrismLab の物語 (選んでいるシーンの{w}) に送りました。"))

    def _send(self, send, done: str) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            problem = send()
        finally:
            QApplication.restoreOverrideCursor()
        if problem:
            QMessageBox.warning(self, "PrismLab に送る", problem)
        else:
            self.status.emit(done)

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
        self.preview.hide()  # 再生している動画を手放してから消す
        error = results.send_to_recycle_bin(paths)
        self.preview.forget(paths)
        gone = [p for p in paths if not p.exists()]
        self.refresh()
        # 消したあとは、同じ位置 (= 次) のものを選ぶ
        if self.list.count():
            row = min(first_row, self.list.count() - 1)
            self.list.setCurrentRow(row)
        if error and len(gone) < len(paths):
            self.status.emit(f"{error}  (残った {len(paths) - len(gone)} 個 / ごみ箱へ移動 {len(gone)} 個)")
        elif len(gone) == 1:
            self.status.emit(f"ごみ箱へ移動しました: {gone[0].name}")
        else:
            self.status.emit(f"{len(gone)} 個のファイルをごみ箱へ移動しました")
