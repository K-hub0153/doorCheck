from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QLabel,
                             QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from ..application.manual_devices import parse_sns, read_excel


class ManualDeviceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('手动添加主控 · VSM')
        self.resize(620, 380)
        self.sns = []
        layout = QVBoxLayout(self)
        hint = QLabel('粘贴 SN，多个 SN 用英文逗号或换行分隔。\n'
                      'Excel 支持 .xlsx / .xlsm，读取 SN 列（无表头时读取第一列），自动去重。\n'
                      '添加前逐台验证 VSM 访问、真实 SN 和 root 权限。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText('wdse-xxxx,hisi-xxxx,orangepi-xxxx')
        layout.addWidget(self.input)
        import_button = QPushButton('导入 Excel')
        import_button.clicked.connect(self.import_excel)
        layout.addWidget(import_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('验证并添加')
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def import_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入 SN', '', 'Excel 工作簿 (*.xlsx *.xlsm)')
        if not path:
            return
        try:
            imported = read_excel(path)
            current = self.input.toPlainText().strip()
            combined = parse_sns(current + '\n' + '\n'.join(imported))
            self.input.setPlainText('\n'.join(combined))
        except Exception as exc:
            QMessageBox.warning(self, 'Excel 导入失败', str(exc))

    def validate(self):
        try:
            self.sns = parse_sns(self.input.toPlainText())
        except ValueError as exc:
            QMessageBox.warning(self, 'SN 输入有误', str(exc))
            return
        self.accept()
