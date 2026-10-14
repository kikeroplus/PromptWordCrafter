"""生成ジョブの管理と、メインウィンドウから呼ぶ「生成する」の流れ。

J-Rate の JRate.cs「生成する (ComfyUI)」以降と同じ動き:
同じ接続先には同時に 2 件まで送り、残りはアプリ側で待機させる。
"""

import os
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from . import comfy
from .comfy_dialogs import ComfySettingsDialog, GenerateDialog, GenInfoDialog, JobWindow

MAX_RUNNING_JOBS = 2  # 同じ接続先に同時に依頼する件数
CONFIRM_JOB_COUNT = 30  # これを超える件数を登録するときは確かめる

WAITING, RUNNING, DONE, FAILED, CANCELED = range(5)
STATE_TEXT = {WAITING: "待機中", RUNNING: "実行中", DONE: "完了", FAILED: "失敗", CANCELED: "キャンセル"}


class GenJob:
    """生成ジョブ 1 件 (テキストファイル 1 つを 1 回生成する)。"""

    def __init__(self, source: str, request: comfy.GenRequest, batch: bool):
        self.source = source
        self.request = request
        self.batch = batch  # まとめて生成の 1 件 (エラーの窓を出さない、完成しても開かない)
        self.cancel = threading.Event()
        self.outputs: list[str] = []
        self.state = WAITING
        self.status = ""

    @property
    def file_name(self) -> str:
        return Path(self.source).name

    @property
    def is_active(self) -> bool:
        return self.state in (WAITING, RUNNING)

    @property
    def state_text(self) -> str:
        return STATE_TEXT[self.state]

    def set_status(self, text: str):
        self.status = " ".join(line for line in (text or "").splitlines() if line)


class _Signals(QObject):
    """裏のスレッドから、画面のスレッドへ知らせる。"""

    progress = Signal(object, str)
    finished = Signal(object, object, object)  # job, outputs, error


