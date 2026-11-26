"""按运行查看可读执行记录、连接部署过程及原始工具输出。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout

from ..domain.execution_log import format_record


class ExecutionLogDialog(QDialog):
    def __init__(self, repository, run_id, report_server, parent=None):
        super().__init__(parent)
        self.repository, self.run_id = repository, run_id
        self.report_server = report_server
        self.controller_id = 0
        self.last_seq = None
        self.files = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aging-log-view")
        self.pending = None
        self.next_refresh = 0
        self.setWindowTitle('执行日志 · ' + run_id)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(1000, 680)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setText('正在从 MySQL 读取日志…')
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.execution = self.viewer()
        self.execution.setPlainText('运行：%s\n时间为 UTC；每两秒刷新。窗口保留最近 5000 行，完整日志可从数据库查看。' % run_id)
        self.tabs.addTab(self.execution, '执行过程 / 4G 响应')
        self.controller = self.viewer()
        self.tabs.addTab(self.controller, '连接与部署')
        actions = QHBoxLayout()
        for title, callback in (('刷新', self.refresh_now), ('查看完整执行日志', self.open_full_log),
                                ('查看本次报告', self.open_report), ('关闭', self.close)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(200)
        self.finished.connect(self.stop_refresh)
        self.refresh()

    @staticmethod
    def viewer():
        widget = QPlainTextEdit()
        widget.setReadOnly(True)
        widget.setMaximumBlockCount(5000)
        return widget

    def fetch(self, last_seq, controller_id, signatures):
        row = self.repository.get(self.run_id)
        if not row:
            raise ValueError('运行不存在')
        start_seq = max(0, self.repository.last_seq(self.run_id) - 500) if last_seq is None else last_seq
        records = self.repository.records(self.run_id, after_seq=start_seq, limit=500)
        logs = self.repository.controller_logs(self.run_id, controller_id)
        artifacts = []
        for artifact in self.repository.artifacts(self.run_id):
            name, signature = artifact['name'], artifact['sha256']
            if name.endswith('.log') and name != 'execution.log' and signatures.get(name) != signature:
                content = self.repository.artifact(self.run_id, name)
                tail = content[-2**20:].decode('utf-8', errors='replace')
                if len(content) > 2**20:
                    tail = '仅显示末尾 1 MiB，完整附件可在报告中下载。\n' + tail
                artifacts.append((name, signature, tail))
        return row, start_seq, records, logs, artifacts

    def stop_refresh(self):
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def refresh_now(self):
        self.next_refresh = 0
        self.refresh()

    def refresh(self):
        if self.pending is None:
            if time.monotonic() < self.next_refresh:
                return
            signatures = {name: signature for name, (_, signature) in self.files.items()}
            self.pending = self.pool.submit(self.fetch, self.last_seq, self.controller_id, signatures)
            return
        if not self.pending.done():
            return
        future, self.pending = self.pending, None
        self.next_refresh = time.monotonic() + 2
        try:
            row, start_seq, records, logs, artifacts = future.result()
            self.setWindowTitle('执行日志 · ' + row['sn'])
            if self.last_seq is None and start_seq:
                self.execution.appendPlainText('较早的记录已省略，可通过“查看完整执行日志”查看。')
            self.last_seq = start_seq
            status = json.loads(row['status'])
            self.status.setText('%s (%s) · %s · %s\n%s' % (
                row['sn'], row['ip'], row['state'], row['result'] or '未结束',
                status.get('error') or status.get('connection_error') or status.get('environment_message') or ''))
            if records:
                self.execution.appendPlainText(''.join(format_record(r) for r in records).rstrip())
                self.last_seq = records[-1]['seq']
            if logs:
                self.controller.appendPlainText('\n'.join('%s %s' % (r['created_at'], r['message']) for r in logs))
                self.controller_id = logs[-1]['id']
            for name, signature, tail in artifacts:
                if name not in self.files:
                    viewer = self.viewer()
                    self.files[name] = (viewer, None)
                    self.tabs.addTab(viewer, name)
                viewer, _ = self.files[name]
                viewer.setPlainText(tail or '工具未输出内容。')
                self.files[name] = (viewer, signature)
        except Exception as exc:
            self.status.setText('读取日志失败：' + str(exc))

    def open_full_log(self):
        QDesktopServices.openUrl(QUrl(self.report_server.url('run', self.run_id, 'execution.log')))

    def open_report(self):
        QDesktopServices.openUrl(QUrl(self.report_server.url('run', self.run_id)))
