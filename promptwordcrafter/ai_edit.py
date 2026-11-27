"""AI に指示 (2026-11-27 利用者の希望): 「背景を紅葉の森に変えてください。」のような指示で、編集欄の本文を書き換える。

LLM の接続とモデルは「盛る」「項目整理」と同じもの (エンハンサーの設定のサーバーURL とモデル) を使う。
返事は書き換えたプロンプトだけにさせ、編集欄に丸ごと入れる (Ctrl+Z で戻せる。ファイルへは「保存」で)。
最近の指示は設定 (promptwordcrafter_settings.json の ai_edit_history) に残し、窓で選び直せる。
"""

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

from . import organizer

TEMPERATURE = 0.3
HISTORY_MAX = 20

SYSTEM = (
    "You edit prompts for image and video generation models.\n"
    "You receive a prompt and an instruction from the user. Rewrite the prompt so that it follows the instruction.\n"
    "Rules:\n"
    "- Change only what the instruction asks for. Keep everything else as it is: wording, order, details, "
    "line breaks and section headers such as [背景・小道具].\n"
    "- Keep weighting syntax such as (word:1.2), wildcards such as {a|b} and special tags such as <lora:...> "
    "exactly as written, unless the instruction is about them.\n"
    "- The instruction may be written in a different language from the prompt. Write your changes in the "
    "language of the prompt, not of the instruction, unless the instruction asks for a language.\n"
    "- Output only the whole rewritten prompt. No explanation, no notes, no quotes, no code block."
)


def user_message(text: str, instruction: str) -> str:
    return f"Instruction:\n{instruction.strip()}\n\nPrompt:\n{text}"


def rewrite(text: str, instruction: str, base_url: Optional[str] = None,
            model: Optional[str] = None) -> tuple[str, bool]:
    """(書き換えた本文, 長さの上限で打ち切られたか)。返事が空なら llm.LLMError。"""
    base_url = base_url or settings.llm_url()
    model = model or settings.enhance_model()
    system = SYSTEM + organizer.language_rule(text)
    reply, cut = llm.chat_full(base_url, model, system, user_message(text, instruction), TEMPERATURE)
    result = presets.clean(reply)
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

