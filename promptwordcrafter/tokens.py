"""おおよそのトークン数と、AI (盛る・項目整理) に長さの上限を伝える部分。

数え方は LLM 系の目安 (2026-11-26 利用者の決定。正確でなくてよい。LM Studio の Qwen 系の数で合わせた):
英語などの単語は 1 語 ≒ 1 トークン (長い語は 6 文字ごとに 1)、数字は 3 桁ごとに 1、
漢字・かなは 1 文字 ≒ 0.65、記号は 1 文字 ≒ 1。空白と改行は数えない。
生成のモデルの CLIP・T5 は数え方が違う (英語で 1〜3 割多い。T5 の日本語はずっと多い)。

上限は「約 N トークン以内で」と LLM に指示し、さらに N の 1.5 倍 (+ 余裕) で返事を打ち切ってもらう
(指示を守らなかったときの歯止め。ぴったりで切ると文が途中で切れるので余裕を持たせる)。
"""

import math
import re

LIMIT_MAX = 4000  # 画面で選べる上限の最大
LIMIT_STEP = 50
CUT_RATIO = 1.5  # 返事を打ち切る長さ = 上限 × これ + CUT_MARGIN
CUT_MARGIN = 32

_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ']+|[0-9]+|\S")
_LETTER_RE = re.compile(r"[A-Za-zÀ-ɏ]")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
JA_PER_CHAR = 0.65  # 漢字・かな 1 文字 (Qwen 系で 0.6 前後)


def estimate(text: str) -> int:
    """おおよそのトークン数。"""
    count = 0.0
    for piece in _WORD_RE.findall(text or ""):
        first = piece[0]
        if first.isdigit():
            count += math.ceil(len(piece) / 3)
        elif len(piece) > 1 or _LETTER_RE.match(first):
            # ふつうの語は 1 トークン、長い語は 6 文字ごとくらいに分かれる
            count += max(1.0, len(piece) / 6)
        elif _CJK_RE.match(first):
            count += JA_PER_CHAR
        else:
            count += 1  # 記号
    return int(round(count))


def label(text: str) -> str:
    return f"約 {estimate(text):,} トークン"


def cut_length(limit: int) -> int:
    """返事を打ち切ってもらう長さ (上限なし = 0 のときは 0)。"""
    return int(limit * CUT_RATIO) + CUT_MARGIN if limit > 0 else 0


def instruction(limit: int) -> str:
    """指示文の最後に付ける、長さの上限の説明 (上限なしなら空)。"""
    if limit <= 0:
        return ""
    # 「日本語なら何文字」と書くと日本語に訳してしまうモデルがあったので、言語には触れない
    return (
        f"\n\nLength limit: keep the whole output within about {limit} tokens. "
        "If the content would be longer, condense it and keep the most important details. "
        "Do not change the language because of this limit, and do not mention the limit in the output."
    )
