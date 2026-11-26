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

# 多重起動できる (2026-11-26 利用者の希望。前は 2 つ目を起動すると 1 つ目を前に出して終わっていた)。
# パイプはどれか 1 つの窓 (受け手) だけが持ち、ほかのアプリからの頼みはその窓で受ける。
# 受け手が閉じたら、残っている窓が引き継ぐ (TAKEOVER_MS ごとに確かめる)。
_SERVER_NAME = f"PromptWordCrafter-{getpass.getuser()}"
TAKEOVER_MS = 3000

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


def _is_running() -> bool:
    """ほかの窓がパイプを持っているか (つながるか)。"""
    socket = QLocalSocket()
    socket.connectToServer(_SERVER_NAME)
    if not socket.waitForConnected(300):
        return False
    socket.write(b"ping\n")  # 受け手は何もしない
    socket.flush()
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def _keep_server(window: MainWindow) -> None:
    """パイプの受け手になる。ほかの窓が持っていれば、その窓が閉じるまで待って引き継ぐ。"""
    if not _is_running():
        window._link_server = _start_server(window)
        return
    timer = QTimer(window)

    def check():
        if not _is_running():
            timer.stop()
            window._link_server = _start_server(window)

    timer.timeout.connect(check)
    timer.start(TAKEOVER_MS)


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
                if bytes(buffer).strip() in (b"", b"ping"):
                    return  # ほかの窓が受け手がいるか確かめただけ (前に出さない)
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
    # ほかのアプリからの頼み (--open) は、動いている窓があればそこで開く。ふつうの起動は新しい窓を開く
    if open_request and _notify_running_instance(open_request):
        sys.exit(0)
    app.setWindowIcon(make_icon())
    merged = profile.setup()  # 設定の置き場所を決め、初めてなら共有の設定に移す
    window = MainWindow(resume_jobs=not _is_running())
    _keep_server(window)
    window.show()
    if merged:
        window.statusBar().showMessage("共有の設定: " + "。".join(merged))
    if open_request:
        _handle_message(window, open_request)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
