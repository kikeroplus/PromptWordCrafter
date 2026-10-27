"""生成 (ComfyUI) の画面: 生成の設定、生成する、生成ジョブ、生成時の設定。

J-Rate の ComfySettingsDialog / ComfyPromptDialog / JobWindow / GenInfoDialog と同じ作り。
"""

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import comfy

OK_COLOR = "#2e7d32"
ERROR_COLOR = "#b71c1c"
HINT_COLOR = "#666666"

MAX_SECONDS = 60
MAX_REPEAT = 20


def _swapped(index: int, a: int, b: int) -> int:
    """a 番と b 番を入れ替えたあとの、index 番の行き先。"""
    return b if index == a else a if index == b else index


def _move_button(text: str, tip: str) -> QPushButton:
    """並び替えのボタン (押し続けると繰り返す)。"""
    button = QPushButton(text)
    button.setToolTip(tip)
    button.setAutoRepeat(True)
    button.setAutoRepeatDelay(400)
    button.setAutoRepeatInterval(120)
    return button


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {HINT_COLOR};")
    return label


def _shorten(text: str, limit: int = 300) -> str:
    return text[:limit] + "..." if len(text) > limit else text


def copy_params(button: QPushButton, label: str, request: comfy.GenRequest) -> None:
    """パラメーターをクリップボードへ。押したボタンの表示をしばらく「コピーしました」にする。"""
    QApplication.clipboard().setText(comfy.params_to_text(request))
    button.setMinimumWidth(button.width())
    button.setText("コピーしました")
    QTimer.singleShot(1500, lambda: button.setText(label))


def paste_params() -> Optional[comfy.GenRequest]:
    return comfy.parse_params(QApplication.clipboard().text())


# ----------------------------------------------------------------------
# 生成の設定
# ----------------------------------------------------------------------
class _PresetTab:
    """タブ 1 枚分: プリセットの一覧、名前と値の欄、「プリセットを保存」「削除」「▲ 上へ」「▼ 下へ」。"""

    def __init__(self, presets, caption, name_caption, value_caption, value_widget, side_button,
                 label, normalize, save, swapped):
        self.list = presets
        self.value_widget = value_widget
        self._label = label
        self._normalize = normalize
        self._save = save
        self._swapped = swapped
        self._relabeling = False
        self.shown = -1

        self.panel = QWidget()
        layout = QVBoxLayout(self.panel)
        layout.addWidget(QLabel(caption))
        self.box = QComboBox()
        self.box.setMaxVisibleItems(20)
        for i, preset in enumerate(presets):
            self.box.addItem(label(preset, i))
        layout.addWidget(self.box)
        layout.addSpacing(6)

        layout.addWidget(QLabel(name_caption))
        self.name_edit = QLineEdit()
        layout.addWidget(self.name_edit)
        layout.addSpacing(6)

        layout.addWidget(QLabel(value_caption))
        row = QHBoxLayout()
        row.addWidget(value_widget, 1)
        if side_button is not None:
            row.addWidget(side_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)
        layout.addSpacing(6)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        up = _move_button("▲ 上へ", "選んでいるプリセットを 1 つ上の番号と入れ替えます (押し続けると続けて動きます)")
        down = _move_button("▼ 下へ", "選んでいるプリセットを 1 つ下の番号と入れ替えます (押し続けると続けて動きます)")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        buttons.addWidget(up)
        buttons.addWidget(down)
        buttons.addSpacing(24)
        self.save_button = QPushButton("プリセットを保存")
        self.delete_button = QPushButton("削除")
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)
        self.extra = layout  # 説明などを足す場所

        # 番号を選ぶと、その名前と値を欄に出す (保存していない入力は捨てる)
        self.box.currentIndexChanged.connect(self._on_select)

    def _set_value_text(self, text: str):
        if isinstance(self.value_widget, QPlainTextEdit):
            self.value_widget.setPlainText(text)
        else:
            self.value_widget.setText(text)

    def value_text(self) -> str:
        if isinstance(self.value_widget, QPlainTextEdit):
            return self.value_widget.toPlainText()
        return self.value_widget.text()

    def _on_select(self, index: int):
        if self._relabeling or index < 0:
            return
        self.shown = index
        self.name_edit.setText(self.list[index].name)
        self._set_value_text(self.list[index].value)

    def select(self, index: int):
        self.box.setCurrentIndex(index)
        self._on_select(index)

    @property
    def value(self) -> str:
        return self._normalize(self.value_text())

    def store(self):
        """欄の名前と値を、選んでいる番号のプリセットとして保存する (キャンセルしても残る)。"""
        self.list[self.shown].name = self.name_edit.text().strip()
        self.list[self.shown].value = self.value
        self._saved()

    def delete(self):
        self.list[self.shown].clear()
        self.name_edit.setText("")
        self._set_value_text("")
        self._saved()

    def _saved(self):
        self._relabeling = True
        self.box.setItemText(self.shown, self._label(self.list[self.shown], self.shown))
        self.box.setCurrentIndex(self.shown)
        self._relabeling = False
        self._save(self.list)

    def _move(self, step: int):
        """隣の番号と入れ替えて、すぐ保存する。欄の内容 (保存していない入力も) はそのまま付いていく。"""
        a, b = self.shown, self.shown + step
        if a < 0 or b < 0 or b >= len(self.list):
            return
        self.list[a], self.list[b] = self.list[b], self.list[a]
        self._relabeling = True
        self.box.setItemText(a, self._label(self.list[a], a))
        self.box.setItemText(b, self._label(self.list[b], b))
        self.box.setCurrentIndex(b)
        self._relabeling = False
        self.shown = b
        self._save(self.list)
        self._swapped(a, b)


