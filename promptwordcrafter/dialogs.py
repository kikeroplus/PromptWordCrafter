"""一括操作用のダイアログ群。"""

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from . import bulk_ops


class RenameDialog(QDialog):
    """選択中（または全件）のファイルを連番形式に一括リネームするダイアログ。"""

    def __init__(self, files: list[Path], parent=None):
        super().__init__(parent)
        self.setWindowTitle("ファイル名の一括変更（連番）")
        self.resize(480, 440)
        self.files = files

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"対象: {len(files)} 件（現在の一覧の並び順）"))

        form = QFormLayout()
        self.prefix_edit = QLineEdit()
        form.addRow("プレフィックス", self.prefix_edit)

        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 999999)
        self.start_spin.setValue(1)
        form.addRow("開始番号", self.start_spin)

        self.digits_spin = QSpinBox()
        self.digits_spin.setRange(1, 10)
        self.digits_spin.setValue(max(3, len(str(len(files) + 1))))
        form.addRow("桁数（0埋め）", self.digits_spin)
        layout.addLayout(form)

        layout.addWidget(QLabel("プレビュー（変更前 → 変更後）"))
        self.preview_list = QListWidget()
        layout.addWidget(self.preview_list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.prefix_edit.textChanged.connect(self.update_preview)
        self.start_spin.valueChanged.connect(self.update_preview)
        self.digits_spin.valueChanged.connect(self.update_preview)
        self.update_preview()

    def build_plan(self) -> list[tuple[Path, Path]]:
        return bulk_ops.plan_sequential_rename(
            self.files, self.prefix_edit.text(), self.start_spin.value(), self.digits_spin.value()
        )

    def update_preview(self):
        self.preview_list.clear()
        for old_path, new_path in self.build_plan():
            self.preview_list.addItem(f"{old_path.name}  →  {new_path.name}")


class AddTextDialog(QDialog):
    """ファイル先頭/末尾に文字列を追加するダイアログ。"""

    def __init__(self, current_file_name: str | None, folder_file_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文字列の追加")
        self.resize(420, 340)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("対象"))
        self.scope_current = QRadioButton(
            f"現在のファイルのみ（{current_file_name}）" if current_file_name else "現在のファイルのみ（未選択）"
        )
        self.scope_folder = QRadioButton(f"フォルダ内の全ファイル（{folder_file_count} 件・直下のみ）")
        self.scope_group = QButtonGroup(self)
        self.scope_group.addButton(self.scope_current)
        self.scope_group.addButton(self.scope_folder)
        if current_file_name is None:
            self.scope_current.setEnabled(False)
            self.scope_folder.setChecked(True)
        else:
            self.scope_current.setChecked(True)
        layout.addWidget(self.scope_current)
        layout.addWidget(self.scope_folder)

        layout.addWidget(QLabel("追加位置"))
        position_layout = QHBoxLayout()
        self.position_start = QRadioButton("先頭に追加")
        self.position_end = QRadioButton("末尾に追加")
        self.position_group = QButtonGroup(self)
        self.position_group.addButton(self.position_start)
        self.position_group.addButton(self.position_end)
        self.position_end.setChecked(True)
        position_layout.addWidget(self.position_start)
        position_layout.addWidget(self.position_end)
        layout.addLayout(position_layout)

        layout.addWidget(QLabel("追加する文字列"))
        self.text_input = QPlainTextEdit()
        layout.addWidget(self.text_input, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def scope(self) -> str:
        return "current" if self.scope_current.isChecked() else "folder"

    def position(self) -> str:
        return "start" if self.position_start.isChecked() else "end"

    def text(self) -> str:
        return self.text_input.toPlainText()


class NewFileDialog(QDialog):
    """新規テキストファイルの名前を入力するダイアログ。"""

    def __init__(self, default_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新規ファイル")
        self.resize(420, 120)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("ファイル名（最後のファイル名+1 を初期表示）"))
        self.name_edit = QLineEdit(default_name)
        self.name_edit.selectAll()
        layout.addWidget(self.name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def name(self) -> str:
        return self.name_edit.text().strip()


class RemoveTextDialog(QDialog):
    """ファイルから指定文字列を削除するダイアログ。"""

    def __init__(
        self,
        current_file_name: str | None,
        folder_file_count: int,
        initial_text: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("文字列の削除")
        self.resize(420, 220)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("対象"))
        self.scope_current = QRadioButton(
            f"現在のファイルのみ（{current_file_name}）" if current_file_name else "現在のファイルのみ（未選択）"
        )
        self.scope_folder = QRadioButton(f"フォルダ内の全ファイル（{folder_file_count} 件・直下のみ）")
        self.scope_group = QButtonGroup(self)
        self.scope_group.addButton(self.scope_current)
        self.scope_group.addButton(self.scope_folder)
        if current_file_name is None:
            self.scope_current.setEnabled(False)
            self.scope_folder.setChecked(True)
        else:
            self.scope_current.setChecked(True)
        layout.addWidget(self.scope_current)
        layout.addWidget(self.scope_folder)

        layout.addWidget(QLabel("削除する文字列"))
        self.text_edit = QLineEdit(initial_text)
        layout.addWidget(self.text_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def scope(self) -> str:
        return "current" if self.scope_current.isChecked() else "folder"

    def text(self) -> str:
        return self.text_edit.text()
