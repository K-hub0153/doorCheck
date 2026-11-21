"""按运行查看可读执行记录、连接部署过程及原始工具输出。"""
import json
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout

from ..domain.execution_log import format_record
from ..infrastructure.report import export_execution_log


class ExecutionLogDialog(QDialog):
    def __init__(self, repository, run_id, parent=None):
        super().__init__(parent)
        row = repository.get(run_id)
        if not row:
            raise ValueError('运行不存在')
        self.repository, self.run_id = repository, run_id
        self.directory = Path(row['output_dir'])
        self.last_seq = max(0, repository.last_seq(run_id) - 500)
        self.files = {}
        self.setWindowTitle('执行日志 · ' + row['sn'])
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(1000, 680)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.execution = self.viewer()
        self.execution.setPlainText('运行：%s\n时间为 UTC；每两秒刷新。窗口保留最近 5000 行，保存日志可获取完整记录。' % run_id)
        if self.last_seq:
            self.execution.appendPlainText('较早的记录已省略，可通过“保存完整执行日志”查看。')
        self.tabs.addTab(self.execution, '执行过程 / 4G 响应')
        self.controller = self.viewer()
        self.tabs.addTab(self.controller, '连接与部署')
        actions = QHBoxLayout()
        for title, callback in (('刷新', self.refresh), ('保存完整执行日志', self.save_log),
                                ('打开本次目录', self.open_directory), ('关闭', self.close)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    @staticmethod
    def viewer():
        widget = QPlainTextEdit()
        widget.setReadOnly(True)
        widget.setMaximumBlockCount(5000)
        return widget

    def refresh(self):
        try:
            row = self.repository.get(self.run_id)
            if not row:
                raise ValueError('运行不存在')
            status = json.loads(row['status'])
            self.status.setText('%s (%s) · %s · %s\n%s' % (
                row['sn'], row['ip'], row['state'], row['result'] or '未结束',
                status.get('error') or status.get('connection_error') or status.get('environment_message') or ''))
            records = self.repository.records(self.run_id, after_seq=self.last_seq, limit=500)
            if records:
                self.execution.appendPlainText(''.join(format_record(r) for r in records).rstrip())
                self.last_seq = records[-1]['seq']
            self.read_file(self.directory / 'controller.log', self.controller)
            for path in sorted((self.directory / 'device').glob('*.log')):
                if path.name == 'execution.log':
                    continue  # 同份事件已在第一个页签展示。
                if path not in self.files:
                    viewer = self.viewer()
                    self.files[path] = (viewer, None)
                    self.tabs.addTab(viewer, path.name)
                self.read_file(path, self.files[path][0])
        except Exception as exc:
            self.status.setText('读取日志失败：' + str(exc))

    def read_file(self, path, viewer):
        if not path.exists():
            viewer.setPlainText('等待日志生成；CPU/DDR 原始输出在设备结束并同步附件后可见。')
            return
        info = path.stat()
        signature = (info.st_mtime_ns, info.st_size)
        if self.files.get(path, (None, None))[1] == signature:
            return
        limit = 2**20
        with path.open('rb') as stream:
            stream.seek(max(0, info.st_size - limit))
            content = stream.read(limit).decode('utf-8', errors='replace')
        if info.st_size > limit:
            content = '仅显示文件末尾 1 MiB，完整内容请打开本次目录。\n' + content
        viewer.setPlainText(content or '日志文件为空（工具未输出内容）。')
        self.files[path] = (viewer, signature)

    def save_log(self):
        try:
            path = export_execution_log(self.repository, self.run_id)
            QMessageBox.information(self, '执行日志已保存', str(path))
        except Exception as exc:
            QMessageBox.warning(self, '保存失败', str(exc))

    def open_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory)))
