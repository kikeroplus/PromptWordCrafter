"""フォルダ・複数ファイルに対する一括操作。"""

import re
import shutil
from pathlib import Path

from . import text_io


def backup_if_needed(path: Path) -> Path:
    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
    return backup_path


def plan_sequential_rename(
    paths: list[Path], prefix: str, start: int, digits: int
) -> list[tuple[Path, Path]]:
    """連番リネームの計画を作成する（実行はしない）。"""
    plans = []
    number = start
    for path in paths:
        new_name = f"{prefix}{number:0{digits}d}{path.suffix}"
        plans.append((path, path.with_name(new_name)))
        number += 1
    return plans


def apply_renames(plans: list[tuple[Path, Path]]) -> None:
    """(旧パス, 新パス) の計画を実行する。衝突を避けるため一時名を経由する。"""
    temp_plans = []
    for index, (old_path, _new_path) in enumerate(plans):
        temp_path = old_path.with_name(f"__pwc_rename_tmp_{index}__{old_path.name}")
        old_path.rename(temp_path)
        temp_plans.append(temp_path)

    for temp_path, (_old_path, new_path) in zip(temp_plans, plans):
        temp_path.rename(new_path)


def add_text_to_file(path: Path, text: str, position: str) -> None:
    """position: 'start' または 'end'。"""
    content, encoding = text_io.read_text(path)
    new_content = text + content if position == "start" else content + text
    backup_if_needed(path)
    path.write_text(new_content, encoding=encoding, newline="")


CR = chr(13)
LF = chr(10)
NBSP = chr(0xA0)
LINE_SEP = chr(0x2028)
PARA_SEP = chr(0x2029)

# 編集欄（toPlainText）で改行として扱われるものすべてに一致する
_LINE_BREAK = "(?:" + "|".join([CR + LF, CR, LF, LINE_SEP, PARA_SEP]) + ")"
_SPACE = "[ " + NBSP + "]"


def _build_pattern(needle: str) -> re.Pattern:
    """編集欄（toPlainText）と同じ揺れを許容する検索パターンを作る。

    空白は NBSP にも、改行は CRLF / CR / LF / U+2028 / U+2029 のどれにも一致する。
    """
    needle = needle.replace(CR + LF, LF).replace(NBSP, " ")
    parts = []
    for ch in needle:
        if ch == " ":
            parts.append(_SPACE)
        elif ch == LF:
            parts.append(_LINE_BREAK)
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts))


def _replace_in_content(content: str, needle: str, replacement: str) -> tuple[str, int]:
    pattern = _build_pattern(needle)
    newline = CR + LF if (CR + LF) in content else LF
    replacement = replacement.replace(CR + LF, LF).replace(LF, newline)
    return pattern.subn(lambda _match: replacement, content)


def remove_text_from_file(path: Path, needle: str) -> int:
    return replace_text_in_file(path, needle, "")


def replace_text_in_file(path: Path, needle: str, replacement: str) -> int:
    content, encoding = text_io.read_text(path)
    new_content, count = _replace_in_content(content, needle, replacement)
    if count == 0:
        return 0
    backup_if_needed(path)
    path.write_text(new_content, encoding=encoding, newline="")
    return count


def suggest_new_file_name(existing_names: list[str]) -> str:
    """一覧の最後のファイル名の末尾番号を +1 した名前を返す（桁数・拡張子は維持）。"""
    taken = {name.lower() for name in existing_names}
    if not existing_names:
        candidate = "prompt_001.txt"
        number = 1
        while candidate.lower() in taken:
            number += 1
            candidate = f"prompt_{number:03d}.txt"
        return candidate

    last = Path(existing_names[-1])
    match = re.match(r"^(.*?)(\d+)$", last.stem)
    if match:
        prefix, digits = match.group(1), match.group(2)
        number = int(digits)
        width = len(digits)
    else:
        prefix, number, width = f"{last.stem}_", 1, 1

    while True:
        number += 1
        candidate = f"{prefix}{number:0{width}d}{last.suffix}"
        if candidate.lower() not in taken:
            return candidate


def create_empty_file(folder: Path, name: str) -> Path:
    path = folder / name
    with path.open("x", encoding="utf-8"):
        pass
    return path


def delete_bak_files(folder: Path) -> int:
    count = 0
    for path in folder.iterdir():
        if path.is_file() and path.suffix.lower() == ".bak":
            path.unlink()
            count += 1
    return count


def reformat_sentences(text: str) -> str:
    """「。」「.」の直後に改行を追加する。既に改行がある場合は二重にしない。"""
    pieces = []
    for ch in text:
        pieces.append(ch)
        if ch in "。.":
            pieces.append("\n")
    result = "".join(pieces)
    return result.replace("。\n\n", "。\n").replace(".\n\n", ".\n")
