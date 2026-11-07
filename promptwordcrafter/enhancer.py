"""プロンプトエンハンサー (AI で盛る) の、編集欄まわりの部分。

「盛る」を押すと、編集欄の今の内容をローカル LLM に渡して書き足してもらい、
結果を編集欄の右上の小窓に出す。ファイルには書き込まない (コピーして使う)。

エンハンサーの登録・LLM の接続・盛る処理は共通部品 (gencommon.enhancer) にあり、
J-Rate・ImageMimicArt と共有している。ここで選んでいる番号は %APPDATA%\PromptWordCrafter\enhance_preset.txt。
"""

from typing import Callable

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gencommon import enhancer, llm, presets, settings
from gencommon.presets import LlmPreset

from .translation import COPIED_MS, check_icon, copy_icon


# ----------------------------------------------------------------------
# 編集欄の右上に出す小窓
# ----------------------------------------------------------------------
class EnhancerPopup(QFrame):
    """盛った結果を見てコピーするための小窓。翻訳の小窓 (右下) と重ならないよう右上に出す。"""

    again_requested = Signal()

    WIDTH_RATIO = 0.55
    HEIGHT_RATIO = 0.42
    MARGIN = 10

    def __init__(self, editor: QTextEdit):
        super().__init__(editor)
        self.editor = editor
        self.setObjectName("enhancerPopup")
        self.setStyleSheet(
            "#enhancerPopup { background: #f4f8fb; border: 1px solid #9fb3c4; border-radius: 6px; }"
            "#enhancerTitle { color: #4f6273; }"
            "QPlainTextEdit { background: transparent; border: none; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 6)
        layout.setSpacing(2)

        header = QHBoxLayout()
        self.title_label = QLabel("盛った結果")
        self.title_label.setObjectName("enhancerTitle")
        header.addWidget(self.title_label, 1)
        self._copy_icon = copy_icon()
        self._check_icon = check_icon()
        self.copy_button = QToolButton()
        self.copy_button.setIcon(self._copy_icon)
        self.copy_button.setIconSize(QSize(16, 16))
        self.copy_button.setToolTip("結果をコピー")
        self.copy_button.setAutoRaise(True)
        self.copy_button.clicked.connect(self.copy_text)
        header.addWidget(self.copy_button)
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.timeout.connect(self._reset_copy_button)
        self.again_button = QToolButton()
        self.again_button.setText("もう一度")
        self.again_button.setToolTip("編集欄の今の内容を、選んでいるエンハンサーで盛り直します (毎回違う結果になります)")
        self.again_button.setAutoRaise(True)
        self.again_button.clicked.connect(self.again_requested)
        header.addWidget(self.again_button)
        close_button = QToolButton()
        close_button.setText("×")
        close_button.setToolTip("小窓を閉じます")
        close_button.setAutoRaise(True)
        close_button.clicked.connect(self.hide)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setFont(editor.font())
        layout.addWidget(self.body, 1)

        editor.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() == QEvent.Type.Resize:
            self.place()
        return super().eventFilter(watched, event)

    def place(self):
        area = self.editor.viewport().geometry()  # スクロールバーを除いた表示部分
        width = max(220, int(area.width() * self.WIDTH_RATIO))
        height = max(90, int(area.height() * self.HEIGHT_RATIO))
        width = min(width, area.width() - self.MARGIN * 2)
        height = min(height, area.height() - self.MARGIN * 2)
        self.setGeometry(
            area.right() - width - self.MARGIN + 1,
            area.top() + self.MARGIN,
            max(width, 0),
            max(height, 0),
        )

    def copy_text(self):
        """結果を丸ごとクリップボードへ。一部だけなら本文を選んで Ctrl+C でもコピーできる。"""
        QGuiApplication.clipboard().setText(self.body.toPlainText())
        self.copy_button.setIcon(self._check_icon)
        self.copy_button.setToolTip("コピーしました")
        self._copied_timer.start(COPIED_MS)

    def _reset_copy_button(self):
        self.copy_button.setIcon(self._copy_icon)
        self.copy_button.setToolTip("結果をコピー")

    def show_message(self, title: str, text: str, waiting: bool = False, copyable: bool = True):
        self.title_label.setText(title)
        self.body.setPlainText(text)
        self.body.setEnabled(not waiting)
        self.copy_button.setEnabled(copyable and not waiting)
        self.again_button.setEnabled(not waiting)
        self._copied_timer.stop()
        self._reset_copy_button()
        self.place()
        self.show()
        self.raise_()


# ----------------------------------------------------------------------
# メインウィンドウから使う部分
# ----------------------------------------------------------------------
class EnhancerController(QObject):
    """エンハンサーの選択・「盛る」・「エンハンサーの設定」と小窓を受け持つ。

    依頼は同時に 1 件だけ (盛っている間は「盛る」を押せない)。
    結果はファイルを切り替えても小窓に残す (新しいファイルに貼るときなどに消えないように)。
    """

    _done = Signal(object, object)  # 結果, エラー文

    def __init__(self, editor: QTextEdit, current_name: Callable[[], str], parent: QWidget):
        super().__init__(parent)
        self.parent_widget = parent
        self.editor = editor
        self._current_name = current_name  # 表示中のファイル名
        self.presets = enhancer.load_presets()
        self._busy = False
        self._source = ""  # 盛った元のファイル名 (小窓の見出し用)
        self._running = LlmPreset()  # 盛っている最中のエンハンサー

        self.combo = QComboBox()
        self.combo.setToolTip("使うエンハンサー (生成 AI のモデルごとの書き方)")
        self.combo.setMinimumContentsLength(8)
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        presets.fill_combo(self.combo, self.presets, presets.load_index(enhancer.PANEL_FILE))
        self.combo.currentIndexChanged.connect(lambda i: presets.save_index(enhancer.PANEL_FILE, i))
        self.enhance_button = QPushButton("盛る")
        self.enhance_button.setToolTip("編集欄の今の内容を AI で書き足し、右上の小窓に出します (ファイルは変えません)")
        self.settings_button = QPushButton("設定")  # グループ名が「エンハンサー (AI)」なので短く
        self.settings_button.setToolTip("エンハンサーの設定 (指示文の登録、LLM のサーバーURL とモデル)")

        self.popup = EnhancerPopup(editor)

        self.enhance_button.clicked.connect(self.enhance)
        self.popup.again_requested.connect(self.enhance)
        self.settings_button.clicked.connect(self.show_settings)
        self._done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    def _title(self, prefix: str, preset: LlmPreset) -> str:
        name = preset.name or f"{self.combo.currentIndex() + 1} 番"
        return f"{prefix} ({name}): {self._source}" if self._source else f"{prefix} ({name})"

    # ---- 盛る ----
    def enhance(self):
        if self._busy:
            return
        self._source = self._current_name()
        self.presets = enhancer.load_presets()  # ほかのアプリで直したものも使う
        preset = self.presets[max(self.combo.currentIndex(), 0)]
        text = self.editor.toPlainText()
        if preset.is_empty:
            self.popup.show_message(
                "盛れません", "このエンハンサーは未登録です。「エンハンサーの設定」で指示文を登録してください。", copyable=False
            )
            return
        if not text.strip():
            self.popup.show_message("盛れません", "編集欄が空です。", copyable=False)
            return
        self._busy = True
        self.enhance_button.setEnabled(False)
        self.popup.show_message(self._title("盛っています...", preset), "AI が書き足しています...", waiting=True)
        base_url, model = settings.llm_url(), settings.enhance_model()
        self._running = preset
        llm.run_in_thread(lambda: enhancer.enhance(base_url, model, preset, text), self._done)

    def _on_done(self, result, error):
        self._busy = False
        self.enhance_button.setEnabled(True)
        preset = self._running
        if error is None:
            self.popup.show_message(self._title("盛った結果", preset), result or "")
        else:
            self.popup.show_message(self._title("盛れませんでした", preset), error, copyable=False)

    # ---- 設定 ----
    def show_settings(self):
        dialog = enhancer.EnhancerSettingsDialog(self.combo.currentIndex(), self.parent_widget)
        dialog.exec()
        # プリセットは窓の中で保存済み (キャンセルしても残る)。並び替えたら選んでいた番号も付いていく
        self.presets = enhancer.load_presets()
        presets.fill_combo(self.combo, self.presets, presets.load_index(enhancer.PANEL_FILE))
