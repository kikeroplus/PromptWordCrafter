"""PrismLab に送る (PrismLab の docs\\spec.md 5 章)。

選んだテキストを、PrismLab の量産のレシピの入口か、物語の選んでいるシーンの元のプロンプトに入れてもらう。
PrismLab の 2 重起動を止めるパイプ (PrismLab-(ユーザー名)) に JSON を書いて閉じる。
動いていなければ、JSON を一時フォルダに書いて「PrismLab.exe --send (JSON のパス)」で起動する。
"""

import getpass
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtNetwork import QLocalSocket

APP_NAME = "PrismLab"
SENDER = "PromptWordCrafter"
SERVER_NAME = f"PrismLab-{getpass.getuser()}"
SEND_ARG = "--send"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home())


def recipe_names() -> list[str]:
    """PrismLab の量産のレシピの名前 (並び順。読めなければ空)。PrismLab が動いていなくても読める。"""
    path = _appdata() / APP_NAME / "recipes.json"
    try:
        items = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [str(d.get("name") or "") if isinstance(d, dict) else "" for d in items]


def find_exe() -> Optional[list[str]]:
    """PrismLab を起動するコマンド (デスクトップの Application、なければ隣の PrismLab の dist、開発中は main.py)。"""
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    candidates = [Path.home() / "Desktop" / "Application" / "PrismLab.exe"]
    for parent in here.parents:
        candidates.append(parent / "PrismLab" / "dist" / "PrismLab.exe")
    exe = next((p for p in candidates if p.is_file()), None)
    if exe is not None:
        return [str(exe)]
    if not getattr(sys, "frozen", False):
        for parent in here.parents:
            script = parent / "PrismLab" / "main.py"
            if script.is_file():
                return [sys.executable, str(script)]
    return None


def send(message: dict) -> str:
    """送る。うまくいけば空、だめなら理由。"""
    message = {"cmd": "send", "from": SENDER, **message}
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if sys.platform == "win32":
        # PrismLab が前に出るのを Windows に拒まれないよう、こちらから許可しておく
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(700):
        # 送るものは 1 行 (改行で終わる)。PrismLab が読み終えて切るまで待つ (先に切ると、読まれずに落ちることがある)
        sock.write(data + b"\n")
        sock.flush()
        sock.waitForBytesWritten(1000)
        if not sock.waitForDisconnected(3000):
            sock.abort()
            return "PrismLab が受け取りませんでした (応答がありません)。"
        return ""
    command = find_exe()
    if command is None:
        return "PrismLab が見つかりません (デスクトップの Application に PrismLab.exe を置いてください)。"
    try:
        fd, temp = tempfile.mkstemp(prefix="prismlab_send_", suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        subprocess.Popen(command + [SEND_ARG, temp])  # noqa: S603 - 決まった PrismLab に送るものを渡す
    except OSError as exc:
        return f"PrismLab を起動できません: {exc}"
    return ""


def send_to_recipe(files: list[Path], name: str, index: int) -> str:
    return send({"target": "recipe", "recipe": name, "index": index, "files": [str(p) for p in files]})


def send_to_story_original(file: Path) -> str:
    return send({"target": "story", "slot": "original", "files": [str(file)]})
