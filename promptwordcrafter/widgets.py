"""小さなカスタムウィジェット群。"""

import os
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QLineEdit, QSpinBox, QTreeView

from . import text_io, tokens


class NewlineAwareLineEdit(QLineEdit):
    """Ctrl+Enter で改行文字を挿入できる一行入力欄。

    通常の Enter は従来通り returnPressed を発火させる。
    """

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.insert("\n")
            event.accept()
            return
        super().keyPressEvent(event)


class FolderTreeView(QTreeView):
    """選択変更時に横スクロール位置を保持する QTreeView。

    Qt 標準の QTreeView はマウスクリックやキー操作で選択項目を
    変更すると、その内部処理だけで選択項目が見えるように横スクロール
    位置を自動調整してしまう（外部のシグナルハンドラーより先に発生する
    ため、後から値を戻そうとしても間に合わない）。マウス押下・キー操作
    イベントの前後でスクロール位置を退避・復元することで、ユーザーが
    調整した横スクロール位置を維持する。

    テキストをフォルダの上にドラッグして離すと、そのフォルダへ移動 / コピーする (エクスプローラーと同じ。
    2026-11-28)。同じドライブなら移動、違うドライブならコピー。Ctrl を押していればコピー、Shift なら移動。
    動かすのは窓 (files_dropped を受けた側) がする。ドラッグの結果はいつも「コピー」と返す
    (ファイル一覧は他のアプリへはコピーしか渡さないので。エクスプローラーから来たものも、こちらで動かす)。
    """

    files_dropped = Signal(list, str, bool)  # テキストのパス, 行き先のフォルダ, 移動か
    drop_hint = Signal(str)  # ドラッグ中の説明 (ステータスバーへ。空 = 消す)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(False)  # 行き先は枠で示す (paintEvent)
        self.setAutoExpandDelay(800)  # 閉じたフォルダの上で待つと開く
        self._drop_target = QPersistentModelIndex()

    @staticmethod
    def _dragged_texts(event) -> list[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
        return [p for p in paths if text_io.is_text_file(p)]

    def _drop_folder(self, event) -> str:
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return ""
        path = self.model().filePath(index) if hasattr(self.model(), "filePath") else ""
        return path if path and Path(path).is_dir() else ""

    @staticmethod
    def _is_move(event, texts: list[Path], folder: str) -> bool:
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            return False
        if mods & Qt.KeyboardModifier.ShiftModifier:
            return True
        drive = Path(folder).drive.lower()
        return all(p.drive.lower() == drive for p in texts)

    def _set_target(self, index) -> None:
        if QModelIndex(self._drop_target) != index:
            self._drop_target = QPersistentModelIndex(index)
            self.viewport().update()

    def dragEnterEvent(self, event):
        if self._dragged_texts(event):
            self.setState(QAbstractItemView.State.DraggingState)  # 待つと開く・端で自動スクロールに要る
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        super().dragMoveEvent(event)  # 自動スクロールと、待つと開く (読み取り専用のモデルなので、受けるかはこちらで決める)
        texts = self._dragged_texts(event)
        folder = self._drop_folder(event)
        same = folder and all(os.path.normcase(str(p.parent)) == os.path.normcase(folder) for p in texts)
        if not texts or not folder or same:
            self._set_target(QModelIndex())
            self.drop_hint.emit("同じフォルダです" if same else "")
            event.ignore()
            return
        self._set_target(self.indexAt(event.position().toPoint()))
        move = self._is_move(event, texts, folder)
        what = texts[0].name if len(texts) == 1 else f"{len(texts)} 件のテキスト"
        other = "Ctrl を押すとコピー" if move else "Shift を押すと移動"
        self.drop_hint.emit(f"離すと {what}を「{Path(folder).name or folder}」へ{'移動' if move else 'コピー'}します ({other})")
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragLeaveEvent(self, event):
        super().dragLeaveEvent(event)
        self._set_target(QModelIndex())
        self.drop_hint.emit("")

    def dropEvent(self, event):
        texts = self._dragged_texts(event)
        folder = self._drop_folder(event)
        self._set_target(QModelIndex())
        self.drop_hint.emit("")
        self.stopAutoScroll()
        self.setState(QAbstractItemView.State.NoState)
        if not texts or not folder:
            event.ignore()
            return
        move = self._is_move(event, texts, folder)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.files_dropped.emit([str(p) for p in texts], folder, move)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_target.isValid():
            rect = self.visualRect(QModelIndex(self._drop_target))
            if rect.isValid():
                painter = QPainter(self.viewport())
                painter.setPen(QPen(QColor("#2f7de1"), 2))
                painter.drawRect(rect.adjusted(1, 1, -2, -2))
                painter.end()

    def mousePressEvent(self, event):
        h_scroll = self.horizontalScrollBar()
        preserved = h_scroll.value()
        super().mousePressEvent(event)
        h_scroll.setValue(preserved)

    def keyPressEvent(self, event):
        h_scroll = self.horizontalScrollBar()
        preserved = h_scroll.value()
        super().keyPressEvent(event)
        h_scroll.setValue(preserved)


def token_limit_spin(value: int, what: str) -> QSpinBox:
    """AI の返事の長さの上限 (おおよそのトークン数。0 = 上限なし)。"""
    spin = QSpinBox()
    spin.setRange(0, tokens.LIMIT_MAX)
    spin.setSingleStep(tokens.LIMIT_STEP)
    spin.setSpecialValueText("上限なし")
    spin.setPrefix("上限 ")
    spin.setSuffix(" トークン")
    spin.setValue(max(0, min(int(value or 0), tokens.LIMIT_MAX)))
    spin.setToolTip(
        f"{what}の結果の長さの上限 (おおよそのトークン数)。0 で上限なし。\n"
        "「約 N トークン以内で」と AI に伝え、守らないときは N の 1.5 倍ほどで打ち切ります。"
    )
    return spin
