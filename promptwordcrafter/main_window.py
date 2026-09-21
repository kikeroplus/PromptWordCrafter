"""PromptWordCrafter のメインウィンドウ。

PromptManager プロジェクトの text_replace_tool.py (Text Preview Search Replace)
と同等の機能を PySide6 で再実装したもの。
"""

import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QDir, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFileSystemModel,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import bulk_ops
from . import settings as settings_module
from . import text_io
from .dialogs import AddTextDialog, NewFileDialog, RemoveTextDialog, RenameDialog
from .file_list import FileListWidget
from .highlighter import TagHighlighter
from .widgets import FolderTreeView, NewlineAwareLineEdit

FILE_MATCH_BACKGROUND = "#edf5ef"
MATCH_BACKGROUND = "#fff2a8"
CURRENT_MATCH_BACKGROUND = "#ffcf66"

DEFAULT_WIDTH = 1100
DEFAULT_HEIGHT = 720
MIN_WIDTH = 860
MIN_HEIGHT = 520

SCAN_DEBOUNCE_MS = 250


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PromptWordCrafter")

        self.settings_data = settings_module.load_settings()
        self.files: list[Path] = []
        self.current_path: Optional[Path] = None
        self.current_encoding = "utf-8"
        self._restoring_selection = False
        self._tree_root = None
        self._tree_selection_guard = False
        self._suppress_tree_scroll = False
        self._scan_document = QTextDocument(self)

        self.scan_timer = QTimer(self)
        self.scan_timer.setSingleShot(True)
        self.scan_timer.timeout.connect(self.scan_file_list_matches)

        self._build_ui()
        self._restore_window_geometry()
        self._connect_signals()

        self.load_folder(confirm=False)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 8)
        root_layout.setSpacing(6)

        # フォルダ選択バー
        folder_bar = QHBoxLayout()
        folder_bar.addWidget(QLabel("フォルダ"))
        self.folder_edit = QLineEdit(self.settings_data.get("folder", str(Path.cwd())))
        folder_bar.addWidget(self.folder_edit, 1)
        self.browse_button = QPushButton("選択")
        folder_bar.addWidget(self.browse_button)
        self.refresh_button = QPushButton("更新")
        folder_bar.addWidget(self.refresh_button)
        root_layout.addLayout(folder_bar)

        # 検索・置換コントロールバー
        control_bar = QHBoxLayout()

        search_group = QGroupBox("検索")
        search_layout = QHBoxLayout(search_group)
        self.search_edit = NewlineAwareLineEdit(self.settings_data.get("search", ""))
        search_layout.addWidget(self.search_edit, 1)
        self.search_button = QPushButton("検索")
        search_layout.addWidget(self.search_button)
        self.clear_button = QPushButton("クリア")
        search_layout.addWidget(self.clear_button)
        self.count_label = QLabel("出現回数: 0")
        search_layout.addWidget(self.count_label)
        control_bar.addWidget(search_group, 1)

        replace_group = QGroupBox("置換")
        replace_layout = QHBoxLayout(replace_group)
        self.replace_edit = NewlineAwareLineEdit(self.settings_data.get("replace", ""))
        replace_layout.addWidget(self.replace_edit, 1)
        self.replace_clear_button = QPushButton("クリア")
        replace_layout.addWidget(self.replace_clear_button)
        self.replace_button = QPushButton("置換")
        replace_layout.addWidget(self.replace_button)
        self.folder_replace_button = QPushButton("フォルダ一括置換")
        replace_layout.addWidget(self.folder_replace_button)
        control_bar.addWidget(replace_group, 1)

        file_buttons = QVBoxLayout()
        self.save_button = QPushButton("保存")
        file_buttons.addWidget(self.save_button)
        self.new_file_button = QPushButton("新規ファイル")
        file_buttons.addWidget(self.new_file_button)
        control_bar.addLayout(file_buttons)
        root_layout.addLayout(control_bar)

        # 一括操作グループ
        bulk_group = QGroupBox("一括操作")
        bulk_bar = QHBoxLayout(bulk_group)
        self.rename_button = QPushButton("一括リネーム")
        bulk_bar.addWidget(self.rename_button)
        self.add_text_button = QPushButton("文字列追加")
        bulk_bar.addWidget(self.add_text_button)
        self.remove_text_button = QPushButton("文字列削除")
        bulk_bar.addWidget(self.remove_text_button)
        self.delete_bak_button = QPushButton(".bakファイル削除")
        bulk_bar.addWidget(self.delete_bak_button)
        self.reformat_button = QPushButton("文字列成型")
        bulk_bar.addWidget(self.reformat_button)
        bulk_bar.addStretch(1)
        root_layout.addWidget(bulk_group)

        # フォルダツリー + ファイル一覧 + プレビュー編集
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.folder_tree_model = QFileSystemModel(self)
        self.folder_tree_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot)
        self.folder_tree = FolderTreeView()
        self.folder_tree.setModel(self.folder_tree_model)
        self.folder_tree.setHeaderHidden(True)
        for column in range(1, 4):
            self.folder_tree.hideColumn(column)
        self.folder_tree.header().setStretchLastSection(False)
        # ResizeToContents だと、表示中の行が変わるたびに列幅（＝横スクロールの
        # 基準）が再計算されてしまい、選び直すたびに横スクロール位置がずれる
        # 原因になる。列幅を固定し、選択操作では変化しないようにする。
        self.folder_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.folder_tree.header().resizeSection(0, 1200)
        self.folder_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.folder_tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        splitter.addWidget(self.folder_tree)

        self.file_list = FileListWidget()
        splitter.addWidget(self.file_list)

        self.text_edit = QTextEdit()
        self.text_edit.setAcceptRichText(False)
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        splitter.addWidget(self.text_edit)
        self.tag_highlighter = TagHighlighter(self.text_edit.document())

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 4)
        root_layout.addWidget(splitter, 1)

        self.statusBar().showMessage("フォルダを選択してください。")

    def _restore_window_geometry(self):
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        width = self.settings_data.get("window_width", DEFAULT_WIDTH)
        height = self.settings_data.get("window_height", DEFAULT_HEIGHT)
        self.resize(width, height)
        x = self.settings_data.get("window_x")
        y = self.settings_data.get("window_y")
        if x is not None and y is not None:
            self.move(x, y)

    def _connect_signals(self):
        self.folder_edit.returnPressed.connect(lambda: self.load_folder())
        self.browse_button.clicked.connect(self.choose_folder)
        self.refresh_button.clicked.connect(lambda: self.load_folder())

        self.search_edit.textChanged.connect(self.on_search_text_changed)
        self.search_edit.returnPressed.connect(self.search_text_and_focus)
        self.search_button.clicked.connect(self.search_text)
        self.clear_button.clicked.connect(self.clear_search_text)

        self.replace_clear_button.clicked.connect(self.clear_replace_text)
        self.replace_button.clicked.connect(self.replace_text)
        self.folder_replace_button.clicked.connect(self.replace_text_in_folder)
        self.save_button.clicked.connect(lambda: self.save_current_file())

        self.file_list.currentItemChanged.connect(self.on_current_item_changed)
        self.text_edit.selectionChanged.connect(self.copy_selection_to_search)

        self.new_file_button.clicked.connect(self.create_new_file)
        self.rename_button.clicked.connect(self.open_rename_dialog)
        self.add_text_button.clicked.connect(self.open_add_text_dialog)
        self.remove_text_button.clicked.connect(self.open_remove_text_dialog)
        self.delete_bak_button.clicked.connect(self.remove_bak_files)
        self.reformat_button.clicked.connect(self.reformat_current_file)

        self.folder_tree.selectionModel().currentChanged.connect(self.on_tree_current_changed)

        save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        save_shortcut.activated.connect(lambda: self.save_current_file())

    # ------------------------------------------------------------------
    # 設定
    # ------------------------------------------------------------------
    def save_settings(self):
        geo = self.geometry()
        data = {
            "folder": self.folder_edit.text(),
            "search": self.search_edit.text(),
            "replace": self.replace_edit.text(),
            "window_x": geo.x(),
            "window_y": geo.y(),
            "window_width": geo.width(),
            "window_height": geo.height(),
        }
        settings_module.save_settings(data)

    def closeEvent(self, event):
        if not self.confirm_save_changes():
            event.ignore()
            return
        self.save_settings()
        event.accept()

    # ------------------------------------------------------------------
    # フォルダ・ファイル一覧
    # ------------------------------------------------------------------
    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "フォルダを選択", self.folder_edit.text() or str(Path.cwd())
        )
        if not folder:
            return
        if not self.confirm_save_changes():
            return
        self.folder_edit.setText(folder)
        self.load_folder(confirm=False)

    def load_folder(self, confirm: bool = True):
        if confirm and not self.confirm_save_changes():
            return

        folder = Path(self.folder_edit.text()).expanduser()
        self.files = []
        self._restoring_selection = True
        self.file_list.clear()
        self._restoring_selection = False
        self.text_edit.clear()
        self.text_edit.document().setModified(False)
        self.current_path = None
        self.count_label.setText("出現回数: 0")

        if not folder.is_dir():
            self.statusBar().showMessage("指定されたフォルダが見つかりません。")
            return

        self._sync_folder_tree(folder)

        row_height = self.file_list.fontMetrics().height() + 2
        for path in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
            if text_io.is_text_file(path):
                self.files.append(path)
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setSizeHint(QSize(0, row_height))
                self.file_list.addItem(item)

        self.statusBar().showMessage(f"{len(self.files)} 件のテキストファイルを表示しています。")
        if self.files:
            self.file_list.setCurrentRow(0)
            self.file_list.setFocus()
        self.scan_file_list_matches()
        self.save_settings()

    # ------------------------------------------------------------------
    # フォルダツリー
    # ------------------------------------------------------------------
    def _sync_folder_tree(self, folder: Path):
        anchor = folder.anchor or str(folder)
        if anchor != self._tree_root:
            self.folder_tree_model.setRootPath(anchor)
            self.folder_tree.setRootIndex(self.folder_tree_model.index(anchor))
            self._tree_root = anchor

        index = self.folder_tree_model.index(str(folder))
        scroll_into_view = not self._suppress_tree_scroll
        self._suppress_tree_scroll = False

        # QTreeView は setCurrentIndex() の内部処理だけで、選択項目を
        # 見せるために横スクロール位置を勝手に動かしてしまう。
        # ツリー自身のクリックで遷移した場合はユーザーが調整した
        # 横スクロール位置を復元し、それ以外（フォルダ欄への直接入力や
        # 「選択」ダイアログ経由）では選択フォルダが見えるよう
        # scrollTo で改めて自動スクロールする。
        h_scroll = self.folder_tree.horizontalScrollBar()
        preserved_scroll = h_scroll.value()

        self._tree_selection_guard = True
        try:
            self.folder_tree.setCurrentIndex(index)
            if scroll_into_view:
                self.folder_tree.expand(index)
                self.folder_tree.scrollTo(index)
            else:
                h_scroll.setValue(preserved_scroll)
        finally:
            self._tree_selection_guard = False

    def on_tree_current_changed(self, current, _previous):
        if self._tree_selection_guard or not current.isValid():
            return
        path = Path(self.folder_tree_model.filePath(current))
        if not path.is_dir() or path == Path(self.folder_edit.text()).expanduser():
            return
        if not self.confirm_save_changes():
            self._sync_folder_tree(Path(self.folder_edit.text()).expanduser())
            return
        self.folder_edit.setText(str(path))
        self._suppress_tree_scroll = True
        self.load_folder(confirm=False)

    # ------------------------------------------------------------------
    # ファイル選択・表示
    # ------------------------------------------------------------------
    def on_current_item_changed(self, current: Optional[QListWidgetItem], _previous):
        if self._restoring_selection or current is None:
            return
        self.open_file(self.file_list.row(current))

    def select_current_file(self):
        if self.current_path is None or self.current_path not in self.files:
            return
        index = self.files.index(self.current_path)
        self._restoring_selection = True
        try:
            self.file_list.setCurrentRow(index)
        finally:
            self._restoring_selection = False

    def open_file(self, index: int):
        if index < 0 or index >= len(self.files):
            return
        if not self.confirm_save_changes():
            self.select_current_file()
            return

        path = self.files[index]
        try:
            text, encoding = text_io.read_text(path)
        except OSError as exc:
            QMessageBox.critical(self, "読み込みエラー", f"{path.name} を読み込めませんでした。\n\n{exc}")
            self.select_current_file()
            return

        self.current_path = path
        self.current_encoding = encoding
        self.text_edit.setPlainText(text)
        self.text_edit.document().clearUndoRedoStacks()
        self.text_edit.document().setModified(False)
        self.clear_matches()
        self.statusBar().showMessage(f"{path.name} を表示中 / 文字コード: {encoding}")
        if self.search_edit.text():
            self.search_text()

    # ------------------------------------------------------------------
    # 検索
    # ------------------------------------------------------------------
    def on_search_text_changed(self, _text: str):
        self.scan_timer.start(SCAN_DEBOUNCE_MS)

    def clear_matches(self):
        self.text_edit.setExtraSelections([])
        self.count_label.setText("出現回数: 0")

    def clear_search_text(self):
        self.search_edit.clear()
        self.clear_matches()
        self.scan_file_list_matches()
        self.statusBar().showMessage("検索文字列をクリアしました。")
        self.search_edit.setFocus()

    def editor_search_text(self) -> str:
        """編集欄（toPlainText）と同じ正規化を検索語にも適用する（NBSP→空白）。"""
        return self.search_edit.text().replace(" ", " ")

    def clear_replace_text(self):
        self.replace_edit.clear()
        self.statusBar().showMessage("置換文字列をクリアしました。")
        self.replace_edit.setFocus()

    def search_text(self):
        needle = self.editor_search_text()
        self.clear_matches()
        if not needle:
            self.file_list.setFocus()
            self.statusBar().showMessage("検索文字列を入力してください。")
            return

        document = self.text_edit.document()
        selections = []
        first_cursor = None
        count = 0
        # QTextDocument.find は行（段落）をまたぐ検索ができないため、
        # プレーンテキスト上で検索して文字位置をカーソルに変換する
        # （toPlainText の各文字はカーソル位置と1対1に対応する）
        plain_text = self.text_edit.toPlainText()
        position = 0
        while True:
            position = plain_text.find(needle, position)
            if position < 0:
                break
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            cursor.setPosition(position + len(needle), QTextCursor.MoveMode.KeepAnchor)
            position += len(needle)

            match_selection = QTextEdit.ExtraSelection()
            match_selection.cursor = QTextCursor(cursor)
            match_format = QTextCharFormat()
            match_format.setBackground(QColor(MATCH_BACKGROUND))
            match_selection.format = match_format
            selections.append(match_selection)
            if first_cursor is None:
                first_cursor = QTextCursor(cursor)
            count += 1

        if first_cursor is not None:
            current_selection = QTextEdit.ExtraSelection()
            current_selection.cursor = first_cursor
            current_format = QTextCharFormat()
            current_format.setBackground(QColor(CURRENT_MATCH_BACKGROUND))
            current_selection.format = current_format
            selections.append(current_selection)
            self.text_edit.setTextCursor(first_cursor)
            self.text_edit.ensureCursorVisible()

        self.text_edit.setExtraSelections(selections)
        self.count_label.setText(f"出現回数: {count}")
        self.statusBar().showMessage(f"検索完了: {count} 件見つかりました。")
        self.file_list.setFocus()

    def search_text_and_focus(self):
        self.search_text()
        self.file_list.setFocus()

    def scan_file_list_matches(self):
        needle = self.editor_search_text()
        for i in range(self.file_list.count()):
            self.file_list.item(i).setBackground(QColor(Qt.GlobalColor.white))

        if not needle:
            return

        for i, path in enumerate(self.files):
            try:
                if path == self.current_path:
                    text = self.text_edit.toPlainText()
                else:
                    text, _encoding = text_io.read_text(path)
                    # 編集欄と同じ変換（NBSP→空白、U+2028/単独CR→改行など）を通して比較する
                    self._scan_document.setPlainText(text)
                    text = self._scan_document.toPlainText()
            except OSError:
                continue
            if needle in text:
                self.file_list.item(i).setBackground(QColor(FILE_MATCH_BACKGROUND))

    def copy_selection_to_search(self):
        cursor = self.text_edit.textCursor()
        selected = cursor.selectedText().replace(" ", "\n")
        if not selected:
            return
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier:
            self.replace_edit.setText(selected)
        else:
            self.search_edit.setText(selected)

    # ------------------------------------------------------------------
    # 置換・保存
    # ------------------------------------------------------------------
    def confirm_save_changes(self) -> bool:
        if not self.text_edit.document().isModified():
            return True

        response = QMessageBox.question(
            self,
            "未保存の変更",
            "編集内容が保存されていません。保存しますか？",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if response == QMessageBox.StandardButton.Cancel:
            return False
        if response == QMessageBox.StandardButton.Yes:
            return self.save_current_file(refocus=False)
        return True

    def save_current_file(self, refocus: bool = True) -> bool:
        if self.current_path is None:
            QMessageBox.information(self, "保存", "保存するファイルを選択してください。")
            return False

        text = self.text_edit.toPlainText()
        backup_path = self.current_path.with_suffix(self.current_path.suffix + ".bak")
        try:
            if not backup_path.exists():
                shutil.copy2(self.current_path, backup_path)
            self.current_path.write_text(text, encoding=self.current_encoding, newline="")
        except OSError as exc:
            QMessageBox.critical(self, "保存エラー", f"保存できませんでした。\n\n{exc}")
            return False

        self.text_edit.document().setModified(False)
        self.statusBar().showMessage(f"{self.current_path.name} を保存しました。バックアップ: {backup_path.name}")
        self.scan_file_list_matches()
        if refocus:
            self.text_edit.setFocus()
        return True

    def replace_text(self):
        if self.current_path is None:
            QMessageBox.information(self, "置換", "置換するファイルを選択してください。")
            return

        needle = self.editor_search_text()
        replacement = self.replace_edit.text()
        if not needle:
            QMessageBox.information(self, "置換", "検索文字列を入力してください。")
            return

        old_text = self.text_edit.toPlainText()
        count = old_text.count(needle)
        if count == 0:
            self.count_label.setText("出現回数: 0")
            self.statusBar().showMessage("置換対象は見つかりませんでした。")
            return

        # 編集欄を直接編集する（Ctrl+Z で元に戻せる。ファイルへは「保存」で反映）。
        # 後ろの一致から置換して、前の一致の位置がずれないようにする
        positions = []
        position = 0
        while True:
            position = old_text.find(needle, position)
            if position < 0:
                break
            positions.append(position)
            position += len(needle)

        cursor = QTextCursor(self.text_edit.document())
        cursor.beginEditBlock()
        for position in reversed(positions):
            cursor.setPosition(position)
            cursor.setPosition(position + len(needle), QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(replacement)
        cursor.endEditBlock()

        self.text_edit.document().setModified(True)
        self.search_text()
        self.statusBar().showMessage(
            f"置換完了: {count} 件を置換しました（Ctrl+Zで元に戻せます）。内容を確認して保存してください。"
        )
        self.scan_file_list_matches()
        self.file_list.setFocus()

    def replace_text_in_folder(self):
        needle = self.search_edit.text()
        if not needle:
            QMessageBox.information(self, "フォルダ一括置換", "検索文字列を入力してください。")
            return
        if not self.files:
            QMessageBox.information(self, "フォルダ一括置換", "対象のファイルがありません。")
            return
        if not self.confirm_save_changes():
            return

        replacement = self.replace_edit.text()
        total = 0
        changed_files = 0
        errors = []
        for path in list(self.files):
            try:
                count = bulk_ops.replace_text_in_file(path, needle, replacement)
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if count:
                total += count
                changed_files += 1

        message = f"フォルダ一括置換: {changed_files} ファイル / 計 {total} 件を置換しました。"
        if errors:
            message += f" 失敗: {len(errors)} 件"
        self.load_folder(confirm=False)
        self.statusBar().showMessage(message)

    # ------------------------------------------------------------------
    # 一括操作
    # ------------------------------------------------------------------
    def create_new_file(self):
        folder = Path(self.folder_edit.text()).expanduser()
        if not folder.is_dir():
            QMessageBox.information(self, "新規ファイル", "有効なフォルダを選択してください。")
            return

        default_name = bulk_ops.suggest_new_file_name([path.name for path in self.files])
        dialog = NewFileDialog(default_name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        name = dialog.name()
        if not name or any(ch in name for ch in '\\/:*?"<>|'):
            QMessageBox.information(self, "新規ファイル", "ファイル名が正しくありません。")
            return
        if Path(name).suffix.lower() not in text_io.TEXT_EXTENSIONS:
            name += ".txt"
        if not self.confirm_save_changes():
            return

        try:
            new_path = bulk_ops.create_empty_file(folder, name)
        except FileExistsError:
            QMessageBox.critical(self, "新規ファイル", f"{name} は既に存在します。")
            return
        except OSError as exc:
            QMessageBox.critical(self, "新規ファイル", f"ファイルを作成できませんでした。\n\n{exc}")
            return

        self.load_folder(confirm=False)
        if new_path in self.files:
            self.file_list.setCurrentRow(self.files.index(new_path))
        self.text_edit.setFocus()
        self.statusBar().showMessage(f"{new_path.name} を作成しました。")

    def open_rename_dialog(self):
        selected_items = self.file_list.selectedItems()
        if len(selected_items) >= 2:
            indices = sorted(self.file_list.row(item) for item in selected_items)
            targets = [self.files[i] for i in indices]
        else:
            targets = list(self.files)

        if not targets:
            QMessageBox.information(self, "一括リネーム", "対象のファイルがありません。")
            return

        dialog = RenameDialog(targets, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        plan = dialog.build_plan()
        renamed_names = {old.name for old, _new in plan}
        other_names = {path.name for path in self.files if path.name not in renamed_names}
        conflicts = [new for _old, new in plan if new.name in other_names]
        if conflicts:
            QMessageBox.critical(
                self, "一括リネーム", "リネーム後のファイル名が既存のファイルと重複しています。"
            )
            return

        if not self.confirm_save_changes():
            return

        try:
            bulk_ops.apply_renames(plan)
        except OSError as exc:
            QMessageBox.critical(self, "一括リネーム", f"リネーム中にエラーが発生しました。\n\n{exc}")

        renamed_count = len(plan)
        self.load_folder(confirm=False)
        self.statusBar().showMessage(f"{renamed_count} 件のファイル名を変更しました。")

    def open_add_text_dialog(self):
        current_name = self.current_path.name if self.current_path else None
        dialog = AddTextDialog(current_name, len(self.files), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        text = dialog.text()
        if not text:
            QMessageBox.information(self, "文字列の追加", "追加する文字列を入力してください。")
            return
        position = dialog.position()

        if dialog.scope() == "current":
            if self.current_path is None:
                QMessageBox.information(self, "文字列の追加", "対象のファイルがありません。")
                return
            if not self.confirm_save_changes():
                return
            try:
                bulk_ops.add_text_to_file(self.current_path, text, position)
            except OSError as exc:
                QMessageBox.critical(self, "文字列の追加", f"書き込みに失敗しました。\n\n{exc}")
                return
            file_name = self.current_path.name
            self.open_file(self.files.index(self.current_path))
            self.statusBar().showMessage(f"{file_name} に文字列を追加しました。")
        else:
            if not self.files:
                QMessageBox.information(self, "文字列の追加", "対象のファイルがありません。")
                return
            if not self.confirm_save_changes():
                return
            errors = []
            for path in list(self.files):
                try:
                    bulk_ops.add_text_to_file(path, text, position)
                except OSError as exc:
                    errors.append(f"{path.name}: {exc}")
            message = f"{len(self.files) - len(errors)} 件のファイルに文字列を追加しました。"
            if errors:
                message += f" 失敗: {len(errors)} 件"
            self.load_folder(confirm=False)
            self.statusBar().showMessage(message)

    def open_remove_text_dialog(self):
        current_name = self.current_path.name if self.current_path else None
        dialog = RemoveTextDialog(current_name, len(self.files), self.search_edit.text(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        needle = dialog.text()
        if not needle:
            QMessageBox.information(self, "文字列の削除", "削除する文字列を入力してください。")
            return

        if dialog.scope() == "current":
            if self.current_path is None:
                QMessageBox.information(self, "文字列の削除", "対象のファイルがありません。")
                return
            if not self.confirm_save_changes():
                return
            try:
                count = bulk_ops.remove_text_from_file(self.current_path, needle)
            except OSError as exc:
                QMessageBox.critical(self, "文字列の削除", f"書き込みに失敗しました。\n\n{exc}")
                return
            file_name = self.current_path.name
            self.open_file(self.files.index(self.current_path))
            self.statusBar().showMessage(f"{file_name} から {count} 件削除しました。")
        else:
            if not self.files:
                QMessageBox.information(self, "文字列の削除", "対象のファイルがありません。")
                return
            if not self.confirm_save_changes():
                return
            total = 0
            errors = []
            for path in list(self.files):
                try:
                    total += bulk_ops.remove_text_from_file(path, needle)
                except OSError as exc:
                    errors.append(f"{path.name}: {exc}")
            message = f"フォルダ内で {total} 件削除しました。"
            if errors:
                message += f" 失敗: {len(errors)} 件"
            self.load_folder(confirm=False)
            self.statusBar().showMessage(message)

    def remove_bak_files(self):
        folder = Path(self.folder_edit.text()).expanduser()
        if not folder.is_dir():
            QMessageBox.information(self, ".bakファイル削除", "有効なフォルダを選択してください。")
            return

        bak_files = sorted(folder.glob("*.bak"))
        if not bak_files:
            QMessageBox.information(self, ".bakファイル削除", ".bakファイルは見つかりませんでした。")
            return

        response = QMessageBox.question(
            self,
            ".bakファイル削除",
            f"{len(bak_files)} 件の .bak ファイルを削除します。よろしいですか？\n（このフォルダの直下のみが対象です）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return

        count = bulk_ops.delete_bak_files(folder)
        self.statusBar().showMessage(f"{count} 件の .bak ファイルを削除しました。")

    def reformat_current_file(self):
        if self.current_path is None:
            QMessageBox.information(self, "文字列成型", "対象のファイルを選択してください。")
            return

        text = self.text_edit.toPlainText()
        new_text = bulk_ops.reformat_sentences(text)
        if new_text == text:
            self.statusBar().showMessage("成型の必要な箇所はありませんでした。")
            return

        cursor = self.text_edit.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(new_text)
        cursor.endEditBlock()

        self.text_edit.document().setModified(True)
        self.statusBar().showMessage("文字列を成型しました（Ctrl+Zで元に戻せます）。内容を確認して保存してください。")
