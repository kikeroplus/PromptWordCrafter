import getpass
import json
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from . import profile
from .icon import make_icon
from .main_window import MainWindow

# 2 つ目を起動したら、先に動いているほうを前に出して自分は終わる。
# 2 つ目を閉じたときに 1 つ目のジョブリストが失われないようにするため。
_SERVER_NAME = f"PromptWordCrafter-{getpass.getuser()}"

# ほかのアプリ (MuseFactory) から「フォルダを開いて、ファイルを選ぶ」と頼まれる。
# 動いているときはパイプに 1 行の JSON {"cmd": "open", "from": ..., "folder": ..., "select": [...]} が来る。
# 動いていないときは「PromptWordCrafter.exe --open (JSON のパス)」で起動される。
_OPEN_ARG = "--open"


def _read_open_arg() -> Optional[bytes]:
    """--open で渡された JSON を読む (読んだら一時ファイルは消す)。"""
    if _OPEN_ARG not in sys.argv:
        return None
    index = sys.argv.index(_OPEN_ARG)
    if index + 1 >= len(sys.argv):
        return None
    path = Path(sys.argv[index + 1])
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return data


def _notify_running_instance(payload: bytes = b"show") -> bool:
    socket = QLocalSocket()
    socket.connectToServer(_SERVER_NAME)
    if not socket.waitForConnected(500):
        return False
    if sys.platform == "win32":
        # 前面に出るのを Windows に拒まれないよう、こちらから許可しておく
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    socket.write(payload if payload.endswith(b"\n") else payload + b"\n")
    socket.flush()
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def _handle_message(window: MainWindow, data: bytes) -> None:
    text = data.decode("utf-8", "replace").strip()
    if not text.startswith("{"):
        return  # "show" (前に出すだけ)
    try:
        message = json.loads(text)
    except ValueError:
        return
    if not isinstance(message, dict) or message.get("cmd") != "open" or not message.get("folder"):
        return
    folder = Path(str(message["folder"]))
    select = [Path(str(p)) for p in message.get("select") or []]
    sender = str(message.get("from") or "")
    # 保存の確認などのダイアログを出すことがあるので、受け取りを終えてから開く
    QTimer.singleShot(0, lambda: window.open_from_link(folder, select, sender))


def _bring_to_front(window: MainWindow) -> None:
    if window.isMinimized():
        window.setWindowState(window.windowState() & ~Qt.WindowMinimized)
    window.show()
    window.raise_()
    window.activateWindow()


def _start_server(window: MainWindow) -> QLocalServer:
    server = QLocalServer(window)
    if not server.listen(_SERVER_NAME):
        QLocalServer.removeServer(_SERVER_NAME)
        server.listen(_SERVER_NAME)

    def on_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is None:
                return
            buffer = bytearray()
            state = {"done": False}

            def finish(conn=conn, buffer=buffer, state=state):
                if state["done"]:
                    return
                state["done"] = True
                # 送り手は切られるのを待っている (読み終えた合図)
                conn.disconnectFromServer()
                _bring_to_front(window)
                _handle_message(window, bytes(buffer))

            def read(conn=conn, buffer=buffer, finish=finish):
                buffer.extend(bytes(conn.readAll()))
                if b"\n" in buffer:
                    finish()

            conn.readyRead.connect(read)
            conn.disconnected.connect(finish)
            conn.disconnected.connect(conn.deleteLater)
            read()

    server.newConnection.connect(on_connection)
    return server


def main():
    app = QApplication(sys.argv)
    open_request = _read_open_arg()
    if _notify_running_instance(open_request or b"show"):
        sys.exit(0)
    app.setWindowIcon(make_icon())
    merged = profile.setup()  # 設定の置き場所を決め、初めてなら共有の設定に移す
    window = MainWindow()
    _start_server(window)
    window.show()
    if merged:
        window.statusBar().showMessage("共有の設定: " + "。".join(merged))
    if open_request:
        _handle_message(window, open_request)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
