"""小さなカスタムウィジェット群。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QTreeView


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
    """

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
