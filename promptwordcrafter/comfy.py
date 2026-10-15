"""ComfyUI で画像・動画を生成する (テキストファイルの本文をプロンプトにする t2i / t2v)。

テキストファイル 1 つが 1 本のプロンプト。本文をそのままワークフローのプロンプトの場所に入れ、
ベースプロンプトを改行をはさんで後ろに付ける。

J-Rate の ComfyVideo.cs と同じ決まりごと (generation-standard.md) で、
設定・記録・ワークフローの書き換え・ComfyUI とのやりとりを受け持つ。
設定は %APPDATA%\\PromptWordCrafter\\comfy_*.txt に置く (形式は J-Rate と同じ)。
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "PromptWordCrafter"
DEFAULT_URL = "http://127.0.0.1:8188"
PRESET_COUNT = 10
WORKFLOW_COUNT = 100
PROMPT_COUNT = 100

# テストで差し替えられるよう、モジュール変数にしておく
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME

VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mov", ".mkv", ".gif", ".webp")
IMAGE_EXTS = (".png", ".jpg", ".jpeg")

PARAMS_HEADER = f"[{APP_NAME} 生成パラメーター]"
# J-Rate でコピーしたパラメーターも貼り付けられるようにする
PARAMS_HEADERS = (PARAMS_HEADER, "[J-Rate 生成パラメーター]")

EXPORT_KIND = "comfy-settings"

_rng = random.Random()
_history_lock = threading.Lock()


class ComfyError(Exception):
    """利用者に見せるエラー (文は日本語)。"""


class ComfyConnectionError(ComfyError):
    """ComfyUI に接続できない。"""


class GenerationCancelled(Exception):
    pass


# ----------------------------------------------------------------------
# プリセット・セット・1 回の生成の設定
# ----------------------------------------------------------------------
@dataclass
class Preset:
    """名前と値。値は、接続先なら URL、ワークフローなら JSON のパス。"""

    name: str = ""
    value: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.value

    def label(self, index: int) -> str:
        return self._label(index, self.value)

    def file_label(self, index: int) -> str:
        """ワークフロー用: パスの代わりにファイル名を出す。"""
        return self._label(index, file_name(self.value))

    def clear(self):
        self.name = ""
        self.value = ""

    def _label(self, index: int, detail: str) -> str:
        if self.name:
            text = f"{self.name}  ({detail})" if detail else self.name
        else:
            text = detail or "(未登録)"
        return f"{index + 1}: {text}"


@dataclass
class ComfySet:
    """接続先・ワークフロー・長さをまとめて呼び出すセット。中身は値で覚える。

    プロンプトはテキストファイルの本文を使うので、このアプリでは prompt を空で保存する
    (J-Rate と同じ形式のファイルなので、項目は残しておく)。
    """

    name: str = ""
    url: str = ""
    workflow: str = ""
    prompt: str = ""
    seconds: str = ""
    prompt_preset: int = -1  # 保存したときに選んでいたプロンプトのプリセット (0 始まり。なければ -1)

    @property
    def is_empty(self) -> bool:
        return not self.url and not self.workflow

    def label(self, index: int) -> str:
        return f"{index + 1}: " + ("(未登録)" if self.is_empty else self.name or "(名前なし)")


@dataclass
class GenRequest:
    connection_name: str = ""
    url: str = ""
    workflow_name: str = ""
    workflow_path: str = ""
    prompt: Optional[str] = None  # None = ワークフローに書かれているまま
    seconds: Optional[float] = None  # 同上
    base_prompt: str = ""  # 生成を始めたときのベースプロンプト (prompt のあとに改行をはさんで送る)

    @property
    def full_prompt(self) -> Optional[str]:
        if self.prompt is None:
            return None
        p = self.prompt.strip()
        b = (self.base_prompt or "").strip()
        if not p:
            return b
        return p if not b else p + "\n" + b

    def copy(self, **changes) -> "GenRequest":
        data = dict(self.__dict__)
        data.update(changes)
        return GenRequest(**data)


@dataclass
class GenRecord:
    output: str = ""
    source: str = ""
    time: str = ""
    request: GenRequest = field(default_factory=GenRequest)


def file_name(path: str) -> str:
    """他の PC のパスでも区切りが \\ と / のどちらでもファイル名を取り出す。"""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def format_seconds(seconds: float) -> str:
    return str(int(seconds)) if seconds == int(seconds) else repr(seconds)


# ----------------------------------------------------------------------
# パラメーターのコピー / 貼り付け (人が読める文章)
# ----------------------------------------------------------------------
def params_to_text(r: GenRequest) -> str:
    lines = [
        PARAMS_HEADER,
        f"接続先: {r.connection_name}",
        f"URL: {r.url}",
        f"ワークフロー: {r.workflow_name}",
        f"ワークフローのパス: {r.workflow_path}",
    ]
    if r.seconds is not None:
        lines.append(f"長さ: {format_seconds(r.seconds)}")
    text = "\r\n".join(lines) + "\r\n"
    if r.prompt is not None:
        text += "プロンプト:\r\n" + r.prompt.strip()
    return text


def parse_params(text: str) -> Optional[GenRequest]:
    """目印のない文字列なら None。"""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() not in PARAMS_HEADERS:
        return None
    r = GenRequest()
    for j in range(i + 1, len(lines)):
        line = lines[j]
        if line.strip() == "プロンプト:":
            r.prompt = "\r\n".join(lines[j + 1:]).strip()
            break
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key == "接続先":
            r.connection_name = value
        elif key == "URL":
            r.url = value
        elif key == "ワークフロー":
            r.workflow_name = value
        elif key == "ワークフローのパス":
            r.workflow_path = value
        elif key == "長さ":
            try:
                r.seconds = float(value)
            except ValueError:
                pass
    return r


# ----------------------------------------------------------------------
# 設定ファイル (%APPDATA%\PromptWordCrafter\comfy_*.txt)
# ----------------------------------------------------------------------
def file_path(name: str) -> Path:
    return DATA_DIR / name


def load(name: str) -> Optional[str]:
    try:
        path = file_path(name)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return None


def save(name: str, value: str) -> None:
    try:
        path = file_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8", newline="")
    except OSError:
        pass


def _one_line(text: str) -> str:
    return (text or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _load_presets(name: str, legacy: str, legacy_default: str, count: int) -> list[Preset]:
    presets = [Preset() for _ in range(count)]
    text = load(name)
    if text is None:
        old = load(legacy)
        presets[0].value = old.strip().rstrip("/") if old and old.strip() else legacy_default
        return presets
    for line in text.split("\n"):
        parts = line.rstrip("\r").split("\t")
        if len(parts) < 3:
            continue
        try:
            n = int(parts[0])
        except ValueError:
            continue
        if 1 <= n <= count:
            presets[n - 1].name = parts[1].strip()
            presets[n - 1].value = parts[2].strip().rstrip("/")
    return presets


def _save_presets(name: str, presets: list[Preset], count: int) -> None:
    lines = [
        f"{i + 1}\t{_one_line(p.name)}\t{_one_line(p.value).rstrip('/')}\r\n"
        for i, p in enumerate(presets[:count])
    ]
    save(name, "".join(lines))


def _load_index(name: str, count: int) -> int:
    try:
        n = int(load(name) or "")
    except ValueError:
        return 0
    return n - 1 if 1 <= n <= count else 0


def _load_optional_index(name: str, count: int) -> int:
    try:
        n = int(load(name) or "")
    except ValueError:
        return -1
    return n - 1 if 1 <= n <= count else -1


def connections() -> list[Preset]:
    return _load_presets("comfy_presets.txt", "comfy_url.txt", DEFAULT_URL, PRESET_COUNT)


def set_connections(presets: list[Preset]) -> None:
    _save_presets("comfy_presets.txt", presets, PRESET_COUNT)


def active_connection() -> int:
    return _load_index("comfy_preset.txt", PRESET_COUNT)


def set_active_connection(index: int) -> None:
    save("comfy_preset.txt", str(index + 1))


def workflows() -> list[Preset]:
    return _load_presets("comfy_workflows.txt", "comfy_workflow.txt", "", WORKFLOW_COUNT)


def set_workflows(presets: list[Preset]) -> None:
    _save_presets("comfy_workflows.txt", presets, WORKFLOW_COUNT)


def active_workflow() -> int:
    return _load_index("comfy_workflow_preset.txt", WORKFLOW_COUNT)


def set_active_workflow(index: int) -> None:
    save("comfy_workflow_preset.txt", str(index + 1))


def active_workflow_path() -> str:
    return workflows()[active_workflow()].value


def base_prompt_video() -> str:
    return load("comfy_base_prompt_video.txt") or load("comfy_base_prompt.txt") or ""


def set_base_prompt_video(text: str) -> None:
    save("comfy_base_prompt_video.txt", (text or "").strip())


def base_prompt_image() -> str:
    return load("comfy_base_prompt_image.txt") or load("comfy_base_prompt.txt") or ""


def set_base_prompt_image(text: str) -> None:
    save("comfy_base_prompt_image.txt", (text or "").strip())


def base_prompt_for(video: bool) -> str:
    return base_prompt_video() if video else base_prompt_image()


def last_seconds() -> str:
    return load("comfy_seconds.txt") or ""


def set_last_seconds(text: str) -> None:
    save("comfy_seconds.txt", (text or "").strip())


def open_result() -> bool:
    """生成が終わったら、出来たファイルを開くか (既定はオン。まとめて生成のときは開かない)。"""
    return load("comfy_jump.txt") != "0"


def set_open_result(value: bool) -> None:
    save("comfy_jump.txt", "1" if value else "0")


def sets() -> list[ComfySet]:
    result = [ComfySet() for _ in range(PRESET_COUNT)]
    try:
        data = json.loads(load("comfy_sets.txt") or "[]")
    except ValueError:
        data = []
    if not isinstance(data, list):
        data = []
    for i, item in enumerate(data[:PRESET_COUNT]):
        if not isinstance(item, dict):
            continue
        s = result[i]
        s.name = _str(item, "name")
        s.url = _str(item, "url")
        s.workflow = _str(item, "workflow")
        s.prompt = _str(item, "prompt")
        s.seconds = _str(item, "seconds")
        try:
            pp = int(_str(item, "promptPreset"))
        except ValueError:
            pp = 0
        s.prompt_preset = pp - 1 if 1 <= pp <= PROMPT_COUNT else -1
    return result


def set_sets(items: list[ComfySet]) -> None:
    data = [
        {
            "name": s.name,
            "url": s.url,
            "workflow": s.workflow,
            "prompt": s.prompt,
            "seconds": s.seconds,
            "promptPreset": s.prompt_preset + 1,
        }
        for s in items
    ]
    save("comfy_sets.txt", json.dumps(data, ensure_ascii=False))


def last_set() -> int:
    return _load_optional_index("comfy_set.txt", PRESET_COUNT)


def set_last_set(index: int) -> None:
    save("comfy_set.txt", str(index + 1))


def _str(data: dict, key: str) -> str:
    value = data.get(key)
    return "" if value is None else str(value)


# ----------------------------------------------------------------------
# エクスポート / インポート (J-Rate と同じ形式なので、互いに読み込める)
# ----------------------------------------------------------------------
def import_backup_path() -> Path:
    return file_path("backup_before_import.json")


def _is_setting_file(name: str) -> bool:
    if not name.startswith("comfy_") or not name.endswith(".txt") or name == "comfy_history.txt":
        return False
    return all(c in "abcdefghijklmnopqrstuvwxyz0123456789_." for c in name)


def _setting_files() -> list[Path]:
    if not DATA_DIR.is_dir():
        return []
    return [p for p in DATA_DIR.glob("comfy_*.txt") if _is_setting_file(p.name)]


def export_settings(path: str) -> str:
    files = {p.name: p.read_text(encoding="utf-8-sig") for p in _setting_files()}
    paths = [p.value for p in workflows()] + [s.workflow for s in sets()]
    workflow_items = []
    seen = set()
    missing = 0
    for wf in paths:
        key = wf.lower()
        if not wf or key in seen:
            continue
        seen.add(key)
        try:
            content = Path(wf).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            missing += 1
            continue
        workflow_items.append({"path": wf, "content": content})

    root = {
        "app": APP_NAME,
        "kind": EXPORT_KIND,
        "version": 1,
        "exported": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": files,
        "workflows": workflow_items,
    }
    Path(path).write_text(json.dumps(root, ensure_ascii=False), encoding="utf-8")
    note = f"。{missing} 個は読めなかったので含めていません" if missing else ""
    return f"生成の設定をエクスポートしました (ワークフローの JSON {len(workflow_items)} 個を含む{note}): {path}"


def import_settings(path: str) -> str:
    """今の設定をすべて置き換える。読めないファイルのときは ComfyError (設定は変えない)。"""
    try:
        root = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ComfyError(f"ファイルを読めません。\n{exc}") from exc
    files = root.get("files") if isinstance(root, dict) else None
    if not isinstance(files, dict) or _str(root, "kind") != EXPORT_KIND:
        raise ComfyError("生成の設定をエクスポートしたファイルではありません。")

    try:
        export_settings(str(import_backup_path()))
    except OSError:
        pass

    for p in _setting_files():
        p.unlink()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        if _is_setting_file(name) and content is not None:
            file_path(name).write_text(str(content), encoding="utf-8", newline="")

    # この PC にないワークフローは書き出して、登録してあるパスを付け替える
    moved: dict[str, str] = {}
    for item in root.get("workflows") or []:
        if not isinstance(item, dict):
            continue
        old = _str(item, "path")
        if not old or old.lower() in moved:
            continue
        try:
            exists = Path(old).is_file()
        except OSError:
            exists = False
        if not exists:
            moved[old.lower()] = _restore_workflow(old, _str(item, "content"))
    if moved:
        presets = workflows()
        for p in presets:
            p.value = moved.get(p.value.lower(), p.value)
        set_workflows(presets)
        items = sets()
        if any(s.workflow.lower() in moved for s in items):
            for s in items:
                s.workflow = moved.get(s.workflow.lower(), s.workflow)
            set_sets(items)

    message = "生成の設定をインポートしました。"
    if moved:
        message += (
            f"\n\nこの PC になかったワークフローの JSON {len(moved)} 個を次のフォルダに書き出しました。\n"
            f"{file_path('workflows')}"
        )
    return message


def _restore_workflow(old_path: str, content: str) -> str:
    name = file_name(old_path)
    if not name or any(c in name for c in '\\/:*?"<>|'):
        name = "workflow.json"
    folder = file_path("workflows")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 2
    while target.exists():
        try:
            if target.read_text(encoding="utf-8-sig") == content:
                return str(target)
        except (OSError, UnicodeDecodeError):
            pass
        target = folder / f"{stem} ({i}){suffix}"
        i += 1
    target.write_text(content, encoding="utf-8")
    return str(target)


# ----------------------------------------------------------------------
# 生成の記録 (%APPDATA%\PromptWordCrafter\comfy_history.txt、1 行に 1 件の JSON)
# ----------------------------------------------------------------------
def add_history(output: str, source: str, req: GenRequest) -> None:
    record = {
        "output": output,
        "source": source,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "connection": req.connection_name,
        "url": req.url,
        "workflow": req.workflow_name,
        "workflowPath": req.workflow_path,
        "prompt": req.prompt,
        "basePrompt": req.base_prompt,
        "seconds": req.seconds,
    }
    try:
        with _history_lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with file_path("comfy_history.txt").open("a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\r\n")
    except OSError:
        pass


def find_history_by_source(source: str) -> Optional[GenRecord]:
    """そのテキストファイルから生成したときの記録 (新しいものを優先)。

    同じパスの記録を探し、なければ同じファイル名の記録 (あとで別のフォルダへ移したもの) を使う。
    """
    try:
        with _history_lock:
            path = file_path("comfy_history.txt")
            if not path.is_file():
                return None
            lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    name = Path(source).name.lower()
    target = os.path.normcase(os.path.abspath(source))
    by_name = None
    for line in reversed(lines):
        if name not in line.lower():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        recorded = _str(data, "source")
        if recorded and os.path.normcase(os.path.abspath(recorded)) == target:
            return _to_record(data)
        if by_name is None and file_name(recorded).lower() == name:
            by_name = data
    return _to_record(by_name) if by_name else None


_history_cache: tuple = (None, [])  # ((パス, 更新日時, サイズ), 記録)


def read_history() -> list[GenRecord]:
    """すべての記録 (古い順)。

    テキストを選ぶたびに呼ばれるので、ファイルが変わっていなければ前に読んだものを返す。
    返した記録は書き換えないこと (読むだけの呼び出し元で共有している)。
    """
    global _history_cache
    try:
        with _history_lock:
            path = file_path("comfy_history.txt")
            try:
                stat = path.stat()
            except FileNotFoundError:
                return []
            stamp = (str(path), stat.st_mtime_ns, stat.st_size)
            if _history_cache[0] == stamp:
                return list(_history_cache[1])
            lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    records = []
    for line in lines:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            records.append(_to_record(data))
    _history_cache = (stamp, records)
    return list(records)


def rename_in_history(renames: dict[str, str]) -> int:
    """記録の元のファイルと出来たファイルのパスを付け替える (名前を変えたあとに呼ぶ)。

    renames は {旧パス: 新パス}。大文字小文字は区別しない。戻り値は書き換えた行の数。
    """
    if not renames:
        return 0
    table = {os.path.normcase(os.path.abspath(old)): new for old, new in renames.items()}

    def mapped(value: str) -> Optional[str]:
        if not value:
            return None
        return table.get(os.path.normcase(os.path.abspath(value)))

    changed = 0
    try:
        with _history_lock:
            path = file_path("comfy_history.txt")
            if not path.is_file():
                return 0
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            out = []
            for line in lines:
                try:
                    data = json.loads(line)
                except ValueError:
                    out.append(line)
                    continue
                if isinstance(data, dict):
                    hit = False
                    for key in ("source", "output"):
                        new = mapped(_str(data, key))
                        if new is not None:
                            data[key] = new
                            hit = True
                    if hit:
                        changed += 1
                        line = json.dumps(data, ensure_ascii=False)
                out.append(line)
            if changed:
                temp = path.with_name(path.name + ".tmp")
                temp.write_text("".join(x + "\r\n" for x in out), encoding="utf-8", newline="")
                temp.replace(path)
    except (OSError, UnicodeDecodeError):
        return 0
    return changed


def _to_record(data: dict) -> GenRecord:
    req = GenRequest(
        connection_name=_str(data, "connection"),
        url=_str(data, "url"),
        workflow_name=_str(data, "workflow"),
        workflow_path=_str(data, "workflowPath"),
        prompt=None if data.get("prompt") is None else str(data["prompt"]),
        base_prompt=_str(data, "basePrompt"),
    )
    if data.get("seconds") is not None:
        try:
            req.seconds = float(data["seconds"])
        except (TypeError, ValueError):
            pass
    return GenRecord(_str(data, "output"), _str(data, "source"), _str(data, "time"), req)


# ----------------------------------------------------------------------
# ワークフロー (ComfyUI の「Export (API)」で保存した JSON)
# ----------------------------------------------------------------------
def load_workflow(path: str) -> dict:
    if not path or not Path(path).is_file():
        raise ComfyError(f"ワークフローのファイルが見つかりません。\n{path}")
    try:
        wf = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ComfyError(f"ワークフローの JSON を読めません。\n{exc}") from exc
    if (
        not isinstance(wf, dict)
        or "nodes" in wf
        or not any(isinstance(v, dict) and "class_type" in v for v in wf.values())
    ):
        raise ComfyError(
            "このファイルは API 形式のワークフローではありません。\n"
            "ComfyUI のメニュー「Export (API)」(Save (API Format)) で保存した JSON を指定してください。\n"
            f"{path}"
        )
    return wf


def _nodes(wf: dict):
    for node in wf.values():
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
            yield node


def _class_type(node: dict) -> str:
    value = node.get("class_type")
    return value if isinstance(value, str) else ""


def _title(node: dict) -> str:
    meta = node.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    return title if isinstance(title, str) else ""


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def count_nodes(wf: dict, class_type: str) -> int:
    return sum(1 for node in _nodes(wf) if _class_type(node) == class_type)


def is_video_workflow(wf: dict) -> bool:
    """名前に video を含むノードか、動く画像を保存するノードがあれば動画用。"""
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        ct = _class_type(node)
        if "video" in ct.lower() or ct in ("SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG"):
            return True
    return False


def _prompt_slots(wf: dict) -> list[tuple[dict, str]]:
    """文字列が直接入っている prompt 入力があればそこ (すべて)。
    なければ CLIPTextEncode の text: 1 つならそれ、複数ならタイトルに Positive とあるもの。"""
    slots = []
    clip = []
    for node in _nodes(wf):
        inputs = node["inputs"]
        if isinstance(inputs.get("prompt"), str):
            slots.append((inputs, "prompt"))
        elif _class_type(node) == "CLIPTextEncode" and isinstance(inputs.get("text"), str):
            clip.append(node)
    if slots:
        return slots
    pick = clip if len(clip) == 1 else [n for n in clip if "positive" in _title(n).lower()]
    return [(n["inputs"], "text") for n in pick]


def has_prompt(wf: dict) -> bool:
    return bool(_prompt_slots(wf))


def prompt_slot_problem(wf: dict) -> Optional[str]:
    """テキストファイルの本文を入れる場所がなければ、その理由 (あれば None)。"""
    if _prompt_slots(wf):
        return None
    clip = sum(1 for node in _nodes(wf)
               if _class_type(node) == "CLIPTextEncode" and isinstance(node["inputs"].get("text"), str))
    if clip > 1:
        return (
            "このワークフローには「CLIP Text Encode」が複数あり、どれにテキストファイルの本文を入れるか決められません。\n"
            "ComfyUI で、本文を入れるノードのタイトルに「Positive」を含めてから、もう一度「Export (API)」で保存してください。"
        )
    return (
        "このワークフローには、テキストファイルの本文 (プロンプト) を入れる場所がありません。\n"
        "文字列が直接入っている prompt 入力か、「CLIP Text Encode」のあるワークフローを指定してください。"
    )


def _duration_slot(wf: dict) -> tuple[Optional[dict], str, Optional[dict]]:
    """長さを入れる場所 (inputs, キー, ノード)。
    タイトルに duration とある Float / Int ノードの value。なければ、数値が直接入っている duration 入力。"""
    direct = None
    for node in _nodes(wf):
        inputs = node["inputs"]
        if (
            _class_type(node) in ("PrimitiveFloat", "PrimitiveInt")
            and "duration" in _title(node).lower()
            and _is_number(inputs.get("value"))
        ):
            return inputs, "value", node
        if direct is None and _is_number(inputs.get("duration")):
            direct = (inputs, "duration", node)
    return direct if direct else (None, "duration", None)


def get_duration(wf: dict) -> Optional[float]:
    inputs, key, _node = _duration_slot(wf)
    return None if inputs is None else float(inputs[key])


def duration_is_integer(wf: dict) -> bool:
    inputs, key, node = _duration_slot(wf)
    return inputs is not None and key == "value" and _class_type(node) == "PrimitiveInt"


def _set_duration(wf: dict, seconds: float) -> None:
    inputs, key, _node = _duration_slot(wf)
    if inputs is not None:
        # 端数がなければ整数で入れる (整数の入力でも小数の入力でも通る)
        inputs[key] = int(seconds) if seconds == int(seconds) else seconds


def _randomize_seeds(wf: dict) -> None:
    for node in _nodes(wf):
        inputs = node["inputs"]
        for key in ("seed", "noise_seed"):
            if _is_number(inputs.get(key)):
                inputs[key] = _rng.randrange(0, 10**15)


def describe_workflow(wf: dict) -> str:
    """設定画面に出す確認結果。"""
    kind = "動画用" if is_video_workflow(wf) else "画像用"
    slots = len(_prompt_slots(wf))
    prompt = f"本文を入れる場所 {slots} か所" if slots else "本文を入れる場所がありません"
    duration = "長さを変えられます" if get_duration(wf) is not None else "長さは変えられません"
    text = f"ワークフローを確認しました ({kind}、ノード {len(wf)} 個、{prompt}、{duration})"
    if count_nodes(wf, "LoadImage"):
        text += "\n※「Load Image」ノードがあります。画像はワークフローに書かれているものがそのまま使われます。"
    return text


# ----------------------------------------------------------------------
# ComfyUI の HTTP API
#   1. ワークフローにプロンプト・長さを入れ、シードをランダムにする
#   2. POST /prompt       で実行を依頼
#   3. GET  /history/{id} で完了を待つ (待ち中は GET /queue で順番を表示)
#   4. GET  /view         で出来たファイルを受け取り、テキストファイルと同じフォルダに保存
# ----------------------------------------------------------------------
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # ローカル / LAN にプロキシを通さない


def _request(url: str, method: str = "GET", body: Optional[bytes] = None, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=timeout) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        # 400 などは本文に理由 (node_errors など) が入っているので、それを見せる
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except OSError:
            text = ""
        raise ComfyError(f"ComfyUI がエラーを返しました ({exc.code})。\n{_shorten(text)}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ComfyConnectionError(str(reason)) from exc


def _json(url: str, method: str = "GET", body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    raw = _request(url, method, data)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def test_connection(base_url: str) -> Optional[str]:
    """ComfyUI が動いているか確かめる。戻り値はエラーメッセージ (正常なら None)。"""
    try:
        _request((base_url or "").rstrip("/") + "/system_stats")
        return None
    except ComfyError as exc:
        return str(exc)


def generate(
    source: str,
    req: GenRequest,
    progress: Callable[[str], None],
    cancel: threading.Event,
) -> list[str]:
    """生成して、保存したファイルのパスを返す (動画なら 1 つ、画像なら出力された枚数ぶん)。

    失敗したときは ComfyError、cancel で止められたときは ComfyUI 側も取り消して GenerationCancelled。
    """
    base_url = (req.url or DEFAULT_URL).rstrip("/")
    wf = load_workflow(req.workflow_path)
    _randomize_seeds(wf)
    prompt = req.full_prompt
    if prompt is not None:
        for inputs, key in _prompt_slots(wf):
            inputs[key] = prompt
    if req.seconds is not None:
        _set_duration(wf, req.seconds)

    if cancel.is_set():
        raise GenerationCancelled()
    progress("ComfyUI に実行を依頼中...")
    res = _json(
        base_url + "/prompt",
        "POST",
        {"prompt": wf, "client_id": "promptwordcrafter-" + uuid.uuid4().hex},
    )
    prompt_id = res.get("prompt_id") if isinstance(res, dict) else None
    if not prompt_id:
        raise ComfyError(f"ComfyUI が実行を受け付けませんでした。\n{_shorten(str(res))}")
    prompt_id = str(prompt_id)

    started = time.monotonic()
    failures = 0
    while True:
        if cancel.wait(2):
            _cancel_prompt(base_url, prompt_id)
            raise GenerationCancelled()
        try:
            history = _json(base_url + "/history/" + urllib.parse.quote(prompt_id, safe=""))
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                break
            progress(f"{_queue_state(base_url, prompt_id)} {_elapsed(started)}")
            failures = 0
        except ComfyConnectionError:
            # ComfyUI が落ちた・再起動中など。1 分ほど続いたらあきらめる
            failures += 1
            if failures >= 30:
                raise ComfyError(f"ComfyUI との接続が切れました。({base_url})")
            progress(f"ComfyUI の応答待ち... {_elapsed(started)}")

    _check_status(entry)
    outputs = _find_outputs(entry)
    if not outputs:
        raise ComfyError(
            "ワークフローの出力に動画や画像のファイルが見つかりません。\n"
            "Save Video / Save Image などの保存ノードがあるか確認してください。"
        )

    # 動画は「(元の名前)_video」、画像は「(元の名前)_t2i」で保存する
    src = Path(source)
    saved = []
    for output in outputs:
        if cancel.is_set():
            raise GenerationCancelled()
        if len(outputs) > 1:
            progress(f"結果を受け取り中... ({len(saved) + 1} / {len(outputs)})")
        else:
            progress("結果を受け取り中...")
        suffix = "_video" if output["video"] else "_t2i"
        dest = _unique_path(src.with_name(src.stem + suffix + Path(output["filename"]).suffix))
        query = urllib.parse.urlencode(
            {"filename": output["filename"], "subfolder": output["subfolder"], "type": output["type"]}
        )
        data = _request(base_url + "/view?" + query, timeout=120)
        temp = dest.with_name(dest.name + ".part")
        try:
            temp.write_bytes(data)
            temp.replace(dest)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
        saved.append(str(dest))
    return saved


def _cancel_prompt(base_url: str, prompt_id: str) -> None:
    """待ち行列から外し、ちょうど自分の番で動いているときだけ中断を送る。"""
    try:
        _json(base_url + "/queue", "POST", {"delete": [prompt_id]})
    except ComfyError:
        pass
    try:
        queue = _json(base_url + "/queue")
        if isinstance(queue, dict) and _contains_prompt(queue.get("queue_running"), prompt_id):
            _json(base_url + "/interrupt", "POST", {"prompt_id": prompt_id})
    except ComfyError:
        pass


def _contains_prompt(items, prompt_id: str) -> bool:
    return isinstance(items, list) and any(
        isinstance(x, list) and len(x) >= 2 and str(x[1]) == prompt_id for x in items
    )


def _queue_state(base_url: str, prompt_id: str) -> str:
    try:
        queue = _json(base_url + "/queue")
        if isinstance(queue, dict):
            if _contains_prompt(queue.get("queue_running"), prompt_id):
                return "生成中"
            pending = [x for x in queue.get("queue_pending") or [] if isinstance(x, list) and len(x) >= 2]
            pending.sort(key=lambda x: x[0] if _is_number(x[0]) else 0)
            for i, item in enumerate(pending):
                if str(item[1]) == prompt_id:
                    return f"順番待ち ({i + 1} 番目)"
    except ComfyError:
        pass
    return "生成中"


def _check_status(entry: dict) -> None:
    status = entry.get("status")
    if not isinstance(status, dict) or status.get("status_str") != "error":
        return
    detail = ""
    for message in status.get("messages") or []:
        if not isinstance(message, list) or len(message) < 2 or message[0] != "execution_error":
            continue
        data = message[1] if isinstance(message[1], dict) else {}
        node_type = data.get("node_type")
        text = str(data.get("exception_message") or "").strip()
        detail = (f"[{node_type}] " if node_type else "") + text
    raise ComfyError(f"ComfyUI での生成中にエラーが起きました。\n{_shorten(detail)}")


def _find_outputs(entry: dict) -> list[dict]:
    """動画があれば 1 つだけ (mp4 を優先)。なければ画像をすべて (保存用があればそれだけ)。"""
    found = []
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        return found
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for value in node_out.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                name = str(item["filename"])
                ext = Path(name).suffix.lower()
                video = ext in VIDEO_EXTS
                if not video and ext not in IMAGE_EXTS:
                    continue
                found.append(
                    {
                        "filename": name,
                        "subfolder": str(item.get("subfolder") or ""),
                        "type": str(item.get("type") or "output"),
                        "video": video,
                    }
                )
    videos = [f for f in found if f["video"]]
    if videos:
        videos.sort(
            key=lambda f: (f["type"] != "output", VIDEO_EXTS.index(Path(f["filename"]).suffix.lower()))
        )
        return [videos[0]]
    kept = [f for f in found if f["type"] == "output"]
    return kept or found


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _elapsed(started: float) -> str:
    seconds = int(time.monotonic() - started)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _shorten(text: str) -> str:
    text = (text or "").strip()
    return text[:800] + "..." if len(text) > 800 else text
