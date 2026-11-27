"""AI に指示 (2026-11-27 利用者の希望): 「背景を紅葉の森に変えてください。」のような指示で、編集欄の本文を書き換える。

LLM の接続とモデルは「盛る」「項目整理」と同じもの (エンハンサーの設定のサーバーURL とモデル) を使う。
返事は書き換えたプロンプトだけにさせ、編集欄に丸ごと入れる (Ctrl+Z で戻せる。ファイルへは「保存」で)。
最近の指示は設定 (promptwordcrafter_settings.json の ai_edit_history) に残し、窓で選び直せる。
"""

import re
from typing import Optional

from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from gencommon import llm, presets, settings

TEMPERATURE = 0.7  # チャットで頼むときと同じくらい (全体を書き直す指示もあるので、縛らない)
HISTORY_MAX = 20

# 2026-11-27 利用者の希望: チャットで「下記のプロンプトを、…に書き直してください。プロンプトのみを出力してください。」
# と頼んだときのように、全体を書き換えてよい。なので指示文 (system) は使わず、そのとおりの頼み方だけを送る
# (前は「指示にないところは変えない」「本文の言語のまま」などで縛っていて、控えめにしか変わらなかった)。


def user_message(text: str, instruction: str) -> str:
    return (f"下記のプロンプトを、次の指示どおりに書き直してください。\n"
            f"指示: {instruction.strip()}\n"
            f"プロンプトのみを出力してください。Markdown の装飾 (** など) や見出し・説明は付けないでください。\n\n"
            f"{text}")


# 返事の先頭の「**書き直したプロンプト:**」「Prompt:」のような見出しだけの行
_LABEL_RE = re.compile(r"^[#>*_\s]*[^\n]{0,30}(プロンプト|prompt)[^\n]{0,10}?[:：]\s*[*_]*\s*$", re.IGNORECASE)


def strip_markdown(text: str) -> str:
    """モデルが付けてくる Markdown の太字 (**…**) と、先頭の見出しの行を外す (2026-11-27 利用者の指摘)。"""
    text = text.replace("**", "")
    lines = text.strip().split("\n")
    if len(lines) > 1 and _LABEL_RE.match(lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def rewrite(text: str, instruction: str, base_url: Optional[str] = None,
            model: Optional[str] = None) -> tuple[str, bool]:
    """(書き換えた本文, 長さの上限で打ち切られたか)。返事が空なら llm.LLMError。"""
    base_url = base_url or settings.llm_url()
    model = model or settings.enhance_model()
    reply, cut = llm.chat_full(base_url, model, "", user_message(text, instruction), TEMPERATURE)
    result = presets.clean(strip_markdown(presets.clean(reply)))
    if not result.strip():
        raise llm.LLMError("AI の返事が空でした。もう一度試すか、モデルを替えてください。")
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"  # 元の終わりの改行はそろえる
    return result, cut


def add_history(history: list, instruction: str) -> list:
    """最近の指示の先頭に入れる (同じものは前のものを外す)。"""
    instruction = instruction.strip()
    rest = [h for h in history if isinstance(h, str) and h.strip() and h.strip() != instruction]
    return ([instruction] + rest)[:HISTORY_MAX]


class InstructionDialog(QDialog):
    """AI への指示を入れる窓。Ctrl+Enter でも書き換える。"""

    def __init__(self, history: list, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("AI に指示")
        self.resize(560, 300)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("編集欄の本文を、指示どおりに AI で書き換えます (Ctrl+Z で戻せます)。"))
        self.history = QComboBox()
        self.history.addItem("最近の指示から選ぶ...")
        for h in history:
            self.history.addItem(" ".join(h.split())[:80], h)
        self.history.setEnabled(bool(history))
        self.history.activated.connect(self._pick)
        layout.addWidget(self.history)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("例: このプロンプトの背景を紅葉の森に変えてください。")
        if history:
            self.edit.setPlainText(history[0])
            self.edit.selectAll()
        layout.addWidget(self.edit, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("書き換える")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(key), self, activated=self._accept)
        self.edit.setFocus()

    def _pick(self, index: int) -> None:
        value = self.history.itemData(index)
        if value:
            self.edit.setPlainText(value)
            self.edit.setFocus()
        self.history.setCurrentIndex(0)

    def _accept(self) -> None:
        if self.instruction():
            self.accept()

    def instruction(self) -> str:
        return self.edit.toPlainText().strip()

