"""保留原始行，按关键词筛选；刷新和追加日志时沿用当前条件。"""
from collections import deque

from PyQt5.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget


class LogView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.lines = deque(maxlen=5000)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索日志关键词（不区分大小写）')
        self.search.setClearButtonEnabled(True)
        self.count = QLabel()
        row.addWidget(self.search, 1)
        row.addWidget(self.count)
        layout.addLayout(row)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(5000)
        layout.addWidget(self.text)
        self.search.textChanged.connect(self.refresh_filter)

    def setPlainText(self, text):
        self.lines.clear()
        self.lines.extend(text.splitlines())
        self.refresh_filter()

    def appendPlainText(self, text):
        self.lines.extend(text.splitlines())
        self.refresh_filter()

    def refresh_filter(self):
        keyword = self.search.text().strip().casefold()
        lines = [line for line in self.lines if not keyword or keyword in line.casefold()]
        scrollbar = self.text.verticalScrollBar()
        position, following = scrollbar.value(), scrollbar.value() == scrollbar.maximum()
        self.text.setPlainText('\n'.join(lines))
        scrollbar.setValue(scrollbar.maximum() if following else position)
        self.count.setText('%s / %s 行' % (len(lines), len(self.lines)))
