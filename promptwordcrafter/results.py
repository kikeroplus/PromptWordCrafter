"""プロンプトのテキストファイルと、そこから生成した画像・動画の結び付け。

結び付けは生成の記録 (comfy_history.txt) を正にし、記録のないものはファイル名
「(テキストの名前)_t2i*.png」「(テキストの名前)_video*.mp4」で拾う (「生成結果」フォルダと、
前の置き場所だったテキストと同じフォルダの両方)。
記録に残っている本文と今の本文を比べ、前の本文で作ったものには印を付ける。
"""

import ctypes
import os
import re
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageReader

from gencommon import comfy

from . import bulk_ops, text_io
from .profile import SUFFIXES, results_dir

MOVIE_EXTS = {".mp4", ".m4v", ".webm", ".mov", ".mkv"}
MEDIA_EXTS = MOVIE_EXTS | {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass
class ResultFile:
    path: Path
    is_video: bool
    old: bool  # 今の本文とは違う (前の) 本文で生成した
    time: float  # 並べ替え用 (更新日時)
    record: Optional[comfy.GenRecord] = None


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def same_path(a, b) -> bool:
    return _key(a) == _key(b)


def _same_text(a: str, b: str) -> bool:
    return bulk_ops.normalize_needle(a or "").strip() == bulk_ops.normalize_needle(b or "").strip()


def _name_pattern(stem: str) -> re.Pattern:
    """そのテキストの名前で保存した結果 (「名前_t2i (2).png」など)。"""
    suffixes = "|".join(re.escape(s) for s in SUFFIXES)
    return re.compile(rf"^{re.escape(stem)}(?:{suffixes})(?: \(\d+\))?\.[^.]+$", re.IGNORECASE)


def find_results(source: Path, current_text: str, records: Optional[list] = None) -> list[ResultFile]:
    """そのテキストファイルから生成したファイル (新しい順)。

    - 記録の元のファイルがこのテキストのもの
    - 元のファイルがもう見つからない記録で、送った本文が今の本文と同じもの
      (エクスプローラーでテキストの名前を変えたときの保険)
    - 記録はないが、名前が「(テキストの名前)_t2i / _video」で始まるもの
    """
    if records is None:
        records = comfy.read_history()
    source_key = _key(source)
    found: dict[str, ResultFile] = {}

    for record in reversed(records):  # 新しい記録を優先
        output = record.output
        if not output:
            continue
        key = _key(output)
        if key in found:
            continue
        mine = bool(record.source) and _key(record.source) == source_key
        if not mine:
            # 元のファイルがあるかの確認 (exists) は最後に回す。記録の元がつながっていない
            # ネットワークドライブなどにあると 1 回ごとに待たされ、ファイルを選ぶたびに遅くなる
            mine = (
                bool(record.source)
                and record.request.prompt is not None
                and _key(Path(output).parent) in (_key(source.parent), _key(results_dir(source)))
                and _same_text(record.request.prompt, current_text)
                and not Path(record.source).exists()
            )
        if not mine:
            continue
        path = Path(output)
        if not path.is_file():
            continue
        old = record.request.prompt is not None and not _same_text(record.request.prompt, current_text)
        found[key] = ResultFile(path, _is_video(path), old, _mtime(path), record)

    pattern = _name_pattern(source.stem)
    for folder in (results_dir(source), source.parent):  # 前はテキストと同じフォルダに置いていた
        try:
            for path in folder.iterdir():
                if path.suffix.lower() in MEDIA_EXTS and pattern.match(path.name) and _key(path) not in found:
                    found[_key(path)] = ResultFile(path, _is_video(path), False, _mtime(path))
        except OSError:
            pass

    return sorted(found.values(), key=lambda r: r.time, reverse=True)


def _is_video(path: Path) -> bool:
    return path.suffix.lower() in MOVIE_EXTS


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ----------------------------------------------------------------------
# テキストの名前を変えたとき、結果のファイルの名前と記録も付け替える
# ----------------------------------------------------------------------
def rename_results(plan: list[tuple[Path, Path]]) -> tuple[int, list[str]]:
    """plan は、テキストの (旧パス, 新パス)。テキストの名前を変えた「あと」に呼ぶ。

    結果のファイルは「旧の名前_t2i (2).png」→「新の名前_t2i (2).png」のように頭だけを変える。
    戻り値: (名前を変えた結果の数, 失敗の説明)。
    """
    records = comfy.read_history()
    text_renames = {str(old): str(new) for old, new in plan}
    moves: list[tuple[Path, Path]] = []
    for old, new in plan:
        # 名前を変えたあとなので、記録を探すときの今の本文は新しいパスから読む
        try:
            text, _encoding = text_io.read_text(new)
        except OSError:
            text = ""
        for result in find_results(old, text, records):
            name = result.path.name
            if not name.lower().startswith(old.stem.lower()):
                continue  # 名前の頭がテキストの名前でないもの (別の名前で保存し直したなど) は触らない
            target = result.path.with_name(new.stem + name[len(old.stem):])
            if _key(target) == _key(result.path):
                continue
            moves.append((result.path, target))

    # 入れ替え (a↔b) でぶつからないよう、いったん一時名にしてから付ける
    errors = []
    # 「生成結果」フォルダの結果に付いている説明の .txt (ImageMimicArt などが書いたもの) も、結果に合わせて付け替える
    caption_temps: dict[str, Path] = {}
    for index, (src, _dst) in enumerate(moves):
        caption = src.with_suffix(CAPTION_EXT)
        if src.parent.name.lower() != text_io.RESULTS_DIR.lower() or not caption.is_file():
            continue
        temp = caption.with_name(f"__pwc_rename_tmp_{index}__{caption.name}")
        try:
            caption.rename(temp)
            caption_temps[str(src)] = temp
        except OSError as exc:
            errors.append(f"{caption.name}: {exc}")
    temps = []
    for index, (src, _dst) in enumerate(moves):
        temp = src.with_name(f"__pwc_rename_tmp_{index}__{src.name}")
        try:
            src.rename(temp)
            temps.append(temp)
        except OSError as exc:
            temps.append(None)
            errors.append(f"{src.name}: {exc}")
    output_renames = {}
    for temp, (src, dst) in zip(temps, moves):
        if temp is None:
            continue
        final = dst
        i = 2
        while final.exists():
            final = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
            i += 1
        try:
            temp.rename(final)
            output_renames[str(src)] = str(final)
        except OSError as exc:
            errors.append(f"{src.name}: {exc}")
            try:
                temp.rename(src)
            except OSError:
                pass

    for src_text, temp in caption_temps.items():
        src = Path(src_text)
        final = Path(output_renames.get(src_text, src_text)).with_suffix(CAPTION_EXT)
        if final.exists():
            final = src.with_suffix(CAPTION_EXT)  # 行き先がふさがっていれば元の名前に戻す
        try:
            temp.rename(final)
        except OSError as exc:
            errors.append(f"{final.name}: {exc}")

    comfy.rename_in_history({**text_renames, **output_renames})
    for app_name in READER_APPS:
        comfy.rename_in_history(output_renames, app_name)  # 結果を元画像にした記録
    return len(output_renames), errors


# ----------------------------------------------------------------------
# 前の置き場所 (テキストと同じフォルダ) にある結果を「生成結果」フォルダへ移す
# ----------------------------------------------------------------------
READER_APPS = ("ImageMimicArt",)  # 結果の画像を元画像にして、記録に元のパスを残すアプリ
CAPTION_EXT = ".txt"


def old_place_results(folder: Path) -> list[Path]:
    """folder のテキストから生成して、まだ folder に置いてある結果 (名前順)。"""
    records = comfy.read_history()
    found: dict[str, Path] = {}
    for source in text_io.list_text_files(folder):
        try:
            text, _encoding = text_io.read_text(source)
        except OSError:
            text = ""
        for result in find_results(source, text, records):
            if _key(result.path.parent) == _key(folder):
                found[_key(result.path)] = result.path
    return sorted(found.values(), key=lambda p: p.name.lower())


def move_to_results_dir(folder: Path, paths: list[Path]) -> tuple[int, int, list[str]]:
    """結果のファイルを folder\生成結果 へ移す。同じ名前の説明の .txt (ImageMimicArt などが書いたもの) も一緒に。

    同じ名前の別の画像 (photo.png に対する photo.jpg) が folder に残るときは、その .txt は残す。
    記録 (このアプリと、結果を元画像にしたアプリ) のパスも付け替える。
    戻り値: (移した結果の数, 移した .txt の数, 失敗の説明)。
    """
    target_dir = folder / text_io.RESULTS_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return 0, 0, [f"{target_dir}: {exc}"]
    moving = {_key(p) for p in paths}
    renames: dict[str, str] = {}
    errors: list[str] = []
    captions = 0
    for path in paths:
        dest = target_dir / path.name
        i = 2
        while dest.exists():
            dest = target_dir / f"{path.stem} ({i}){path.suffix}"
            i += 1
        try:
            path.rename(dest)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        renames[str(path)] = str(dest)

        caption = path.with_suffix(CAPTION_EXT)
        if not caption.is_file():
            continue
        shared = [other for other in folder.glob(glob_escape(path.stem) + ".*")
                  if other.suffix.lower() in MEDIA_EXTS and _key(other) not in moving and other.is_file()]
        caption_dest = dest.with_suffix(CAPTION_EXT)
        if shared or caption_dest.exists():
            continue
        try:
            caption.rename(caption_dest)
            captions += 1
        except OSError as exc:
            errors.append(f"{caption.name}: {exc}")

    comfy.rename_in_history(renames)
    for app_name in READER_APPS:
        comfy.rename_in_history(renames, app_name)
    return len(renames), captions, errors


def glob_escape(text: str) -> str:
    """glob の特殊文字 ([ ] * ?) をそのままの文字として扱う。"""
    return "".join(f"[{c}]" if c in "[]*?" else c for c in text)


# ----------------------------------------------------------------------
# エクスプローラー・ごみ箱
# ----------------------------------------------------------------------
def show_in_explorer(path: Path) -> None:
    """そのファイルを選択した状態でフォルダを開く。"""
    subprocess.Popen(["explorer", "/select,", str(path)])


def can_recycle(path: Path) -> bool:
    """ごみ箱に入るか (ネットワーク上のファイルは入らずに消える)。"""
    text = str(path)
    if text.startswith("\\\\"):
        return False
    drive = os.path.splitdrive(os.path.abspath(text))[0] + "\\"
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive) != 4  # DRIVE_REMOTE
    except (AttributeError, OSError):
        return True


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def send_to_recycle_bin(paths: list[Path]) -> Optional[str]:
    """ごみ箱へ送る。失敗したときは説明を返す (うまくいけば None)。"""
    if not paths:
        return None
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = "".join(str(p) + "\0" for p in paths) + "\0"
    # FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
    op.fFlags = 0x0004 | 0x0010 | 0x0040 | 0x0400
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0:
        return f"削除できませんでした (コード 0x{result:X})"
    if op.fAnyOperationsAborted:
        return "削除が中断されました"
    return None


