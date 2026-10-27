import getpass
import sys

from PySide6.QtCore import Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .icon import make_icon
from .main_window import MainWindow

# 2 つ目を起動したら、先に動いているほうを前に出して自分は終わる。
# 2 つ目を閉じたときに 1 つ目のジョブリストが失われないようにするため。
_SERVER_NAME = f"PromptWordCrafter-{getpass.getuser()}"


def _notify_running_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(_SERVER_NAME)
    if not socket.waitForConnected(500):
        return False
    if sys.platform == "win32":
        # 前面に出るのを Windows に拒まれないよう、こちらから許可しておく
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def _start_server(window: MainWindow) -> QLocalServer:
    server = QLocalServer(window)
    if not server.listen(_SERVER_NAME):
        QLocalServer.removeServer(_SERVER_NAME)
        server.listen(_SERVER_NAME)

    def on_connection():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        if window.isMinimized():
            window.setWindowState(window.windowState() & ~Qt.WindowMinimized)
        window.show()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(on_connection)
    return server


def main():
    app = QApplication(sys.argv)
    if _notify_running_instance():
        sys.exit(0)
    app.setWindowIcon(make_icon())
    window = MainWindow()
    _start_server(window)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
