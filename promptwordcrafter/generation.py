"""生成ジョブの管理と、メインウィンドウから呼ぶ「生成する」の流れ。

J-Rate の JRate.cs「生成する (ComfyUI)」以降と同じ動き:
同じ接続先には同時に 2 件まで送り、残りはアプリ側で待機させる。

終わっていないジョブは %APPDATA%/PromptWordCrafter/comfy_jobs.json に書いておき、
「残して閉じる」で閉じたときや PC が落ちたときは、次に起動したときに続きから始められる。
"""

import json
import os
import threading
import time
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
        self.prompt_id = ""  # ComfyUI が受け付けた番号 (続きから始めるときに使う)
        self.held = False  # 一時停止中に待機へ戻したもの (再開するまで実行に移さない)
        self.started = 0.0  # 実行に移った時刻 (time.monotonic)
        self.run_time: Optional[float] = None  # 生成にかかった秒数 (完了したときに決まる)

    @property
    def file_name(self) -> str:
        return Path(self.source).name

    @property
    def is_active(self) -> bool:
        return self.state in (WAITING, RUNNING)

    @property
    def is_running(self) -> bool:
        return self.state == RUNNING

    @property
    def state_text(self) -> str:
        return STATE_TEXT[self.state]

    def set_status(self, text: str):
        self.status = " ".join(line for line in (text or "").splitlines() if line)


