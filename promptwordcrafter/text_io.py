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


def list_text_files(folder: Path, recursive: bool = False) -> list[Path]:
    """folder のテキストファイル (一覧と同じ判定) を名前順に返す。

    recursive のときはサブフォルダもたどる (フォルダごとに、そのフォルダのファイル → サブフォルダの順)。
    読めないフォルダと、"." で始まる隠しフォルダは飛ばす。
    """
    try:
        entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
    files = [path for path in entries if is_text_file(path)]
    if recursive:
        for path in entries:
            if path.is_dir() and not path.is_symlink() and not path.name.startswith("."):
                files.extend(list_text_files(path, recursive=True))
    return files


def read_text(path: Path) -> tuple[str, str]:
    """優先順位に沿って文字コードを判定しながらファイルを読み込む。"""
    data = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"
