"""仅在电池阶段等待人工操作时展示的非阻塞确认窗口。"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)


class PowerConfirmationDialog(QDialog):
    confirmed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('电池老化 · 电源确认')
        self.resize(580, 360)
        self.setModal(False)
        self.entries = {}
        layout = QVBoxLayout(self)
        hint = QLabel('请确认本批全部设备的电源已按提示就绪，整批只需确认一次。同批稍晚准备完成的设备自动沿用本次确认；后续阶段按提示切换电源，无需再次确认。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.rows = QVBoxLayout(self.content)
        self.rows.addStretch()
        scroll.setWidget(self.content)
        layout.addWidget(scroll, 1)
        self.confirm_button = QPushButton('本批全部设备电源已就绪')
        self.confirm_button.setProperty('role', 'confirm')
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.clicked.connect(self.confirmed)
        layout.addWidget(self.confirm_button)
        later = QPushButton('稍后处理')
        later.setProperty('role', 'secondary')
        later.clicked.connect(self.reject)
        layout.addWidget(later)

    def set_entries(self, entries):
        if entries == self.entries:
            return
        self.entries = dict(entries)
        self.confirm_button.setEnabled(bool(entries))
        while self.rows.count() > 1:
            widget = self.rows.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        for key, device in entries.items():
            row = QWidget()
            layout = QHBoxLayout(row)
            phase = key[1]
            operation = '充电：接入适配器' if phase == 'charge' else '放电：拔除适配器并接好放电负载'
            label = QLabel('%s (%s)\n%s' % (device.get('sn', '待读取'), device['ip'], operation))
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label, 1)
            self.rows.insertWidget(self.rows.count() - 1, row)
