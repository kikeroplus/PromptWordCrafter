"""MiniMax H3 用のタグ強調表示。

<d> ～ </d> の間（タグ自体を含む）を常時青い文字で表示する。
さらに、[Japanese] の次の文字から </d> の手前までをセリフ文字部分として
背景ハイライト表示する。どちらも複数行にまたがるブロックに対応する。
"""

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat

TAG_OPEN = "<d>"
TAG_CLOSE = "</d>"
SPEECH_MARK = "[Japanese]"

TAG_TEXT_COLOR = QColor("#1a56db")
SPEECH_HIGHLIGHT_BACKGROUND = QColor("#ffe0b2")


class TagHighlighter(QSyntaxHighlighter):
    IN_TAG = 1
    IN_SPEECH = 2

    def __init__(self, document):
        super().__init__(document)
        self.tag_format = QTextCharFormat()
        self.tag_format.setForeground(TAG_TEXT_COLOR)

        self.speech_format = QTextCharFormat()
        self.speech_format.setBackground(SPEECH_HIGHLIGHT_BACKGROUND)

    def highlightBlock(self, text: str):
        previous = self.previousBlockState()
        if previous == -1:
            previous = 0

        tag_still_open = self._highlight_span(
            text,
            TAG_OPEN,
            TAG_CLOSE,
            continuing=bool(previous & self.IN_TAG),
            fmt=self.tag_format,
            skip_open=False,
            include_close=True,
        )
        speech_still_open = self._highlight_span(
            text,
            SPEECH_MARK,
            TAG_CLOSE,
            continuing=bool(previous & self.IN_SPEECH),
            fmt=self.speech_format,
            skip_open=True,
            include_close=False,
        )

        state = 0
        if tag_still_open:
            state |= self.IN_TAG
        if speech_still_open:
            state |= self.IN_SPEECH
        self.setCurrentBlockState(state)

    def _highlight_span(
        self,
        text: str,
        open_token: str,
        close_token: str,
        continuing: bool,
        fmt: QTextCharFormat,
        skip_open: bool,
        include_close: bool,
    ) -> bool:
        pos = 0 if continuing else text.find(open_token)
        still_open = False

        while pos >= 0:
            if continuing:
                mark_start = 0
            else:
                mark_start = pos + len(open_token) if skip_open else pos

            close_index = text.find(close_token, mark_start)
            if close_index == -1:
                mark_end = len(text)
                still_open = True
            else:
                mark_end = close_index + len(close_token) if include_close else close_index

            if mark_end > mark_start:
                self.setFormat(mark_start, mark_end - mark_start, fmt)

            if close_index == -1:
                break

            continuing = False
            pos = text.find(open_token, close_index + len(close_token))

        return still_open
