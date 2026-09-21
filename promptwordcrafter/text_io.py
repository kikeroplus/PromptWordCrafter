"""テキストファイルの読み込み・拡張子判定ユーティリティ。"""

from pathlib import Path

TEXT_EXTENSIONS = {
    ".txt",
    ".log",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".py",
    ".ini",
    ".cfg",
    ".conf",
    ".yml",
    ".yaml",
}

ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "utf-16", "latin-1")


def is_text_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS


def read_text(path: Path) -> tuple[str, str]:
    """優先順位に沿って文字コードを判定しながらファイルを読み込む。"""
    data = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"
