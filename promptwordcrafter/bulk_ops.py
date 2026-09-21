"""フォルダ・複数ファイルに対する一括操作。"""

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


def remove_text_from_file(path: Path, needle: str) -> int:
    content, encoding = text_io.read_text(path)
    count = content.count(needle)
    if count == 0:
        return 0
    new_content = content.replace(needle, "")
    backup_if_needed(path)
    path.write_text(new_content, encoding=encoding, newline="")
    return count


def replace_text_in_file(path: Path, needle: str, replacement: str) -> int:
    content, encoding = text_io.read_text(path)
    count = content.count(needle)
    if count == 0:
        return 0
    new_content = content.replace(needle, replacement)
    backup_if_needed(path)
    path.write_text(new_content, encoding=encoding, newline="")
    return count


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