class _Worker(QObject):
    """接続テストを裏で行う。"""

    done = Signal(object)

    def __init__(self, func):
        super().__init__()
        self._func = func

    def run(self):
        self.done.emit(self._func())


class ComfySettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("生成の設定 (ComfyUI)")
        self.resize(640, 520)
        self.imported = False  # インポートして閉じたとき (呼び出し側が、読み込んだ内容で開き直す)
        self._thread: Optional[QThread] = None

        sets = comfy.sets()  # 「セット」のタブで並び替える
        self._sets = sets

        test = QPushButton("接続テスト")
        self.connection = _PresetTab(
            comfy.connections(),
            f"接続先のプリセット ({comfy.PRESET_COUNT} 件まで登録できます。ここで選んだものを使います)",
            "名前 (一覧に出す名前。空でもかまいません)",
            "ComfyUI の URL",
            QLineEdit(), test,
            lambda p, i: p.label(i), lambda s: s.strip().rstrip("/"), comfy.set_connections,
            lambda a, b: comfy.set_active_connection(_swapped(comfy.active_connection(), a, b)),
        )

        browse = QPushButton("参照...")
        self.workflow = _PresetTab(
            comfy.workflows(),
            f"ワークフローのプリセット ({comfy.WORKFLOW_COUNT} 件まで登録できます。ここで選んだものを使います)",
            "名前 (一覧に出す名前。空ならファイル名を出します)",
            "ワークフロー (ComfyUI のメニュー「Export (API)」で保存した JSON)",
            QLineEdit(), browse,
            lambda p, i: p.file_label(i), lambda s: s.strip().strip('"'), comfy.set_workflows,
            lambda a, b: comfy.set_active_workflow(_swapped(comfy.active_workflow(), a, b)),
        )
        self.workflow.extra.addWidget(_hint(
            "右クリックしたテキストファイルの本文が、そのままワークフローのプロンプトに入ります "
            "(prompt 入力、なければ CLIPTextEncode。複数あるときはタイトルに Positive とあるもの)。"
            "プロンプトを入れる場所のないワークフローは登録できません。seed は毎回ランダムになります。"
            "出来たファイルはテキストファイルと同じフォルダに、動画なら「(元の名前)_video.mp4」、"
            "画像なら「(元の名前)_t2i.png」で保存します。"
        ))

        # ベースプロンプト: 動画用と画像用が 1 つずつ。OK で保存する
        base_panel = QWidget()
        base_layout = QVBoxLayout(base_panel)
        intro = QLabel("ベースプロンプト (毎回、テキストファイルの本文のあとに続けて ComfyUI に送ります。空なら何も足しません)")
        intro.setWordWrap(True)
        base_layout.addWidget(intro)
        self.base_video = QPlainTextEdit(comfy.base_prompt_video())
        self.base_image = QPlainTextEdit(comfy.base_prompt_image())
        base_layout.addWidget(QLabel("動画用 (動画を作るワークフローのとき)"))
        base_layout.addWidget(self.base_video)
        base_layout.addWidget(QLabel("画像用 (画像を作るワークフローのとき)"))
        base_layout.addWidget(self.base_image)
        base_layout.addWidget(_hint(
            "動画用か画像用かは、ワークフローのノードから自動で見分けます (「生成する」の画面に、どちらを使うかが出ます)。"
            "本文とベースプロンプトの間は改行で区切ります。OK を押すと保存します。"
        ))

        # セット: ここでは並び替えだけ (保存と削除は「生成する」の画面で行う)
        set_panel = QWidget()
        set_layout = QVBoxLayout(set_panel)
        set_layout.addWidget(QLabel("セットの並び替え (セットの保存と削除は「生成する」の画面で行います)"))
        self.set_list = QListWidget()
        for i, s in enumerate(sets):
            self.set_list.addItem(s.label(i))
        self.set_list.setCurrentRow(max(comfy.last_set(), 0))
        set_layout.addWidget(self.set_list)
        set_buttons = QHBoxLayout()
        set_buttons.addStretch(1)
        set_up = _move_button("▲ 上へ", "選んでいるセットを 1 つ上の番号と入れ替えます (押し続けると続けて動きます)")
        set_down = _move_button("▼ 下へ", "選んでいるセットを 1 つ下の番号と入れ替えます (押し続けると続けて動きます)")
        set_up.clicked.connect(lambda: self._move_set(-1))
        set_down.clicked.connect(lambda: self._move_set(1))
        set_buttons.addWidget(set_up)
        set_buttons.addWidget(set_down)
        set_layout.addLayout(set_buttons)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.connection.panel, "接続先")
        self.tabs.addTab(self.workflow.panel, "ワークフロー")
        self.tabs.addTab(base_panel, "ベースプロンプト")
        self.tabs.addTab(set_panel, "セット")

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        self.open_result = QCheckBox("生成が終わったら、出来たファイルを開く (まとめて生成のときは開きません)")
        self.open_result.setChecked(comfy.open_result())
        layout.addWidget(self.open_result)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        bottom = QHBoxLayout()
        export_button = QPushButton("エクスポート...")
        import_button = QPushButton("インポート...")
        bottom.addWidget(export_button)
        bottom.addWidget(import_button)
        bottom.addStretch(1)
        ok = QPushButton("OK")
        ok.setDefault(True)
        cancel = QPushButton("キャンセル")
        bottom.addWidget(ok)
        bottom.addWidget(cancel)
        layout.addLayout(bottom)

        export_button.clicked.connect(self._export)
        import_button.clicked.connect(self._import)
        ok.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)

        self.connection.select(comfy.active_connection())
        self.workflow.select(comfy.active_workflow())

        self.connection.save_button.clicked.connect(lambda: self._store(self.connection, "接続先"))
        self.workflow.save_button.clicked.connect(self._store_workflow)
        self.connection.delete_button.clicked.connect(lambda: self._delete(self.connection, "接続先"))
        self.workflow.delete_button.clicked.connect(lambda: self._delete(self.workflow, "ワークフロー"))
        test.clicked.connect(lambda: self._test(test))
        browse.clicked.connect(self._browse)

    def _result(self, color: str, text: str):
        self.result_label.setStyleSheet(f"color: {color};")
        self.result_label.setText(text)

    def _store(self, tab: _PresetTab, what: str):
        tab.store()
        state = "を未登録にしました" if tab.list[tab.shown].is_empty else "を保存しました"
        self._result(OK_COLOR, f"{what}のプリセット {tab.shown + 1} {state}")

    def _store_workflow(self):
        # JSON が空なら未登録に戻す。入っていれば確かめてから保存する
        path = self.workflow.value
        if path and not self._check(path):
            return
        self._store(self.workflow, "ワークフロー")

    def _delete(self, tab: _PresetTab, what: str):
        preset = tab.list[tab.shown]
        if not preset.is_empty or preset.name:
            text = f"{what}のプリセット {tab.shown + 1} を削除しますか？\n\n"
            if preset.name:
                text += preset.name + "\n"
            text += _shorten(preset.value)
            if QMessageBox.question(self, self.windowTitle(), text) != QMessageBox.StandardButton.Yes:
                return
        tab.delete()
        self._result(OK_COLOR, f"{what}のプリセット {tab.shown + 1} を削除しました")

    def _move_set(self, step: int):
        a = self.set_list.currentRow()
        b = a + step
        if a < 0 or b < 0 or b >= len(self._sets):
            return
        self._sets[a], self._sets[b] = self._sets[b], self._sets[a]
        self.set_list.item(a).setText(self._sets[a].label(a))
        self.set_list.item(b).setText(self._sets[b].label(b))
        self.set_list.setCurrentRow(b)
        comfy.set_sets(self._sets)
        last = comfy.last_set()
        if last >= 0:
            comfy.set_last_set(_swapped(last, a, b))

    def _test(self, button: QPushButton):
        url = self.connection.value
        self._result(HINT_COLOR, "接続中...")
        button.setEnabled(False)

        def finished(error):
            button.setEnabled(True)
            if error is None:
                self._result(OK_COLOR, f"ComfyUI に接続できました: {url}")
            else:
                self._result(ERROR_COLOR, f"接続できません: {error}")
            self._thread.quit()

        self._thread = QThread(self)
        worker = _Worker(lambda: comfy.test_connection(url))
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.done.connect(finished)
        self._thread.finished.connect(worker.deleteLater)
        self._worker = worker
        self._thread.start()

    def _browse(self):
        start = ""
        current = self.workflow.value
        if current and Path(current).is_file():
            start = str(Path(current).parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "API 形式のワークフロー JSON を選択", start,
            "ワークフロー JSON (*.json);;すべてのファイル (*.*)",
        )
        if path:
            self.workflow.value_widget.setText(str(Path(path)))
            self._check(str(Path(path)))

    def _check(self, path: str) -> bool:
        try:
            wf = comfy.load_workflow(path)
            problem = comfy.prompt_slot_problem(wf)
            if problem:
                raise comfy.ComfyError(problem)
        except comfy.ComfyError as exc:
            self._result(ERROR_COLOR, str(exc))
            return False
        self._result(OK_COLOR, comfy.describe_workflow(wf))
        return True

    def _export(self):
        from datetime import datetime

        path, _ = QFileDialog.getSaveFileName(
            self, "生成の設定をエクスポート",
            f"PromptWordCrafter-生成設定-{datetime.now():%Y%m%d}.json",
            "生成設定 (*.json)",
        )
        if not path:
            return
        try:
            self._result(OK_COLOR, comfy.export_settings(path))
        except OSError as exc:
            self._result(ERROR_COLOR, f"エクスポートできません: {exc}")

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "生成の設定をインポート", "", "生成設定 (*.json);;すべてのファイル (*.*)"
        )
        if not path:
            return
        text = (
            "今の生成の設定 (接続先・ワークフロー・プロンプト・セットなど) を、"
            f"このファイルの内容にすべて置き換えます。よろしいですか？\n\n{path}\n\n"
            f"今の設定は次のファイルに控えを残します。\n{comfy.import_backup_path()}"
        )
        if QMessageBox.warning(
            self, self.windowTitle(), text,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Ok:
            return
        try:
            done = comfy.import_settings(path)
        except (comfy.ComfyError, OSError) as exc:
            self._result(ERROR_COLOR, f"インポートできません: {exc}")
            return
        QMessageBox.information(self, self.windowTitle(), done)
        self.imported = True
        self.reject()

    def _accept(self):
        """出している内容を保存して、選んでいる接続先とワークフローを使うものにする。"""
        if not self.connection.value:
            self.tabs.setCurrentIndex(0)
            QMessageBox.warning(self, self.windowTitle(), "選んでいる接続先の URL を入力してください。")
            self.connection.value_widget.setFocus()
            return
        path = self.workflow.value
        if not path:
            self.tabs.setCurrentIndex(1)
            QMessageBox.warning(self, self.windowTitle(), "ワークフローの JSON を指定してください。")
            return
        if not self._check(path):
            self.tabs.setCurrentIndex(1)
            return
        self.connection.store()
        self.workflow.store()
        comfy.set_base_prompt_video(self.base_video.toPlainText())
        comfy.set_base_prompt_image(self.base_image.toPlainText())
        comfy.set_active_connection(self.connection.shown)
        comfy.set_active_workflow(self.workflow.shown)
        comfy.set_open_result(self.open_result.isChecked())
        self.accept()


# ----------------------------------------------------------------------
# 生成する
# ----------------------------------------------------------------------
class GenerateDialog(QDialog):
    """「生成する」のたびに出す、接続先・ワークフロー・長さ・回数の入力画面。

    プロンプトはテキストファイルの本文そのもの。この画面では見せるだけで、書き換えない。
    prompts: 対象の (ファイル名, 本文) の並び。
    prefill: 欄の初期値 (「生成時の設定」から開いたときや、「生成の設定...」から戻ったとき)。
    プロンプトは持たない。接続先・ワークフロー・長さだけを使う。
    """

    def __init__(self, title: str, prompts: list[tuple[str, str]],
                 prefill: Optional[comfy.GenRequest], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 0)
        self.prompts = prompts
        self.batch = len(prompts) > 1
        self.request: Optional[comfy.GenRequest] = None
        self.repeat = 1
        self.reopen: Optional[comfy.GenRequest] = None  # 「生成の設定...」を押して閉じたときの各欄の内容
        self._base_prompt = ""
        self._integer_seconds = False
        self._relabeling_set = False

        layout = QVBoxLayout(self)

        # セット (接続先・ワークフロー・長さをまとめて呼び出す)
        self._sets = comfy.sets()
        set_row = QHBoxLayout()
        set_row.addWidget(QLabel("セット"))
        self.set_box = QComboBox()
        for i, s in enumerate(self._sets):
            self.set_box.addItem(s.label(i))
        self.set_box.setCurrentIndex(-1)
        set_row.addWidget(self.set_box, 1)
        self.save_set = QPushButton("保存")
        self.save_set.setToolTip("今の接続先・ワークフロー・長さを、選んでいる番号にまとめて保存します")
        self.delete_set = QPushButton("削除")
        set_row.addWidget(self.save_set)
        set_row.addWidget(self.delete_set)
        layout.addLayout(set_row)
        # 前回選んでいたセットを選んだ状態にする (欄の中身はそこから入れ直さない)
        last = comfy.last_set()
        if last >= 0 and not self._sets[last].is_empty:
            self.set_box.setCurrentIndex(last)
        self.save_set.setEnabled(self.set_box.currentIndex() >= 0)
        self.delete_set.setEnabled(self.set_box.currentIndex() >= 0)

        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #cccccc;")
        layout.addWidget(line)

        form = QFormLayout()
        # 接続先
        self._conn_index, self._conn_url, self._conn_name = [], [], []
        self.conn_box = QComboBox()
        active = comfy.active_connection()
        for i, p in enumerate(comfy.connections()):
            if p.is_empty:
                continue
            self.conn_box.addItem(p.label(i))
            self._conn_index.append(i)
            self._conn_url.append(p.value)
            self._conn_name.append(p.name)
            if i == active:
                self.conn_box.setCurrentIndex(self.conn_box.count() - 1)
        if self.conn_box.count() == 0:
            self.conn_box.addItem(comfy.DEFAULT_URL)
            self._conn_index.append(-1)
            self._conn_url.append(comfy.DEFAULT_URL)
            self._conn_name.append("")
        if self.conn_box.currentIndex() < 0:
            self.conn_box.setCurrentIndex(0)
        form.addRow("接続先", self.conn_box)

        # ワークフロー
        self._wf_index, self._wf_path, self._wf_name = [], [], []
        self.wf_box = QComboBox()
        self.wf_box.setMaxVisibleItems(20)
        active_wf = comfy.active_workflow()
        for i, p in enumerate(comfy.workflows()):
            if p.is_empty:
                continue
            self.wf_box.addItem(p.file_label(i))
            self._wf_index.append(i)
            self._wf_path.append(p.value)
            self._wf_name.append(p.name or comfy.file_name(p.value))
            if i == active_wf:
                self.wf_box.setCurrentIndex(self.wf_box.count() - 1)
        if self.wf_box.currentIndex() < 0 and self.wf_box.count() > 0:
            self.wf_box.setCurrentIndex(0)
        form.addRow("ワークフロー", self.wf_box)
        layout.addLayout(form)

        # 生成できない理由 (ワークフローを読めない、プロンプトを入れる場所がない、本文が空)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        # プロンプト = テキストファイルの本文 (見せるだけ)
        self.prompt_label = QLabel()
        layout.addWidget(self.prompt_label)
        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setFixedHeight(140)
        if self.batch:
            self.prompt_label.setText(f"プロンプト (各テキストファイルの本文をそれぞれ使います: {len(prompts)} 件)")
            self.prompt_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.prompt_view.setPlainText("\n".join(f"{name} : {_one_line(text, 120)}" for name, text in prompts))
        else:
            self.prompt_label.setText(f"プロンプト ({prompts[0][0]} の本文。書き換えるときは編集欄で)")
            self.prompt_view.setPlainText(prompts[0][1])
        layout.addWidget(self.prompt_view)
        self.base_hint = QLabel()
        self.base_hint.setStyleSheet(f"color: {HINT_COLOR};")
        self.base_hint.hide()
        layout.addWidget(self.base_hint)

        # 長さ・回数
        seconds_row = QHBoxLayout()
        seconds_row.addWidget(QLabel("長さ"))
        self.seconds_edit = QLineEdit()
        self.seconds_edit.setFixedWidth(60)
        self.seconds_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        seconds_row.addWidget(self.seconds_edit)
        self.seconds_unit = QLabel()
        seconds_row.addWidget(self.seconds_unit)
        seconds_row.addStretch(1)
        layout.addLayout(seconds_row)

        repeat_row = QHBoxLayout()
        repeat_row.addWidget(QLabel("回数"))
        self.repeat_edit = QLineEdit("1")
        self.repeat_edit.setFixedWidth(60)
        self.repeat_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        repeat_row.addWidget(self.repeat_edit)
        repeat_row.addWidget(QLabel(
            ("回ずつ" if self.batch else "回") + " (2 回以上にすると、seed を変えて繰り返します)"
        ))
        repeat_row.addStretch(1)
        layout.addLayout(repeat_row)

        # ボタン
        bottom = QHBoxLayout()
        settings_button = QPushButton("生成の設定...")
        settings_button.setToolTip("接続先・ワークフロー・ベースプロンプトの登録や並び替え")
        self.copy_button = QPushButton("コピー")
        self.copy_button.setToolTip("今の接続先・ワークフロー・長さ (1 件のときは本文も) をクリップボードにコピーします")
        paste_button = QPushButton("貼り付け")
        paste_button.setToolTip(
            "コピーした生成パラメーターの接続先・ワークフロー・長さを各欄に入れます (プロンプトはファイルの本文のまま)"
        )
        bottom.addWidget(settings_button)
        bottom.addSpacing(12)
        bottom.addWidget(self.copy_button)
        bottom.addWidget(paste_button)
        bottom.addStretch(1)
        self.ok_button = QPushButton(f"{len(prompts)} 件を生成する" if self.batch else "生成する")
        self.ok_button.setAutoDefault(False)
        cancel = QPushButton("キャンセル")
        cancel.setAutoDefault(False)
        bottom.addWidget(self.ok_button)
        bottom.addWidget(cancel)
        layout.addLayout(bottom)

        self.ok_button.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        settings_button.clicked.connect(self._open_settings)
        self.copy_button.clicked.connect(lambda: copy_params(self.copy_button, "コピー", self._fields()))
        paste_button.clicked.connect(self._paste)
        # 開始は「生成する」ボタンか Ctrl+Enter (どの欄にいても効く)
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(key), self).activated.connect(self._accept)

        self._apply_workflow()
        self.wf_box.currentIndexChanged.connect(self._apply_workflow)
        self.set_box.currentIndexChanged.connect(self._on_set)
        self.save_set.clicked.connect(self._save_set)
        self.delete_set.clicked.connect(self._delete_set)

        if prefill is not None:
            self._apply(
                prefill.url, prefill.workflow_path,
                "" if prefill.seconds is None else comfy.format_seconds(prefill.seconds),
            )

        if self.seconds_edit.isEnabled():
            self.seconds_edit.setFocus()
            self.seconds_edit.selectAll()
        else:
            self.ok_button.setFocus()

    # --- 欄の出し入れ -------------------------------------------------
    def _apply(self, url: str, workflow: str, seconds: str):
        """接続先・ワークフローは一覧にあるものだけ選び直す。"""
        for i, u in enumerate(self._conn_url):
            if u.lower() == (url or "").lower():
                self.conn_box.setCurrentIndex(i)
                break
        for i, p in enumerate(self._wf_path):
            if p.lower() == (workflow or "").lower():
                self.wf_box.setCurrentIndex(i)  # 使えるかどうかは _apply_workflow が確かめる
                break
        if self.seconds_edit.isEnabled() and _parse_seconds(seconds) is not None:
            self.seconds_edit.setText(seconds.strip())

    def _fields(self) -> comfy.GenRequest:
        """今の各欄の内容 (長さは、使えないときや正しくないときは None)。1 件のときは本文も入れる。"""
        sel = max(self.conn_box.currentIndex(), 0)
        r = comfy.GenRequest(url=self._conn_url[sel], connection_name=self._conn_name[sel])
        wf = self.wf_box.currentIndex()
        if wf >= 0:
            r.workflow_path = self._wf_path[wf]
            r.workflow_name = self._wf_name[wf]
        if not self.batch:
            r.prompt = self.prompts[0][1]
        if self.seconds_edit.isEnabled():
            r.seconds = _parse_seconds(self.seconds_edit.text())
        return r

    def _apply_workflow(self):
        """選んでいるワークフローを読んで、本文を渡せるか・長さを変えられるかを決める。

        本文を入れる場所がないワークフローや、本文が空のファイルでは「生成する」を押せなくする。
        """
        video = False
        wf_seconds = None
        error = None
        self._integer_seconds = False
        try:
            sel = self.wf_box.currentIndex()
            if sel < 0:
                raise comfy.ComfyError("ワークフローが登録されていません。「生成の設定...」で登録してください。")
            wf = comfy.load_workflow(self._wf_path[sel])
            problem = comfy.prompt_slot_problem(wf)
            if problem:
                raise comfy.ComfyError(problem)
            video = comfy.is_video_workflow(wf)
            wf_seconds = comfy.get_duration(wf)
            self._integer_seconds = comfy.duration_is_integer(wf)
        except comfy.ComfyError as exc:
            error = str(exc)
        if error is None and not self.batch and not self.prompts[0][1].strip():
            error = "テキストファイルの本文が空です。本文を書いてから生成してください。"

        ok = error is None
        self.error_label.setText(error or "")
        self.error_label.setVisible(not ok)
        self.ok_button.setEnabled(ok)

        self._base_prompt = comfy.base_prompt_for(video) if ok else ""
        kind = "動画用" if video else "画像用"
        one_line = self._base_prompt.replace("\r", "").replace("\n", " ")
        self.base_hint.setText(_shorten(f"＋ ベースプロンプト ({kind}) : {one_line}", 90))
        self.base_hint.setToolTip(self._base_prompt)
        self.base_hint.setVisible(bool(self._base_prompt))

        has_seconds = wf_seconds is not None
        if has_seconds and _parse_seconds(self.seconds_edit.text()) is None:
            # 前回の長さ、なければワークフローに書かれている長さ
            last = comfy.last_seconds()
            self.seconds_edit.setText(last if _parse_seconds(last) is not None else comfy.format_seconds(wf_seconds))
        self.seconds_edit.setEnabled(has_seconds)
        self.seconds_unit.setText("秒" if has_seconds or not ok else "秒 (このワークフローでは変えられません)")
        self.seconds_unit.setStyleSheet("" if has_seconds else f"color: {HINT_COLOR};")

    # --- セット -------------------------------------------------------
    def _on_set(self, n: int):
        if self._relabeling_set:
            return
        self.save_set.setEnabled(n >= 0)
        self.delete_set.setEnabled(n >= 0 and not self._sets[n].is_empty)
        if n < 0 or self._sets[n].is_empty:
            return
        s = self._sets[n]
        self._apply(s.url, s.workflow, s.seconds)  # セットのプロンプト (J-Rate から持ってきたもの) は使わない

    def _relabel_set(self, n: int):
        self._relabeling_set = True
        self.set_box.setItemText(n, self._sets[n].label(n))
        self.set_box.setCurrentIndex(n)
        self._relabeling_set = False
        self.delete_set.setEnabled(not self._sets[n].is_empty)

    def _save_set(self):
        n = self.set_box.currentIndex()
        wf = self.wf_box.currentIndex()
        if n < 0 or wf < 0:
            return
        s = self._sets[n]
        name, ok = QInputDialog.getText(
            self, "セットを保存", f"セット {n + 1} の名前", text=s.name or self._wf_name[wf]
        )
        if not ok:
            return
        s.name = name.strip()
        s.url = self._conn_url[max(self.conn_box.currentIndex(), 0)]
        s.workflow = self._wf_path[wf]
        s.prompt = ""
        s.prompt_preset = -1
        s.seconds = self.seconds_edit.text().strip() if self.seconds_edit.isEnabled() else ""
        comfy.set_sets(self._sets)
        self._relabel_set(n)

    def _delete_set(self):
        n = self.set_box.currentIndex()
        if QMessageBox.question(
            self, self.windowTitle(), f"セット {n + 1} を削除しますか？\n\n{self._sets[n].name}"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._sets[n] = comfy.ComfySet()
        comfy.set_sets(self._sets)
        self._relabel_set(n)

    # --- ボタン -------------------------------------------------------
    def _open_settings(self):
        self.reopen = self._fields()
        self.reject()

    def _paste(self):
        r = paste_params()
        if r is None:
            QMessageBox.information(self, self.windowTitle(), "クリップボードに、コピーした生成パラメーターがありません。")
            return
        missing = []
        if r.url and not any(u.lower() == r.url.lower() for u in self._conn_url):
            missing.append("接続先 : " + (f"{r.connection_name}  ({r.url})" if r.connection_name else r.url))
        wf = r.workflow_path
        if wf and not any(p.lower() == wf.lower() for p in self._wf_path):
            # パスが違っても同じファイル名のものがあればそれ (別の PC でコピーしたとき用)
            name = comfy.file_name(wf).lower()
            same = next((p for p in self._wf_path if comfy.file_name(p).lower() == name), None)
            if same:
                wf = same
            else:
                label = f"{r.workflow_name}  ({wf})" if r.workflow_name and r.workflow_name.lower() != name else wf
                missing.append("ワークフロー : " + label)
        self._apply(r.url, wf, "" if r.seconds is None else comfy.format_seconds(r.seconds))
        if missing:
            QMessageBox.information(
                self, self.windowTitle(),
                "次のものは登録されていないので、今の選択のままにしました。\n\n" + "\n".join(missing),
            )

    def _accept(self):
        if not self.ok_button.isEnabled():
            return  # 生成できない状態 (Ctrl+Enter で来たとき)
        try:
            repeat = int(self.repeat_edit.text().strip())
        except ValueError:
            repeat = 0
        if not 1 <= repeat <= MAX_REPEAT:
            QMessageBox.warning(self, self.windowTitle(), f"回数は 1 ～ {MAX_REPEAT} の整数で指定してください。")
            self.repeat_edit.setFocus()
            self.repeat_edit.selectAll()
            return

        # プロンプトは呼び出し側がファイルごとの本文を入れる。ここではベースプロンプトまで
        request = comfy.GenRequest(base_prompt=self._base_prompt)
        if self.seconds_edit.isEnabled():
            seconds = _parse_seconds(self.seconds_edit.text())
            if seconds is None or (self._integer_seconds and seconds != int(seconds)):
                kind = "整数" if self._integer_seconds else "数字"
                QMessageBox.warning(self, self.windowTitle(), f"長さは 1 ～ {MAX_SECONDS} 秒の{kind}で指定してください。")
                self.seconds_edit.setFocus()
                self.seconds_edit.selectAll()
                return
            request.seconds = seconds
            comfy.set_last_seconds(self.seconds_edit.text())
        sel = max(self.conn_box.currentIndex(), 0)
        request.url = self._conn_url[sel]
        request.connection_name = self._conn_name[sel]
        if self._conn_index[sel] >= 0:
            comfy.set_active_connection(self._conn_index[sel])
        wf = self.wf_box.currentIndex()
        request.workflow_path = self._wf_path[wf]
        request.workflow_name = self._wf_name[wf]
        comfy.set_active_workflow(self._wf_index[wf])
        comfy.set_last_set(self.set_box.currentIndex())
        self.request = request
        self.repeat = repeat
        self.accept()


def _one_line(text: str, limit: int) -> str:
    return _shorten(" ".join(line.strip() for line in text.splitlines() if line.strip()), limit)


def _parse_seconds(text: str) -> Optional[float]:
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if 1 <= value <= MAX_SECONDS else None


# ----------------------------------------------------------------------
# 生成ジョブの一覧 (閉じてもジョブは続く)
# ----------------------------------------------------------------------
class JobWindow(QWidget):
    COLUMNS = ("ファイル", "ワークフロー", "状態", "時間", "内容")

    def __init__(self, manager, cancel: Callable, show: Callable, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("生成ジョブ - PromptWordCrafter")
        self.resize(840, 380)
        self.manager = manager
        self._cancel = cancel
        self._show = show
        self._items = {}

        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.tree.header()
        for column, width in enumerate((190, 140, 70, 60)):
            header.resizeSection(column, width)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("選択したものをキャンセル")
        self.cancel_all_button = QPushButton("すべてキャンセル")
        self.show_button = QPushButton("結果を表示")
        self.clear_button = QPushButton("終わったものを消す")
        for button in (self.cancel_button, self.cancel_all_button, self.show_button, self.clear_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        close = QPushButton("閉じる")
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.cancel_button.clicked.connect(self._cancel_selected)
        self.cancel_all_button.clicked.connect(self._cancel_all)
        self.show_button.clicked.connect(self._show_selected)
        self.clear_button.clicked.connect(self._clear_finished)
        close.clicked.connect(self.close)
        self.tree.itemSelectionChanged.connect(self.update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self._show_selected())
        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)

        self.refresh()

    def _selected_jobs(self):
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()]

    def refresh(self):
        """ジョブの一覧と表示を合わせる。"""
        jobs = self.manager.jobs
        alive = {id(job) for job in jobs}
        for key in [k for k in self._items if k not in alive]:
            item = self._items.pop(key)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        for job in jobs:
            item = self._items.get(id(job))
            if item is None:
                item = QTreeWidgetItem()
                item.setData(0, Qt.ItemDataRole.UserRole, job)
                self.tree.insertTopLevelItem(0, item)  # 新しいものを上に
                self._items[id(job)] = item
            item.setText(0, job.file_name)
            item.setText(1, job.request.workflow_name)
            item.setText(2, job.state_text)
            item.setText(3, "" if job.run_time is None else comfy.format_elapsed(job.run_time))
            item.setText(4, job.status)
            item.setToolTip(4, job.status)
        self.update_buttons()

    def update_buttons(self):
        selected = self._selected_jobs()
        jobs = self.manager.jobs
        self.cancel_button.setEnabled(any(j.is_active for j in selected))
        self.cancel_all_button.setEnabled(any(j.is_active for j in jobs))
        self.show_button.setEnabled(len(selected) == 1 and bool(selected[0].outputs))
        self.clear_button.setEnabled(any(not j.is_active for j in jobs))

    def _cancel_selected(self):
        for job in self._selected_jobs():
            self._cancel(job)
        self.refresh()

    def _cancel_all(self):
        for job in list(self.manager.jobs):
            self._cancel(job)
        self.refresh()

    def _show_selected(self):
        selected = self._selected_jobs()
        if len(selected) == 1 and selected[0].outputs:
            self._show(selected[0])

    def _clear_finished(self):
        self.manager.jobs[:] = [j for j in self.manager.jobs if j.is_active]
        self.refresh()


# ----------------------------------------------------------------------
# 生成時の設定
# ----------------------------------------------------------------------
class GenInfoDialog(QDialog):
    def __init__(self, file_name: str, record: comfy.GenRecord, current_text: Optional[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"生成時の設定 - {file_name}")
        self.resize(620, 0)
        self.regenerate = False  # 「この設定で生成する...」が押されたら True

        req = record.request
        has_source = bool(record.source) and Path(record.source).is_file()
        layout = QVBoxLayout(self)
        form = QFormLayout()

        def row(caption, value):
            # 選んでコピーできるように、読み取り専用の欄で出す
            field = QLineEdit(value)
            field.setReadOnly(True)
            field.setFrame(False)
            field.setStyleSheet("background: transparent;")
            field.setCursorPosition(0)
            label = QLabel(caption)
            label.setStyleSheet(f"color: {HINT_COLOR};")
            form.addRow(label, field)

        row("生成した日時", record.time)
        row("元のファイル", record.source + ("" if has_source else "  (見つかりません)"))
        row("出来たファイル", record.output)
        row("接続先", f"{req.connection_name}  ({req.url})" if req.connection_name else req.url)
        same_name = req.workflow_name == comfy.file_name(req.workflow_path)
        row("ワークフロー", req.workflow_path if not req.workflow_name or same_name
            else f"{req.workflow_name}  ({req.workflow_path})")
        row("長さ", "(ワークフローのまま)" if req.seconds is None else f"{comfy.format_seconds(req.seconds)} 秒")
        layout.addLayout(form)

        layout.addWidget(QLabel("プロンプト (送ったファイルの本文)" if req.prompt is not None
                                else "プロンプト (ワークフローのまま)"))
        prompt = QPlainTextEdit(req.prompt or "")
        prompt.setReadOnly(True)
        prompt.setFixedHeight(120)
        layout.addWidget(prompt)
        if current_text is not None and req.prompt is not None and current_text.strip() != req.prompt.strip():
            layout.addWidget(_hint("※ ファイルの本文は、このあと書き換えられています。やり直すときは今の本文を使います。"))
        if req.base_prompt:
            layout.addWidget(QLabel("ベースプロンプト (プロンプトのあとに続けて送りました)"))
            base = QPlainTextEdit(req.base_prompt)
            base.setReadOnly(True)
            base.setFixedHeight(70)
            layout.addWidget(base)

        bottom = QHBoxLayout()
        copy = QPushButton("パラメーターをコピー")
        copy.setToolTip("接続先・ワークフロー・プロンプト・長さをクリップボードにコピーします (「生成する」の画面で貼り付けられます)")
        copy.clicked.connect(lambda: copy_params(copy, "パラメーターをコピー", req))
        bottom.addWidget(copy)
        bottom.addStretch(1)
        again = QPushButton("この設定で生成する...")
        again.setEnabled(has_source)
        again.setToolTip("今のファイルの本文で、同じ接続先・ワークフロー・長さでもう一度生成します (seed は変わります)")
        again.clicked.connect(self._again)
        close = QPushButton("閉じる")
        close.setDefault(True)
        close.clicked.connect(self.reject)
        bottom.addWidget(again)
        bottom.addWidget(close)
        layout.addLayout(bottom)

    def _again(self):
        self.regenerate = True
        self.accept()