class _Signals(QObject):
    """裏のスレッドから、画面のスレッドへ知らせる。"""

    progress = Signal(object, str)
    submitted = Signal(object, str)  # job, prompt_id
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
        self._signals.submitted.connect(self._on_submitted, Qt.ConnectionType.QueuedConnection)
        self._signals.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._saved_jobs: Optional[str] = None  # 最後に書いた内容 (変わったときだけ書く)
        # 前回終わらなかったジョブ (offer_resume で聞くまでは書き換えない)
        self._leftover, self._leftover_paused = _load_jobs()
        # 一時停止中は新しく ComfyUI に依頼しない (依頼済みのものは最後まで生成して受け取る)。
        # 終わっていないジョブがなくなったら解く
        self.paused = False

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
        text = ""
        if running + waiting > 1:
            text = f"  (実行中 {running} 件" + (f"・待機 {waiting} 件" if waiting else "") + ")"
        return text + ("  [一時停止中]" if self.paused else "")

    def set_paused(self, paused: bool):
        if paused == self.paused or (paused and not self.active_count):
            return
        self.paused = paused
        if paused:
            running = sum(1 for j in self.jobs if j.state == RUNNING)
            self.show_status(
                "生成ジョブを一時停止しました。"
                + (f"実行中の {running} 件は最後まで生成します。" if running else "")
            )
        else:
            self.show_status("生成ジョブを再開しました。" + self.job_counts())
        self._start_jobs()

    def confirm_close(self) -> bool:
        """生成中に閉じようとしたら、残すか捨てるかを確かめる。"""
        active = self.active_count
        if not active:
            return True
        if self.paused and not any(j.state == RUNNING for j in self.jobs):
            # 一時停止して止まりきっているなら、聞かずに残す (次に起動したときも一時停止のまま)
            self._save_jobs()
            return True
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("生成中")
        box.setText(f"生成ジョブが {active} 件残っています。")
        if self.paused:
            keep_text = "次に起動したときも一時停止のまま残ります (実行中のものは、起動したときに結果を受け取ります)。"
        else:
            keep_text = "次に起動したときに続きから始められます。"
        box.setInformativeText(
            f"残して閉じる: {keep_text}\n"
            "捨てて閉じる: 残りのジョブは取り消します。\n\n"
            "どちらでも、ComfyUI 側で実行中・順番待ちのものは止まりません。"
        )
        keep = box.addButton("残して閉じる", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("捨てて閉じる", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("やめる", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(stay)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep:
            self._save_jobs()  # 今の状態を確実に書いておく (裏のスレッドは閉じると止まる)
            return True
        if clicked is discard:
            for job in self.jobs:
                job.cancel.set()
            _write_jobs([])
            return True
        return False

    def offer_resume(self):
        """前回終わらなかったジョブがあれば、続きから始めるか中断するかを聞く。起動の直後に呼ぶ。"""
        leftover, self._leftover = self._leftover, None
        if not leftover:
            return
        jobs = []
        missing = 0
        for item in leftover:
            job = _job_from_dict(item)
            if job is None:
                continue
            if not Path(job.source).is_file():
                missing += 1
                continue
            jobs.append(job)
        if not jobs:
            _write_jobs([])
            if missing:
                QMessageBox.information(
                    self.window, "生成ジョブ",
                    f"前回終わらなかった生成ジョブが {missing} 件ありましたが、"
                    "元のテキストファイルが見つからないため続けられません。",
                )
            return

        if self._leftover_paused:
            # 一時停止したまま閉じたもの: 聞かずに一時停止のまま一覧に戻す
            # (依頼済みのものだけ、ComfyUI から結果を受け取る)
            self.paused = True
            self.jobs.extend(jobs)
            self._start_jobs()
            message = f"前回一時停止した生成ジョブが {len(jobs)} 件あります。ジョブ一覧の「再開」で続きを生成します。"
            if missing:
                message += f" (元のテキストファイルが見つからない {missing} 件は除きました)"
            self.show_status(message)
            return

        sent = sum(1 for j in jobs if j.prompt_id)
        detail = f"(ComfyUI に依頼済み {sent} 件・待機 {len(jobs) - sent} 件)"
        if missing:
            detail += f"\n元のテキストファイルが見つからない {missing} 件は除きます。"
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("生成ジョブ")
        box.setText(f"前回終わらなかった生成ジョブが {len(jobs)} 件あります。")
        box.setInformativeText(
            detail + "\n\n"
            "続きから始める: 依頼済みのものは ComfyUI から結果を受け取ります "
            "(ComfyUI 側に残っていなければ依頼し直します)。\n"
            "ジョブを中断: 残りのジョブを取り消します。"
        )
        resume = box.addButton("続きから始める", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("ジョブを中断", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(resume)
        box.setEscapeButton(resume)  # うっかり Esc で捨てないように
        box.exec()
        if box.clickedButton() is not resume:
            # ComfyUI に依頼済みのものは、待ち行列から外しておく (つながらなければそのまま)
            def drop():
                for job in jobs:
                    if job.prompt_id:
                        comfy.cancel_prompt(job.request.url, job.prompt_id)

            threading.Thread(target=drop, daemon=True).start()
            _write_jobs([])
            self.show_status(f"前回の生成ジョブ {len(jobs)} 件を中断しました。")
            return
        self.jobs.extend(jobs)
        self.show_jobs()
        self._start_jobs()

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
        if batch or self.paused:
            self.show_jobs()
        self._start_jobs()
        message = f"本文が空の {skipped} 件は生成しませんでした。" if skipped else ""
        if self.paused:
            message += "一時停止中です。ジョブ一覧の「再開」で生成を始めます。"
        if message:
            self.show_status(message + self.job_counts())

    def _start_jobs(self):
        """待機中のジョブを、接続先ごとに MAX_RUNNING_JOBS 件まで実行に移す。"""
        for job in [j for j in self.jobs if j.state == WAITING]:
            if self.paused and (not job.prompt_id or job.held):
                continue  # 一時停止中は新しく依頼しない (依頼済みのものは結果を受け取る)
            url = job.request.url
            if sum(1 for j in self.jobs if j.state == RUNNING and j.request.url == url) >= MAX_RUNNING_JOBS:
                continue
            self._run(job)
        self._changed()

    def _run(self, job: GenJob):
        job.state = RUNNING
        job.held = False
        job.started = time.monotonic()
        job.set_status("準備中...")
        self._show_job_status(job)
        signals = self._signals
        prompt_id = job.prompt_id

        def work():
            outputs = None
            error = None
            try:
                outputs = comfy.generate(
                    job.source, job.request, lambda text: signals.progress.emit(job, text), job.cancel,
                    prompt_id, lambda new_id: signals.submitted.emit(job, new_id),
                    lambda seconds: setattr(job, "run_time", seconds),
                    lambda: self.paused,
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

    def _on_submitted(self, job: GenJob, prompt_id: str):
        job.prompt_id = prompt_id
        self._save_jobs()

    def _on_finished(self, job: GenJob, outputs, error):
        failed = False
        message = ""
        if isinstance(error, comfy.GenerationHeld) and not job.cancel.is_set():
            # 一時停止中に ComfyUI 側から消えた・つながらなくなった: 待機に戻して再開を待つ
            job.state = WAITING
            job.held = True
            if not error.sent:
                job.prompt_id = ""
            job.set_status("一時停止のため待機に戻しました")
            self.show_status(f"{job.file_name} を待機に戻しました" + self.job_counts())
        elif isinstance(error, comfy.GenerationCancelled) or (error is not None and job.cancel.is_set()):
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
            if job.run_time is None:
                # ComfyUI の記録から分からなかったときは、アプリで実行に移してからの時間
                job.run_time = time.monotonic() - job.started
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
        if self.paused and not self.active_count:
            self.paused = False
        if self.job_window is not None:
            self.job_window.refresh()
        self.jobs_changed.emit()
        self._save_jobs()

    def _save_jobs(self):
        """終わっていないジョブを書いておく。前回のジョブをまだ聞いていないうちは書かない。"""
        if self._leftover is not None:
            return
        items = [_job_to_dict(j) for j in self.jobs if j.is_active]
        text = json.dumps([self.paused, items], ensure_ascii=False)
        if text == self._saved_jobs:
            return
        if _write_jobs(items, self.paused):
            self._saved_jobs = text

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


# ----------------------------------------------------------------------
# 終わっていないジョブ (%APPDATA%\PromptWordCrafter\comfy_jobs.json)
# ----------------------------------------------------------------------
JOBS_FILE = "comfy_jobs.json"


def _job_to_dict(job: GenJob) -> dict:
    return {
        "source": job.source,
        "batch": job.batch,
        "promptId": job.prompt_id,
        "request": dict(job.request.__dict__),
    }


def _job_from_dict(data) -> Optional[GenJob]:
    if not isinstance(data, dict) or not isinstance(data.get("request"), dict):
        return None
    source = data.get("source")
    if not isinstance(source, str) or not source:
        return None
    fields = comfy.GenRequest.__dataclass_fields__
    try:
        request = comfy.GenRequest(**{k: v for k, v in data["request"].items() if k in fields})
    except TypeError:
        return None
    job = GenJob(source, request, bool(data.get("batch")))
    job.prompt_id = str(data.get("promptId") or "")
    return job


def _load_jobs() -> tuple[list, bool]:
    """(ジョブの一覧, 一時停止したまま閉じたか)。前の版の、一覧だけの形も読む。"""
    try:
        path = comfy.file_path(JOBS_FILE)
        if not path.is_file():
            return [], False
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return [], False
    if isinstance(data, dict):
        jobs = data.get("jobs")
        return (jobs if isinstance(jobs, list) else []), bool(data.get("paused"))
    return (data if isinstance(data, list) else []), False


def _write_jobs(items: list, paused: bool = False) -> bool:
    """書けたら True。空なら消す。書きかけで落ちても壊れないよう、別名に書いてから置き換える。"""
    try:
        path = comfy.file_path(JOBS_FILE)
        if not items:
            if path.exists():
                path.unlink()
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        data = {"paused": paused, "jobs": items}
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(path)
        return True
    except OSError:
        return False
