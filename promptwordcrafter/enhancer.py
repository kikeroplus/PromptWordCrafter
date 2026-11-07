"""プロンプトエンハンサー (AI で盛る)。

「盛る」を押すと、編集欄の今の内容をローカル LLM に渡して書き足してもらい、
結果を編集欄の右上の小窓に出す。ファイルには書き込まない (コピーして使う)。

生成 AI のモデルごとに推奨される書き方が違うので、指示文 (system)・user 側の雛形・
temperature を「エンハンサー」のプリセットとして登録し、選んで使う。
接続先のサーバーは翻訳と同じものを使い、LLM のモデルだけ別に選べる。
"""

import json
import re
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import comfy, llm
from .translation import COPIED_MS, DEFAULT_MODEL, check_icon, copy_icon

PRESET_COUNT = 20
PRESETS_FILE = "enhancer_presets.json"  # %APPDATA%\PromptWordCrafter に置く (comfy_*.txt と同じ場所)
GENERATE_FILE = "enhancer_generate.txt"  # 生成の画面で前回選んだエンハンサーの番号 (0 = なし)
PROMPT_MARK = "{prompt}"  # user 側の雛形で、編集欄の内容に置き換わる目印
DEFAULT_TEMPERATURE = 0.7


@dataclass
class EnhancerPreset:
    name: str = ""
    system: str = ""
    template: str = PROMPT_MARK
    temperature: float = DEFAULT_TEMPERATURE

    @property
    def is_empty(self) -> bool:
        return not self.system.strip() and self.template.strip() in ("", PROMPT_MARK)

    def label(self, index: int) -> str:
        if self.is_empty:
            text = "(未登録)"
        else:
            text = self.name or "(名前なし)"
        return f"{index + 1}: {text}"


# 初めて使うときに 1・2 番に入れておく例。モデルごとの推奨の指示文に差し替えて使う
_EXAMPLES = [
    EnhancerPreset(
        name="例: 自然文 (英語)",
        system=(
            "You are a prompt engineer for text-to-image and text-to-video models.\n"
            "Rewrite the user's prompt into one richly detailed English description in natural sentences: "
            "subject, appearance, action, setting, lighting, camera and style.\n"
            "- Keep every subject and the intent of the original. Do not change who or what is depicted.\n"
            "- The input may be in Japanese or a list of tags; the output must always be English prose.\n"
            "- Keep weighting syntax such as (word:1.2) and special tags such as <lora:...> exactly as written.\n"
            "Output only the rewritten prompt, with no explanation, title or quotation marks."
        ),
    ),
    EnhancerPreset(
        name="例: タグ (カンマ区切り)",
        system=(
            "You are a prompt engineer for anime-style text-to-image models that use comma-separated tags.\n"
            "Expand the user's prompt into a comma-separated list of English tags: keep the original tags first, "
            "then add tags for details, composition, lighting and quality.\n"
            "- Keep every subject and the intent of the original.\n"
            "- The input may be in Japanese; translate it into tags.\n"
            "- Keep weighting syntax such as (word:1.2) and special tags such as <lora:...> exactly as written.\n"
            "Output only the tag list on one line, with no explanation."
        ),
    ),
]


# ----------------------------------------------------------------------
# プリセットの読み書き
# ----------------------------------------------------------------------
def load_presets() -> list[EnhancerPreset]:
    presets = [EnhancerPreset() for _ in range(PRESET_COUNT)]
    text = comfy.load(PRESETS_FILE)
    if text is None:
        for i, example in enumerate(_EXAMPLES):
            presets[i] = EnhancerPreset(**example.__dict__)
        return presets
    try:
        items = json.loads(text)
    except ValueError:
        return presets
    if not isinstance(items, list):
        return presets
    for i, item in enumerate(items[:PRESET_COUNT]):
        if not isinstance(item, dict):
            continue
        try:
            temperature = float(item.get("temperature", DEFAULT_TEMPERATURE))
        except (TypeError, ValueError):
            temperature = DEFAULT_TEMPERATURE
        presets[i] = EnhancerPreset(
            name=str(item.get("name", "")),
            system=str(item.get("system", "")),
            template=str(item.get("template", PROMPT_MARK)),
            temperature=temperature,
        )
    return presets