class GenerationController(QObject):
    """生成ジョブを持ち、各画面を出す。

    text_for(path): そのファイルの今の本文 (表示中のファイルなら編集欄の内容)。
    show_status(text): ステータスバーに 1 行出す。
    """

    jobs_changed = Signal()
    job_done = Signal(object)  # 生成が終わったジョブ (出来たファイルは job.outputs)

    def __init__(self, window: QWidget, text_for: Callable[[Path], str], show_status: Callable[[str], None]):
        super().__init__(window)
        self.window = window
        self.text_for = text_for
        self.show_status = show_status
        self.jobs: list[GenJob] = []
        self.job_window: Optional[JobWindow] = None
        self._signals = _Signals()
        self._signals.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._signals.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    @property
    def active_count(self) -> int:
        return sum(1 for j in self.jobs if j.is_active)

    def job_counts(self) -> str:
        """複数のジョブが残っているときだけ件数を添える。"""
        running = sum(1 for j in self.jobs if j.state == RUNNING)
        waiting = sum(1 for j in self.jobs if j.state == WAITING)
        if running + waiting <= 1:
            return ""
        return f"  (実行中 {running} 件" + (f"・待機 {waiting} 件" if waiting else "") + ")"

    def confirm_close(self) -> bool:
        """生成中に閉じようとしたら確かめる。"""
        active = self.active_count
        if not active:
            return True
        answer = QMessageBox.question(
            self.window,
            "生成中",
            f"生成ジョブが {active} 件残っています。閉じますか？\n\n"
            "閉じると出来たファイルを受け取れません (ComfyUI 側の実行は止まりません)。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        for job in self.jobs:
            job.cancel.set()
        return True

    # ------------------------------------------------------------------
    # 生成する
    # ------------------------------------------------------------------
    def generate(self, sources: list[Path], prefill: Optional[comfy.GenRequest] = None, what: str = ""):
        """設定を聞いてから、sources のテキストファイルをジョブに登録する。

        what は、複数のときにダイアログの題に出す対象の呼び名 (「選択中のファイル」など)。
        """
        if not sources:
            return
        workflow = comfy.active_workflow_path()
        if not workflow or not Path(workflow).is_file():
            # 初回はワークフローを指定してもらう
            if not self.show_settings():
                return
            workflow = comfy.active_workflow_path()
            if not workflow or not Path(workflow).is_file():
                return

        # プロンプトはテキストファイルの本文。ダイアログを出す前に読んでおき、
        # 画面で見せた本文をそのまま送る (まとめて生成では、本文が空のファイルは除く)
        prompts = [(source, self.text_for(source)) for source in sources]
        skipped = 0
        if len(prompts) > 1:
            kept = [(source, text) for source, text in prompts if text.strip()]
            skipped = len(prompts) - len(kept)
            prompts = kept
            if not prompts:
                QMessageBox.information(self.window, "生成する", "本文のあるテキストファイルがありません。")
                return
        if len(prompts) > 1:
            title = f"まとめて生成する - {what} {len(prompts)} 件"
            if skipped:
                title += f" (本文が空の {skipped} 件は除きます)"
        else:
            title = f"生成する - {prompts[0][0].name}"

        while True:
            dialog = GenerateDialog(title, [(source.name, text) for source, text in prompts], prefill, self.window)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                break
            if dialog.reopen is None:
                return
            # 「生成の設定...」が押された: 設定を出してから、入力していた内容で開き直す。
            # 設定を OK で閉じたときは、そこで選んだ接続先とワークフローを使う
            prefill = dialog.reopen
            if self.show_settings():
                prefill.url = ""
                prefill.workflow_path = ""
        request = dialog.request
        repeat = dialog.repeat
        requests = [(source, request.copy(prompt=text.strip())) for source, text in prompts]

        total = len(requests) * repeat
        if total > CONFIRM_JOB_COUNT and QMessageBox.question(
            self.window, title, f"合計 {total} 件の生成を登録します。よろしいですか？",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Ok:
            return

        batch = total > 1
        for source, req in requests:
            for _ in range(repeat):
                self.jobs.append(GenJob(str(source), req, batch))
        if batch:
            self.show_jobs()
        self._start_jobs()
        if skipped:
            self.show_status(f"本文が空の {skipped} 件は生成しませんでした。" + self.job_counts())

    def _start_jobs(self):
        """待機中のジョブを、接続先ごとに MAX_RUNNING_JOBS 件まで実行に移す。"""
        for job in [j for j in self.jobs if j.state == WAITING]:
            url = job.request.url
            if sum(1 for j in self.jobs if j.state == RUNNING and j.request.url == url) >= MAX_RUNNING_JOBS:
                continue
            self._run(job)
        self._changed()

    def _run(self, job: GenJob):
        job.state = RUNNING
        job.set_status("準備中...")
        self._show_job_status(job)
        signals = self._signals

        def work():
            outputs = None
            error = None
            try:
                outputs = comfy.generate(
                    job.source, job.request, lambda text: signals.progress.emit(job, text), job.cancel
                )
                for path in outputs:
                    comfy.add_history(path, job.source, job.request)  # どの設定で作ったかを残す
            except Exception as exc:  # noqa: BLE001 - どんな失敗もジョブの失敗として見せる
                error = exc
            signals.finished.emit(job, outputs, error)

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, job: GenJob, text: str):
        if job.state != RUNNING:
            return  # 完了後に届いた古い進捗は無視
        job.set_status("キャンセル中..." if job.cancel.is_set() else text)
        self._show_job_status(job)
        self._changed()

    def _on_finished(self, job: GenJob, outputs, error):
        failed = False
        message = ""
        if isinstance(error, comfy.GenerationCancelled) or (error is not None and job.cancel.is_set()):
            job.state = CANCELED
            job.set_status("キャンセルしました")
            self.show_status(f"生成をキャンセルしました: {job.file_name}" + self.job_counts())
        elif error is not None:
            failed = True
            job.state = FAILED
            if isinstance(error, comfy.ComfyConnectionError):
                message = (
                    f"ComfyUI に接続できません ({job.request.url})。\n"
                    f"ComfyUI が起動しているか確認してください。\n\n{error}"
                )
            elif isinstance(error, comfy.ComfyError):
                message = str(error)
            else:
                message = f"生成できませんでした。\n{error}"
            job.set_status(message)
            self.show_status(f"生成できませんでした: {job.file_name}" + self.job_counts())
        else:
            job.outputs = list(outputs)
            job.state = DONE
            extra = f" ほか {len(outputs) - 1} 件" if len(outputs) > 1 else ""
            job.set_status(f"生成しました: {Path(outputs[0]).name}{extra}")
            self.show_status(job.status + self.job_counts())
            # 1 件だけの生成なら、出来たファイルを開く (まとめて生成のときは次々に開いてしまうので開かない)
            if not job.batch and comfy.open_result():
                self._open_file(outputs[0])
            self.job_done.emit(job)

        self._start_jobs()
        if not any(j.is_active for j in self.jobs):
            self._notify_finished()
        if failed and not job.batch:
            QMessageBox.warning(self.window, "生成する", f"{job.file_name}\n\n{message}")

    def cancel_job(self, job: GenJob):
        """待機中ならその場で取り消し、実行中なら ComfyUI 側の実行も止める。"""
        if job.state == WAITING:
            job.state = CANCELED
            job.set_status("キャンセルしました")
            self._changed()
        elif job.state == RUNNING and not job.cancel.is_set():
            job.set_status("キャンセル中...")
            job.cancel.set()
            self._changed()

    def _show_job_status(self, job: GenJob):
        self.show_status(f"生成 {job.file_name} : {job.status}" + self.job_counts())

    def _changed(self):
        if self.job_window is not None:
            self.job_window.refresh()
        self.jobs_changed.emit()

    def _notify_finished(self):
        """すべてのジョブが終わった。ほかの作業をしているときはタスクバーの点滅と音で知らせる。"""
        if QApplication.activeWindow() is not None:
            return
        QApplication.alert(self.window, 0)
        QApplication.beep()

    def _open_file(self, path: str):
        try:
            os.startfile(path)  # 関連付けられたアプリで開く
        except OSError as exc:
            self.show_status(f"ファイルを開けません: {Path(path).name} ({exc})")

    # ------------------------------------------------------------------
    # 各画面
    # ------------------------------------------------------------------
    def show_jobs(self):
        if self.job_window is not None:
            self.job_window.show()
            self.job_window.raise_()
            self.job_window.activateWindow()
            return
        self.job_window = JobWindow(self, self.cancel_job, self._show_result, self.window)
        self.job_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.job_window.destroyed.connect(self._job_window_closed)
        self.job_window.show()

    def _job_window_closed(self):
        self.job_window = None

    def _show_result(self, job: GenJob):
        path = next((p for p in job.outputs if Path(p).is_file()), None)
        if path is None:
            self.show_status(f"ファイルが見つかりません: {Path(job.outputs[0]).name}")
            return
        self._open_file(path)

    def show_settings(self) -> bool:
        # インポートしたあとは、読み込んだ内容で開き直す
        while True:
            dialog = ComfySettingsDialog(self.window)
            ok = dialog.exec() == QDialog.DialogCode.Accepted
            if not dialog.imported:
                return ok

    def has_history(self, path: Path) -> bool:
        return comfy.find_history_by_source(str(path)) is not None

    def show_gen_info(self, path: Path):
        """そのテキストファイルから最後に生成したときの設定を見せる。"""
        record = comfy.find_history_by_source(str(path))
        if record is None:
            QMessageBox.information(
                self.window, "生成時の設定", f"{path.name}\n\nこのファイルから生成した記録はありません。"
            )
            return
        source = Path(record.source)
        current = self.text_for(source) if source.is_file() else None
        dialog = GenInfoDialog(path.name, record, current, self.window)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.regenerate:
            # 接続先・ワークフロー・長さは記録のまま、プロンプトは今のファイルの本文で
            self.generate([source], record.request)
