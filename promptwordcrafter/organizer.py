"""項目整理 (AI): 本文を [登場人物] [衣装] … の項目ごとに分けて書き直す。

LLM の接続とモデルは「盛る」と同じもの (エンハンサーの設定のサーバーURL とモデル) を使う。
結果は編集欄にそのまま入れる (Ctrl+Z で戻せる。ファイルへは「保存」で)。
本文が空のときは LLM を使わず、項目名だけを並べて書き足せるようにする。
"""

import re
from typing import Optional

from gencommon import llm, presets, settings

from . import tokens

SECTIONS = ("登場人物", "衣装", "ポーズ・動作", "背景・小道具", "構図", "照明・画風")
TEMPERATURE = 0.2

_HEADER_RE = re.compile(r"^\s*\[[^\[\]\n]+\]\s*$", re.MULTILINE)
# 「[衣装] 黒いジャケット」のように同じ行に内容を書いてくるモデルもある。項目名は短く、: や | を含まないもの
# (プロンプトの [a:b:0.5] などの書き方と区別する)
_INLINE_RE = re.compile(r"^\s*(\[[^\[\]\n:|]{1,20}\])[ \t:：]*(\S.*)$")

SYSTEM = (
    "You reorganize prompts for image and video generation models into labeled sections.\n"
    "Standard sections, in this order: " + " ".join(f"[{s}]" for s in SECTIONS) + "\n"
    "Rules:\n"
    "- Put every piece of the input under the section it belongs to. Keep the original wording and language: "
    "English stays English, Japanese stays Japanese. Never translate. Do not add new details or drop details.\n"
    "- Only the section names are in Japanese.\n"
    "- If some content fits none of the standard sections, invent a short, fitting section name in Japanese "
    "from the content (for example [表情] or [カメラワーク]) and put it after the standard sections. "
    "Group similar content under one such section.\n"
    "- Leave out standard sections that would be empty.\n"
    "- Keep weighting syntax such as (word:1.2) and special tags such as <lora:...> exactly as written.\n"
    "- Output format: each section name in square brackets on its own line, its content on the following lines, "
    "and one blank line between sections.\n"
    "Output only the organized text, with no explanation."
)


def template() -> str:
    """本文が空のときに入れる、項目名だけのもの (項目の下に 1 行空けて書き足せるように)。"""
    return "\n\n".join(f"[{s}]\n" for s in SECTIONS)


def organize(text: str, limit: int = 0, base_url: Optional[str] = None, model: Optional[str] = None) -> tuple[str, bool]:
    """(整理した本文, 長さの上限で打ち切られたか)。"""
    base_url = base_url or settings.llm_url()
    model = model or settings.enhance_model()
    system = SYSTEM + language_rule(text) + tokens.instruction(limit)
    if limit > 0:
        system += " Shorten by leaving out less important words, never by translating."
    reply, cut = llm.chat_full(base_url, model, system, text, TEMPERATURE,
                               max_tokens=tokens.cut_length(limit) or None)
    result = split_inline(presets.clean(reply))
    if not _HEADER_RE.search(result):
        raise llm.LLMError("AI の返事が項目ごとに分かれていませんでした。もう一度試すか、モデルを替えてください。")
    return tidy(result), cut


_JA_RE = re.compile(r"[぀-ヿ㐀-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def language_rule(text: str) -> str:
    """本文の言語を決めて伝える (項目名が日本語なので、英語の本文を日本語に訳してしまうモデルがあった)。"""
    ja, latin = len(_JA_RE.findall(text)), len(_LATIN_RE.findall(text))
    if latin and ja * 3 < latin:
        return "\n\nThe input is written in English. Write all the content in English, exactly as in the input."
    if ja and latin * 3 < ja * 2:
        return "\n\nThe input is written in Japanese. Write all the content in Japanese, exactly as in the input."
    return "\n\nThe input mixes languages. Keep every part in the language it is written in."


def split_inline(text: str) -> str:
    """「[項目] 内容」の行を、項目名の行と内容の行に分ける。"""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _INLINE_RE.match(line)
        if match:
            out.extend([match.group(1), match.group(2)])
        else:
            out.append(line)
    return "\n".join(out)


def tidy(text: str) -> str:
    """項目のあいだを 1 行空けにそろえる。"""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if _HEADER_RE.match(line):
            while out and not out[-1]:
                out.pop()
            if out:
                out.append("")
            out.append(line.strip())
        elif line or (out and out[-1] and not _HEADER_RE.match(out[-1])):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n"