# ----------------------------------------------------------------------
# サムネール (エクスプローラーと同じ Windows のサムネールを使う。動画もこれで取れる)
# ----------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD), ("bmBits", ctypes.c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_IID_IShellItemImageFactory = "{bcc18b79-ba16-442f-80c4-8a59c30c463b}"
_GetImageProto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, _SIZE, ctypes.c_int,
                                    ctypes.POINTER(wintypes.HBITMAP))
_ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


def init_thread_com() -> None:
    """サムネールを作るスレッドで最初に 1 回呼ぶ。"""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    except (AttributeError, OSError):
        pass


def _shell_thumbnail(path: Path, size: int) -> Optional[QImage]:
    try:
        shell32 = ctypes.windll.shell32
        gdi32 = ctypes.windll.gdi32
    except AttributeError:
        return None
    factory = ctypes.c_void_p()
    iid = _guid(_IID_IShellItemImageFactory)
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    if shell32.SHCreateItemFromParsingName(str(path), None, ctypes.byref(iid), ctypes.byref(factory)) != 0:
        return None
    vtable = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    hbitmap = wintypes.HBITMAP()
    try:
        get_image = _GetImageProto(vtable[3])
        try:
            get_image(factory, _SIZE(size, size), 0, ctypes.byref(hbitmap))  # SIIGBF_RESIZETOFIT
        except OSError:
            return None
    finally:
        _ReleaseProto(vtable[2])(factory)
    if not hbitmap:
        return None
    try:
        bmp = _BITMAP()
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        if not gdi32.GetObjectW(hbitmap, ctypes.sizeof(bmp), ctypes.byref(bmp)):
            return None
        width, height = bmp.bmWidth, abs(bmp.bmHeight)
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(header)
        header.biWidth = width
        header.biHeight = -height  # 上から下へ
        header.biPlanes = 1
        header.biBitCount = 32
        buffer = ctypes.create_string_buffer(width * height * 4)
        dc = ctypes.windll.user32.GetDC(None)
        try:
            gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
            lines = gdi32.GetDIBits(dc, hbitmap, 0, height, buffer, ctypes.byref(header), 0)
        finally:
            ctypes.windll.user32.ReleaseDC(None, dc)
        if lines != height:
            return None
        raw = buffer.raw
        # アルファがすべて 0 なら不透明の画像として扱う (動画のサムネールなど)
        has_alpha = any(raw[i] for i in range(3, len(raw), 4 * 97))
        fmt = QImage.Format.Format_ARGB32_Premultiplied if has_alpha else QImage.Format.Format_RGB32
        return QImage(raw, width, height, width * 4, fmt).copy()
    finally:
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteObject(hbitmap)


def make_thumbnail(path: Path, size: int) -> Optional[QImage]:
    """size × size に収まるサムネール。画像は自分で読み、読めないもの・動画は Windows に頼む。"""
    if not _is_video(path):
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        original = reader.size()
        if original.isValid() and original.width() > 0 and original.height() > 0:
            reader.setScaledSize(original.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if not image.isNull():
            return image
    image = _shell_thumbnail(path, size)
    if image is not None and (image.width() > size or image.height() > size):
        image = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return image


def load_preview(path: Path, width: int, height: int) -> tuple[Optional[QImage], Optional[QSize]]:
    """width × height に収まる大きさで読む (元より大きくはしない)。元の大きさも返す (動画は None)。"""
    if not _is_video(path):
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        original = reader.size()
        if original.isValid() and original.width() > 0 and original.height() > 0:
            if original.width() > width or original.height() > height:
                reader.setScaledSize(original.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if not image.isNull():
                return image, original
    return make_thumbnail(path, min(width, height)), None
