"""按运行查看可读执行记录、连接部署过程及原始工具输出。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout

from ..domain.execution_log import format_record
from ..infrastructure.local_logs import LocalLogs


class ExecutionLogDialog(QDialog):
    def __init__(self, repository, run_id, report_server, parent=None):
        super().__init__(parent)
        self.repository, self.run_id = repository, run_id
        self.local_logs = LocalLogs()
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
        self.execution.setPlainText('运行：%s\n时间为 UTC；每两秒刷新。优先显示本机详细日志；其他电脑运行显示云端摘要。窗口读取日志末尾 1 MiB，保留最近 5000 行；完整记录请打开本地详细日志目录。' % run_id)
        self.tabs.addTab(self.execution, '执行日志')
        self.controller = self.viewer()
        self.tabs.addTab(self.controller, '连接与部署')
        actions = QHBoxLayout()
        for title, callback in (('刷新', self.refresh_now), ('本地详细日志目录', self.open_local_directory),
                                ('查看云端日志摘要', self.open_full_log),
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
        directory = self.local_logs.directory(self.run_id, create=False)
        local = (directory / 'records.jsonl').is_file()
        try:
            row = self.repository.get(self.run_id)
        except Exception:
            # 数据库离线时，本机详细日志仍可查看。
            if not (directory / 'status.json').is_file():
                raise
            status = json.loads((directory / 'status.json').read_text(encoding='utf-8'))
            context = json.loads((directory / 'context.json').read_text(encoding='utf-8'))
            row = dict(context['device'], status=json.dumps(status), state=status['state'], result=status.get('result'))
        if not row:
            raise ValueError('运行不存在')
        if local:
            start_seq = last_seq or 0
            records = self.local_logs.tail(self.run_id, 'records.jsonl', start_seq)
            controller = self.local_logs.tail(self.run_id, 'controller.log')
            artifacts = []
            device_dir = directory / 'device'
            for path in sorted(device_dir.glob('*.log')):
                signature = str(path.stat().st_mtime_ns)
                if signatures.get(path.name) == signature:
                    continue
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 2**20))
                    tail = stream.read().decode('utf-8', errors='replace')
                artifacts.append((path.name, signature, tail))
            return row, start_seq, records, [], artifacts, controller
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
        return row, start_seq, records, logs, artifacts, None

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
            row, start_seq, records, logs, artifacts, local_controller = future.result()
            self.setWindowTitle('执行日志 · ' + row['sn'])
            if self.last_seq is None and start_seq:
                self.execution.appendPlainText('较早的记录已省略，可通过“查看云端日志摘要”查看。')
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
            if local_controller is not None:
                self.controller.setPlainText(local_controller)
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

    def open_local_directory(self):
        directory = self.local_logs.directory(self.run_id, create=False)
        if not directory.is_dir():
            self.status.setText('本机没有该次运行的详细日志，请在执行该任务的电脑查看。')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def open_report(self):
        QDesktopServices.openUrl(QUrl(self.report_server.url('run', self.run_id)))
