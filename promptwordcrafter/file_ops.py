"""テキストファイルの削除・コピー・移動 (2026-11-26 利用者の希望)。

テキストと一緒に、そのテキストから生成した結果 (画像・動画。「生成結果」フォルダと前の置き場所のもの)、
結果に付いている説明の .txt (「生成結果」フォルダのもの)、保存のときに作るバックアップ (.bak) も動かす。
PrismLab の量産の出力 (レシピの出力フォルダにある) は動かさない (移動では記録の入口だけ付け替える)。

- 削除: すべてごみ箱へ。記録はそのまま (ファイルがなくなった結果は出てこない)。
- 移動: 行き先のフォルダへ。結果は行き先の「生成結果」フォルダへ。記録のパスも付け替える。
- コピー: 同じように写し、記録も写す (コピーしたテキストからも結果が見えるように)。
- 行き先に同じ名前のテキストがあるとき: 中身が同じならスキップ。違うなら選んだとおり
  (別名で保存「名前 (2).txt」/ スキップ / 上書き)。別名にしたら結果の名前の頭もそれに合わせる。
  結果は上書きしない (ぶつかれば「名前 (2).png」のように番号を付ける)。
"""

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gencommon import comfy

from . import results, text_io

RENAME, SKIP, OVERWRITE = "rename", "skip", "overwrite"
PASTE = "paste"  # 貼り付け (エクスプローラーと同じ): 同じ名前があれば聞かずに「名前 (1).txt」。同じフォルダにも複製できる
CONFLICT_NAMES = {RENAME: "別名で保存", SKIP: "スキップ", OVERWRITE: "上書き"}


@dataclass
class Outcome:
    texts: int = 0  # 動かした (消した) テキストの数
    results: int = 0  # 一緒に動かした結果の数
    skipped: list[str] = field(default_factory=list)  # 同じ名前があってスキップしたもの
    overwritten: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)  # 「元の名前 → 別名」
    errors: list[str] = field(default_factory=list)
    moved: dict[str, str] = field(default_factory=dict)  # テキストの {元のパス: 行き先のパス}


def _bak(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".bak")


def related(text: Path, records: Optional[list] = None) -> list[Path]:
    """テキストと一緒に動かす生成結果 (PrismLab の出力は除く)。"""
    try:
        body, _encoding = text_io.read_text(text)
    except OSError:
        body = ""
    return [r.path for r in results.find_results(text, body, records) if not r.prismlab and r.path.is_file()]


def _caption(result: Path) -> Optional[Path]:
    """結果に付いている説明の .txt (「生成結果」フォルダのものだけ。前の置き場所ではテキストと区別できない)。"""
    if result.parent.name.lower() != text_io.RESULTS_DIR.lower():
        return None
    caption = result.with_suffix(results.CAPTION_EXT)
    return caption if caption.is_file() else None


def delete_targets(texts: list[Path]) -> tuple[list[Path], list[Path]]:
    """(ごみ箱へ送るもの全部, そのうちの結果)。"""
    records = comfy.read_history()
    paths: list[Path] = []
    found: list[Path] = []
    for text in texts:
        paths.append(text)
        if _bak(text).is_file():
            paths.append(_bak(text))
        for result in related(text, records):
            paths.append(result)
            found.append(result)
            caption = _caption(result)
            if caption is not None:
                paths.append(caption)
    seen: set = set()
    unique = []
    for p in paths:
        key = os.path.normcase(os.path.abspath(str(p)))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique, found