def save_presets(presets: list[EnhancerPreset]) -> None:
    data = [p.__dict__ for p in presets[:PRESET_COUNT]]
    comfy.save(PRESETS_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def last_generate_index() -> int:
    """生成の画面で前回選んだエンハンサー (0 始まり。なしなら -1)。"""
    try:
        n = int(comfy.load(GENERATE_FILE) or "")
    except ValueError:
        return -1
    return n - 1 if 1 <= n <= PRESET_COUNT else -1


def set_last_generate_index(index: int) -> None:
    comfy.save(GENERATE_FILE, str(index + 1 if index >= 0 else 0))


def snapshot(preset: EnhancerPreset, base_url: str, model: str) -> dict:
    """ジョブに持たせる、登録した時点のエンハンサーの写し (あとで設定を変えても、頼んだときの内容で盛る)。"""
    return {**preset.__dict__, "url": base_url, "model": model}


# ----------------------------------------------------------------------
# 盛る
# ----------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```$", re.DOTALL)
_QUOTES = ('"', "“", "「")
_CLOSE_QUOTES = {'"': '"', "“": "”", "「": "」"}


def user_message(template: str, text: str) -> str:
    """雛形の {prompt} を編集欄の内容に置き換える。目印がなければ雛形のあとに付ける。"""
    if PROMPT_MARK in template:
        return template.replace(PROMPT_MARK, text)
    return template.rstrip() + "\n\n" + text if template.strip() else text


def clean(text: str) -> str:
    """指示しても付けてくることがある、全体を囲むコードブロックや引用符を外す。"""
    text = text.strip()
    match = _FENCE_RE.match(text)
    if match:
        text = match.group(1).strip()
    for open_quote in _QUOTES:
        close_quote = _CLOSE_QUOTES[open_quote]
        if len(text) >= 2 and text.startswith(open_quote) and text.endswith(close_quote):
            inner = text[1:-1]
            if open_quote not in inner and close_quote not in inner:
                text = inner.strip()
            break
    return text


def enhance(base_url: str, model: str, preset: EnhancerPreset, text: str) -> str:
    reply = llm.chat(base_url, model, preset.system, user_message(preset.template, text), preset.temperature)
    return clean(reply)


def enhance_with(snap: dict, text: str) -> str:
    """snapshot() の写しで盛る (生成ジョブから使う)。"""
    try:
        temperature = float(snap.get("temperature", DEFAULT_TEMPERATURE))
    except (TypeError, ValueError):
        temperature = DEFAULT_TEMPERATURE
    preset = EnhancerPreset(
        str(snap.get("name", "")), str(snap.get("system", "")), str(snap.get("template", PROMPT_MARK)), temperature
    )
    return enhance(str(snap.get("url", "")), str(snap.get("model", "")), preset, text)


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
# 設定の窓
# ----------------------------------------------------------------------
def _move_button(text: str, tip: str) -> QPushButton:
    """並び替えのボタン (押し続けると繰り返す)。生成の設定と同じ作り。"""
    button = QPushButton(text)
    button.setToolTip(tip)
    button.setAutoRepeat(True)
    button.setAutoRepeatDelay(400)
    button.setAutoRepeatInterval(120)
    return button


class EnhancerSettingsDialog(QDialog):
    """LLM のモデルと、エンハンサーのプリセット。

    プリセットは「プリセットを保存」「削除」「▲ 上へ」「▼ 下へ」でその場で書き込む (キャンセルしても残る)。
    モデルは OK で決まる。
    """

    _models_done = Signal(object, object)

    def __init__(self, base_url: str, model: str, index: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("エンハンサーの設定")
        self.resize(680, 620)
        self.base_url = base_url
        self.presets = load_presets()
        self.shown = -1
        self._relabeling = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setEditText(model)
        model_row.addWidget(self.model_combo, 1)
        self.models_button = QPushButton("モデル取得")
        model_row.addWidget(self.models_button)
        form.addRow("LLM のモデル", model_row)
        layout.addLayout(form)
        self.status_label = QLabel(f"接続先は「翻訳の設定」のサーバーURL ({base_url}) を使います。")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #666;")
        layout.addWidget(self.status_label)
        layout.addSpacing(6)

        layout.addWidget(QLabel("エンハンサーのプリセット (生成 AI のモデルごとに登録します)"))
        self.box = QComboBox()
        self.box.setMaxVisibleItems(20)
        for i, preset in enumerate(self.presets):
            self.box.addItem(preset.label(i))
        layout.addWidget(self.box)

        preset_form = QFormLayout()
        self.name_edit = QLineEdit()
        preset_form.addRow("名前", self.name_edit)
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setDecimals(2)
        self.temperature_spin.setToolTip("高いほど毎回違う盛り方になります (目安 0.5〜1.0)")
        preset_form.addRow("temperature", self.temperature_spin)
        layout.addLayout(preset_form)

        layout.addWidget(QLabel("指示文 (system) — モデルごとに推奨されている指示文を貼ります"))
        self.system_edit = QPlainTextEdit()
        layout.addWidget(self.system_edit, 3)
        layout.addWidget(QLabel(f"送る文の雛形 (user) — {PROMPT_MARK} が編集欄の内容に置き換わります"))
        self.template_edit = QPlainTextEdit()
        self.template_edit.setToolTip(
            f"ふつうは {PROMPT_MARK} だけで十分です。{PROMPT_MARK} がないときは、雛形のあとに編集欄の内容を付けて送ります。"
        )
        layout.addWidget(self.template_edit, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        up = _move_button("▲ 上へ", "選んでいるプリセットを 1 つ上の番号と入れ替えます (押し続けると続けて動きます)")
        down = _move_button("▼ 下へ", "選んでいるプリセットを 1 つ下の番号と入れ替えます (押し続けると続けて動きます)")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        buttons.addWidget(up)
        buttons.addWidget(down)
        buttons.addSpacing(24)
        save_button = QPushButton("プリセットを保存")
        delete_button = QPushButton("削除")
        save_button.clicked.connect(self.store)
        delete_button.clicked.connect(self.delete)
        buttons.addWidget(save_button)
        buttons.addWidget(delete_button)
        layout.addLayout(buttons)

        ok_cancel = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_cancel.accepted.connect(self.accept)
        ok_cancel.rejected.connect(self.reject)
        layout.addWidget(ok_cancel)

        self.box.currentIndexChanged.connect(self._on_select)
        self.models_button.clicked.connect(self.fetch_models)
        self._models_done.connect(self._on_models, Qt.ConnectionType.QueuedConnection)
        self.select(min(max(index, 0), PRESET_COUNT - 1))

    def model(self) -> str:
        return self.model_combo.currentText().strip()

    # ---- プリセット ----
    def select(self, index: int):
        self.box.setCurrentIndex(index)
        self._on_select(index)

    def _on_select(self, index: int):
        """番号を選ぶと、その中身を欄に出す (保存していない入力は捨てる)。"""
        if self._relabeling or index < 0:
            return
        self.shown = index
        self._show(self.presets[index])

    def _show(self, preset: EnhancerPreset):
        self.name_edit.setText(preset.name)
        self.system_edit.setPlainText(preset.system)
        self.template_edit.setPlainText(preset.template)
        self.temperature_spin.setValue(preset.temperature)

    def _edited(self) -> EnhancerPreset:
        return EnhancerPreset(
            name=self.name_edit.text().strip(),
            system=self.system_edit.toPlainText().strip(),
            template=self.template_edit.toPlainText().strip() or PROMPT_MARK,
            temperature=round(self.temperature_spin.value(), 2),
        )

    def store(self):
        self.presets[self.shown] = self._edited()
        self._saved()

    def delete(self):
        self.presets[self.shown] = EnhancerPreset()
        self._show(self.presets[self.shown])
        self._saved()

    def _saved(self):
        self._relabeling = True
        self.box.setItemText(self.shown, self.presets[self.shown].label(self.shown))
        self.box.setCurrentIndex(self.shown)
        self._relabeling = False
        save_presets(self.presets)

    def _move(self, step: int):
        """隣の番号と入れ替えて、すぐ保存する。欄の内容 (保存していない入力も) はそのまま付いていく。"""
        a, b = self.shown, self.shown + step
        if a < 0 or b < 0 or b >= len(self.presets):
            return
        self.presets[a], self.presets[b] = self.presets[b], self.presets[a]
        self._relabeling = True
        self.box.setItemText(a, self.presets[a].label(a))
        self.box.setItemText(b, self.presets[b].label(b))
        self.box.setCurrentIndex(b)
        self._relabeling = False
        self.shown = b
        save_presets(self.presets)
        # 生成の画面で前回選んだ番号も、入れ替えに合わせて付いていく
        last = last_generate_index()
        if last in (a, b):
            set_last_generate_index(b if last == a else a)

    # ---- モデル一覧 ----
    def fetch_models(self):
        self.models_button.setEnabled(False)
        self.status_label.setText("モデル一覧を取得中...")
        url = self.base_url
        llm.run_in_thread(lambda: llm.list_models(url), self._models_done)

    def _on_models(self, models, error):
        self.models_button.setEnabled(True)
        if error:
            self.status_label.setText("モデル取得失敗: " + error)
            return
        if not models:
            self.status_label.setText("モデルが見つかりませんでした。")
            return
        current = self.model_combo.currentText().strip()
        picked = llm.pick_model(current, models)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.setEditText(picked)
        message = f"モデル {len(models)} 件を取得しました。"
        if picked != current:
            message += f"「{current}」はサーバーにないので「{picked}」にしました。"
        self.status_label.setText(message)


# ----------------------------------------------------------------------
# メインウィンドウから使う部分
# ----------------------------------------------------------------------
class EnhancerController(QObject):
    """エンハンサーの選択・「盛る」・「エンハンサーの設定」と小窓を受け持つ。

    依頼は同時に 1 件だけ (盛っている間は「盛る」を押せない)。
    結果はファイルを切り替えても小窓に残す (新しいファイルに貼るときなどに消えないように)。
    """

    _done = Signal(object, object)  # 結果, エラー文

    def __init__(self, editor: QTextEdit, settings_data: dict, base_url: Callable[[], str],
                 current_name: Callable[[], str], parent: QWidget):
        super().__init__(parent)
        self.parent_widget = parent
        self.editor = editor
        self._base_url = base_url  # 翻訳の設定のサーバーURL (設定を変えたらすぐ使えるよう、呼ぶたびに聞く)
        self._current_name = current_name  # 表示中のファイル名
        self.model = settings_data.get("enhance_model") or settings_data.get("translate_model") or DEFAULT_MODEL
        self.presets = load_presets()
        self._busy = False
        self._source = ""  # 盛った元のファイル名 (小窓の見出し用)
        self._running = EnhancerPreset()  # 盛っている最中のエンハンサー

        self.combo = QComboBox()
        self.combo.setToolTip("使うエンハンサー (生成 AI のモデルごとの書き方)")
        self.combo.setMinimumContentsLength(8)
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._fill_combo(self._valid_index(settings_data.get("enhance_preset", 0)))
        self.enhance_button = QPushButton("盛る")
        self.enhance_button.setToolTip("編集欄の今の内容を AI で書き足し、右上の小窓に出します (ファイルは変えません)")
        self.settings_button = QPushButton("設定")  # グループ名が「エンハンサー (AI)」なので短く
        self.settings_button.setToolTip("エンハンサーの設定 (指示文の登録、LLM のモデル)")

        self.popup = EnhancerPopup(editor)

        self.enhance_button.clicked.connect(self.enhance)
        self.popup.again_requested.connect(self.enhance)
        self.settings_button.clicked.connect(self.show_settings)
        self._done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    def _valid_index(self, index) -> int:
        try:
            index = int(index)
        except (TypeError, ValueError):
            return 0
        return index if 0 <= index < PRESET_COUNT else 0

    def _fill_combo(self, index: int):
        self.combo.clear()
        for i, preset in enumerate(self.presets):
            self.combo.addItem(preset.label(i))
        self.combo.setCurrentIndex(index)

    def settings(self) -> dict:
        return {"enhance_model": self.model, "enhance_preset": max(self.combo.currentIndex(), 0)}

    def _title(self, prefix: str, preset: EnhancerPreset) -> str:
        name = preset.name or f"{self.combo.currentIndex() + 1} 番"
        return f"{prefix} ({name}): {self._source}" if self._source else f"{prefix} ({name})"

    # ---- 盛る ----
    def enhance(self):
        if self._busy:
            return
        self._source = self._current_name()
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
        base_url, model = self._base_url(), self.model
        self._running = preset
        llm.run_in_thread(lambda: enhance(base_url, model, preset, text), self._done)

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
        dialog = EnhancerSettingsDialog(self._base_url(), self.model, self.combo.currentIndex(), self.parent_widget)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted and dialog.model():
            self.model = dialog.model()
        # プリセットは窓の中で保存済み (キャンセルしても残る)。並び替えたら選んでいた番号も付いていく
        self.presets = load_presets()
        self._fill_combo(dialog.shown if accepted else self.combo.currentIndex())