def same_content(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _sha256(a) == _sha256(b)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _free_name(path: Path, taken: set, first: int = 2) -> Path:
    """path がふさがっていれば「名前 (2).拡張子」… の空いている名前 (貼り付けは (1) から)。"""
    candidate, i = path, first
    while candidate.exists() or os.path.normcase(str(candidate)) in taken:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        i += 1
    return candidate


def transfer(texts: list[Path], dest: Path, move: bool, conflict: str = RENAME) -> Outcome:
    """texts を dest フォルダへコピー / 移動する (結果・説明の .txt・.bak も)。"""
    out = Outcome()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        out.errors.append(f"{dest}: {exc}")
        return out
    records = comfy.read_history()
    act = shutil.move if move else shutil.copy2
    text_map: dict[str, str] = {}
    output_map: dict[str, str] = {}
    taken: set = set()  # 今回の中で使った行き先 (同じ名前のテキストを 2 つ送ったときなど)

    paste = conflict == PASTE
    first = 1 if paste else 2
    for text in texts:
        if results.same_path(text.parent, dest) and (move or not paste):
            if not paste:  # 切り取って同じフォルダに貼り付けたときは、何もしない (エクスプローラーと同じ)
                out.skipped.append(f"{text.name} (同じフォルダです)")
            continue
        target = dest / text.name
        if paste and (target.exists() or os.path.normcase(str(target)) in taken):
            target = _free_name(target, taken, first)  # 貼り付けは、聞かずに「名前 (1).txt」にする
        elif target.exists() or os.path.normcase(str(target)) in taken:
            if target.exists() and same_content(text, target):
                out.skipped.append(f"{text.name} (同じ中身があります)")
                continue
            if conflict == SKIP:
                out.skipped.append(text.name)
                continue
            if conflict == RENAME or not target.exists():
                target = _free_name(target, taken)
                out.renamed.append(f"{text.name} → {target.name}")
            else:
                out.overwritten.append(text.name)
        related_now = related(text, records)  # 動かす前に探す (記録とファイル名で結び付いている)
        try:
            if move and target.exists():
                target.unlink()  # 上書き (shutil.move は行き先があると失敗することがある)
            act(str(text), str(target))
        except OSError as exc:
            out.errors.append(f"{text.name}: {exc}")
            continue
        taken.add(os.path.normcase(str(target)))
        text_map[str(text)] = str(target)
        out.texts += 1

        bak = _bak(text)
        if bak.is_file() and not _bak(target).exists():
            try:
                act(str(bak), str(_bak(target)))
            except OSError as exc:
                out.errors.append(f"{bak.name}: {exc}")

        results_dir = dest / text_io.RESULTS_DIR
        for result in related_now:
            name = result.name
            if name.lower().startswith(text.stem.lower()):
                name = target.stem + name[len(text.stem):]  # 別名にしたテキストに名前の頭を合わせる
            try:
                results_dir.mkdir(exist_ok=True)
                final = _free_name(results_dir / name, taken, first)
                caption = _caption(result)
                act(str(result), str(final))
            except OSError as exc:
                out.errors.append(f"{result.name}: {exc}")
                continue
            taken.add(os.path.normcase(str(final)))
            output_map[str(result)] = str(final)
            out.results += 1
            if caption is not None:
                caption_target = final.with_suffix(results.CAPTION_EXT)
                if not caption_target.exists():
                    try:
                        act(str(caption), str(caption_target))
                    except OSError as exc:
                        out.errors.append(f"{caption.name}: {exc}")

    if move:
        comfy.rename_in_history({**text_map, **output_map})
        for app_name in results.READER_APPS:
            comfy.rename_in_history(output_map, app_name)  # 結果を元画像にした記録
        comfy.rename_in_history(text_map, results.PRISMLAB_APP)  # PrismLab の記録の入口
    else:
        comfy.copy_in_history({**text_map, **output_map})
    out.moved = text_map
    return out


def translated_path(source: Path, target: str) -> Path:
    """翻訳を書き出すファイル: 同じフォルダの「名前_en.txt」/「名前_ja.txt」(あれば「名前_en (1).txt」…)。"""
    return _free_name(source.with_name(f"{source.stem}_{target}{source.suffix}"), set(), 1)


# ----------------------------------------------------------------------
# クリップボード (エクスプローラーと同じ形。PWC で切り取ってエクスプローラーに貼る・その逆もできる)
# ----------------------------------------------------------------------
DROP_EFFECT = 'application/x-qt-windows-mime;value="Preferred DropEffect"'
EFFECT_COPY, EFFECT_MOVE = 5, 2  # DROPEFFECT_COPY | DROPEFFECT_LINK / DROPEFFECT_MOVE


def clipboard_data(paths: list[Path], cut: bool):
    from PySide6.QtCore import QMimeData, QUrl

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    mime.setData(DROP_EFFECT, (EFFECT_MOVE if cut else EFFECT_COPY).to_bytes(4, "little"))
    return mime


def clipboard_files(mime) -> tuple[list[Path], bool]:
    """クリップボードのテキストファイルと、切り取りか。"""
    if mime is None or not mime.hasUrls():
        return [], False
    paths = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
    paths = [p for p in paths if p.is_file() and text_io.is_text_file(p)]
    cut = False
    if mime.hasFormat(DROP_EFFECT):
        raw = bytes(mime.data(DROP_EFFECT))
        cut = len(raw) >= 4 and int.from_bytes(raw[:4], "little") & EFFECT_MOVE == EFFECT_MOVE \
            and not int.from_bytes(raw[:4], "little") & 1
    return paths, cut
