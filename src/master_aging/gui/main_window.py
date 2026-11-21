from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ipaddress
import json
import queue

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QColor, QDesktopServices
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                             QInputDialog, QLabel, QMainWindow, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from ..adapters.discovery import Discovery
from ..adapters.ssh_transport import SSHTransport
from ..application.run_service import RunService
from ..domain.models import ITEMS
from ..domain.policies import planned_seconds
from ..infrastructure.config import load_config, runtime_dir
from ..infrastructure.repository import Repository
from ..infrastructure.report import export_report
from .plan_dialog import PlanDialog
from .metrics_panel import MetricsPanel
from .device_list_panel import DeviceListPanel
from .discovery_dialogs import DeviceSelectionDialog, InterfaceDialog
from .styles import STYLE, STATES, status_color


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('主控设备老化工具')
        self.resize(1380, 900)
        self.setMinimumSize(1080, 720)
        self.setStyleSheet(STYLE)
        self.repository = Repository()
        self.service = RunService(self.repository)
        self.plan = load_config()
        self.plan_valid = False
        self.devices, self.run_devices, self.statuses = {}, {}, {}
        self.jobs, self.work_pool = [], ThreadPoolExecutor(max_workers=4, thread_name_prefix='aging-discovery')
        self.probing, self.job_messages = set(), queue.Queue()
        self.scan_progress = queue.Queue(maxsize=1)
        self.scanning = False
        self.closing = False
        self.detached = False
        self._build_ui()
        self.discovery = Discovery()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(400)
        self.load_history()
        unfinished = self.repository.runs(unfinished=True)
        self.log('本地目录：' + str(runtime_dir()))
        if unfinished:
            self.log('发现 %s 条未结束运行，可在历史记录中选择“恢复补采”；恢复不会重复启动负载。' % len(unfinished))

    def button(self, text, action, layout, role=None):
        button = QPushButton(text)
        if role:
            button.setProperty('role', role)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def header(self, text):
        label = QLabel(text)
        label.setObjectName('sectionHeader')
        return label

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        left = QFrame()
        left.setObjectName('devicePanel')
        left.setMinimumWidth(320)
        left.setMaximumWidth(440)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 8)
        left_layout.setSpacing(8)
        left_layout.addWidget(self.header('设备列表'))
        scan_row = QHBoxLayout()
        scan_row.setContentsMargins(10, 2, 10, 0)
        self.scan_button = self.button('子网扫描', self.scan, scan_row)
        left_layout.addLayout(scan_row)
        self.scan_label = QLabel('自动识别本机局域网')
        self.scan_label.setObjectName('muted')
        self.scan_label.setContentsMargins(12, 0, 12, 0)
        self.scan_label.setWordWrap(True)
        left_layout.addWidget(self.scan_label)
        self.progress = QProgressBar()
        self.progress.hide()
        left_layout.addWidget(self.progress)
        selection_row = QHBoxLayout()
        selection_row.setContentsMargins(10, 0, 10, 0)
        self.button('全选', self.select_all, selection_row, 'secondary')
        self.button('清空选择', self.clear_selection, selection_row, 'secondary')
        left_layout.addLayout(selection_row)
        self.selection_label = QLabel('共 0 台 · 已选 0 台')
        self.selection_label.setObjectName('muted')
        self.selection_label.setContentsMargins(12, 0, 12, 0)
        left_layout.addWidget(self.selection_label)
        self.device_list = DeviceListPanel()
        self.device_list.activated.connect(self.show_details)
        self.device_list.selection_changed.connect(self.update_selection_count)
        left_layout.addWidget(self.device_list, 1)
        splitter.addWidget(left)

        workspace = QFrame()
        workspace.setObjectName('workspace')
        right = QVBoxLayout(workspace)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(12)
        right.addWidget(self.header('老化监控'))
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 0, 16, 16)
        content_layout.setSpacing(12)
        right.addWidget(content, 1)
        self.device_info = QLabel('请选择设备查看详情')
        self.device_info.setTextFormat(Qt.PlainText)
        self.device_info.setObjectName('deviceInfo')
        self.device_info.setWordWrap(True)
        content_layout.addWidget(self.device_info)
        self.plan_label = QLabel('尚未配置计划 · 请选择老化项目和时长')
        self.plan_label.setObjectName('planSummary')
        self.plan_label.setWordWrap(True)
        content_layout.addWidget(self.plan_label)
        actions = QGridLayout()
        actions.setSpacing(8)
        for row, specs in enumerate([
            [('配置计划', self.configure, None), ('读取设备信息', self.probe_selected, 'secondary'),
             ('开始选中设备', self.start, 'start')],
            [('停止选中设备', self.stop_selected, 'danger'), ('停止整批', self.stop_all, 'danger'),
             ('确认电源切换', self.confirm_battery, 'confirm')]]):
            for col, (text, action, role) in enumerate(specs):
                button = QPushButton(text)
                if role:
                    button.setProperty('role', role)
                button.clicked.connect(action)
                actions.addWidget(button, row, col)
                if text == '读取设备信息':
                    self.probe_button = button
                    button.setToolTip('读取设备 SN、机型、内存、存储和工具状态；开始老化时会自动读取。勾选可批量读取，未勾选时读取当前查看的设备。')
        content_layout.addLayout(actions)
        self.tabs = QTabWidget()
        content_layout.addWidget(self.tabs, 1)
        self.metrics = MetricsPanel()
        self.tabs.addTab(self.metrics, '实时曲线')
        history = QWidget()
        history_layout = QVBoxLayout(history)
        history_actions = QHBoxLayout()
        self.button('刷新', self.load_history, history_actions, 'secondary')
        self.button('HTML 报告', self.open_report, history_actions)
        self.button('批次报告', self.open_batch, history_actions, 'secondary')
        self.button('恢复补采', self.resume, history_actions, 'secondary')
        history_layout.addLayout(history_actions)
        self.statistics = QLabel()
        self.statistics.setObjectName('muted')
        self.statistics.setWordWrap(True)
        history_layout.addWidget(self.statistics)
        self.history = QTableWidget(0, 6)
        self.history.setHorizontalHeaderLabels(['创建时间', 'SN', 'IP', '状态', '结论', '运行 ID'])
        self.history.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history.setAlternatingRowColors(True)
        self.history.verticalHeader().hide()
        self.history.verticalHeader().setDefaultSectionSize(38)
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.history.horizontalHeader().setStretchLastSection(True)
        history_layout.addWidget(self.history)
        self.tabs.addTab(history, '历史记录 / 报告')
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.tabs.addTab(self.detail, '运行详情')
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setMaximumBlockCount(1000)
        self.tabs.addTab(self.messages, '操作日志')
        footer = QHBoxLayout()
        hint = QLabel('结果保存在本机 · HTML 报告 / SQLite')
        hint.setObjectName('muted')
        footer.addWidget(hint, 1)
        self.button('打开结果目录', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(runtime_dir()))), footer, 'secondary')
        content_layout.addLayout(footer)
        splitter.addWidget(workspace)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([350, 990])
        self.statusBar().showMessage('就绪')

    def log(self, message):
        self.messages.appendPlainText(message)
        self.statusBar().showMessage(message, 10000)

    def job(self, function, callback, on_error=None):
        self.jobs.append((self.work_pool.submit(function), callback, on_error))

    def found(self, device):
        ip = device['ip']
        new = ip not in self.devices
        self.devices.setdefault(ip, {}).update(device)
        self.device_list.upsert(self.devices[ip])
        self.update_selection_count()
        if new:
            self.log('发现：%s (%s)' % (device.get('sn', ''), ip))

    def selected(self):
        return [device for ip, device in self.devices.items() if self.device_list.cards[ip].check.isChecked()]

    def update_selection_count(self):
        self.selection_label.setText('共 %s 台 · 已选 %s 台' % (len(self.devices), len(self.selected())))

    def select_all(self):
        for card in self.device_list.cards.values():
            card.check.setChecked(True)

    def clear_selection(self):
        for card in self.device_list.cards.values():
            card.check.setChecked(False)

    def scan(self):
        if self.scanning:
            return
        try:
            interfaces = self.discovery.interfaces()
            subnet = interfaces[0]['subnet'] if len(interfaces) == 1 else None
            if len(interfaces) > 1:
                dialog = InterfaceDialog(interfaces, self)
                if dialog.exec_() != QDialog.Accepted:
                    return
                subnet = dialog.subnet()
            self.scanning = True
            self.scan_button.setEnabled(False)
            self.scan_label.setText('正在扫描：' + (subnet or '自动识别局域网'))
            self.progress.setRange(0, 0)
            self.progress.show()
            self.log('开始子网扫描：' + (subnet or '自动识别局域网'))
            def progress(scanned, total):
                try:
                    self.scan_progress.put_nowait((scanned, total))
                except queue.Full:
                    pass
            def complete(devices):
                self.finish_scan()
                self.scan_label.setText('扫描完成 · 发现 %s 台设备' % len(devices))
                self.log('子网扫描完成，发现 %s 台设备' % len(devices))
                if not devices:
                    QMessageBox.information(self, '扫描结果', '未发现任何设备，请检查设备与电脑是否在同一局域网。')
                    return
                dialog = DeviceSelectionDialog(devices, self)
                if dialog.exec_() == QDialog.Accepted:
                    selected = dialog.selected_devices()
                    # 空闲时用本次结果替换列表；运行或补采时保留原设备与运行映射。
                    if not self.service.busy() and not self.jobs and selected:
                        self.devices.clear()
                        self.device_list.clear()
                        self.run_devices.clear()
                        self.device_info.setText('请选择设备查看详情')
                        self.metrics.select(None)
                        self.detail.clear()
                    for device in selected:
                        self.found(device)
                        self.device_list.cards[device['ip']].check.setChecked(True)
            def failed(exc):
                self.finish_scan()
                self.scan_label.setText('扫描失败，请重试')
                self.error(exc)
            self.job(lambda: self.discovery.scan(subnet, progress), complete, failed)
        except Exception as exc:
            self.finish_scan()
            self.error(exc)

    def finish_scan(self):
        self.scanning = False
        self.scan_button.setEnabled(True)
        self.progress.hide()
        while not self.scan_progress.empty():
            try:
                self.scan_progress.get_nowait()
            except queue.Empty:
                break

    def probe(self, device):
        ip = device['ip']
        if ip in self.probing:
            return
        self.probing.add(ip)
        self.update_probe_button()
        self.log('正在读取设备信息：%s (%s)' % (device.get('sn', ''), ip))
        def read():
            transport = SSHTransport(device, progress=lambda message: self.job_messages.put('%s：%s' % (ip, message)))
            try:
                return dict(transport.probe(), ip=device['ip'])
            finally:
                transport.close()
        def completed(info):
            self.finish_probe(ip)
            if not info['sn']:
                self.error('设备 /etc/serial-id 为空，不能作为老化设备')
                return
            self.apply_device_info(info)
            size = info.get('memory', {}).get('MemTotal', 0) / 2**30
            self.log('设备信息读取完成：%s (%s)，机型 %s，内存 %.1f GiB' % (info['sn'], ip, info['chip'], size))
            if self.device_list.current_ip == info['ip']:
                self.show_details(info['ip'])
        def failed(exc):
            self.finish_probe(ip)
            self.error('读取 %s 设备信息失败：%s' % (ip, exc))
        self.job(read, completed, failed)

    def apply_device_info(self, info):
        self.found(info)
        entry = self.devices[info['ip']]
        entry['hardware_text'] = info['chip'] + ' / ' + info['kernel']
        size = info.get('memory', {}).get('MemTotal', 0) / 2**30
        disks = ', '.join('%s %.1f GiB' % (d['name'], d['size_bytes'] / 2**30) for d in info['disks'])
        entry['storage_text'] = '%.1f GiB / %s' % (size, disks)
        self.device_list.upsert(entry)

    def finish_probe(self, ip):
        self.probing.discard(ip)
        self.update_probe_button()

    def update_probe_button(self):
        self.probe_button.setText('正在读取（%s）' % len(self.probing) if self.probing else '读取设备信息')
        self.probe_button.setEnabled(not self.probing)

    def probe_selected(self):
        devices = self.selected()
        if not devices and self.device_list.current_ip in self.devices:
            devices = [self.devices[self.device_list.current_ip]]
        if not devices:
            QMessageBox.information(self, '读取设备信息', '请先扫描并添加设备，再点击设备卡片或勾选需要读取的设备。')
            return
        for device in devices:
            self.probe(device)

    def configure(self):
        dialog = PlanDialog(self.plan, self)
        if dialog.exec_():
            self.plan = dialog.plan
            self.plan_valid = True
            selected = ', '.join(ITEMS[k] for k, v in self.plan['items'].items() if v['enabled'])
            self.plan_label.setText('项目：%s · 并行计划约 %.0f 分钟（另计人工等待）' %
                                    (selected, planned_seconds(self.plan) / 60))

    def start(self):
        try:
            devices = self.selected()
            if not devices:
                raise ValueError('请勾选要运行的设备')
            if any(device['ip'] in self.probing for device in devices):
                raise ValueError('所选设备正在读取信息，请等待完成后开始老化')
            if not self.plan_valid:
                self.configure()
                if not self.plan_valid:
                    return
            ids = self.service.start(deepcopy(devices), self.plan)
            for run_id, device in zip(ids, devices):
                self.run_devices[run_id] = device['ip']
                self.devices[device['ip']]['run_id'] = run_id
            self.load_history()
            self.log('已提交 %s 台设备，正在检查环境并自动部署缺失依赖，完成后开始老化' % len(ids))
        except Exception as exc:
            self.error(exc)

    def stop_selected(self):
        for device in self.selected():
            if device.get('run_id'):
                self.service.stop(device['run_id'])
        self.log('已请求停止选中设备；离线设备会在重连后接收，设备本地保护继续生效')

    def stop_all(self):
        self.service.shutdown()
        self.log('已请求停止全部本地活动运行')

    def confirm_battery(self):
        operator, accepted = QInputDialog.getText(self, '电源切换确认', '请填写本次确认人')
        if not accepted or not operator.strip():
            return
        for device in self.selected():
            run_id = device.get('run_id')
            status = self.statuses.get(run_id, {})
            b = status.get('battery', {})
            if status.get('state') != 'WAIT_OPERATOR' or b.get('state') != 'WAIT_OPERATOR':
                continue
            operation = '接入适配器' if b['phase'] == 'charge' else '拔除适配器，确认放电负载'
            if QMessageBox.question(self, '电源切换确认', '%s (%s)\n请%s，然后确认。' % (device['sn'], device['ip'], operation)) == QMessageBox.Yes:
                self.service.confirm(run_id, b['phase'], b['index'], operator.strip())

    def show_details(self, ip=None):
        ip = ip or self.device_list.current_ip
        device = self.devices.get(ip)
        if device:
            self.metrics.select(device.get('run_id'))
            status = self.statuses.get(device.get('run_id'), device)
            state = status.get('result') or STATES.get(status.get('state'), status.get('state', '待配置'))
            self.device_info.setText('SN: %s    IP: %s    状态: %s' % (device.get('sn', '待读取'), ip, state))
            self.detail.setPlainText(json.dumps(status, ensure_ascii=False, indent=2))

    def load_history(self):
        rows = self.repository.runs()
        counts = {}
        for row in rows:
            key = row['result'] or '未结束'
            counts[key] = counts.get(key, 0) + 1
        self.statistics.setText('总计 %s · %s' % (len(rows), ' · '.join('%s %s' % item for item in counts.items())))
        self.history.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [row['created_at'], row['sn'], row['ip'], row['state'], row['result'] or '', row['run_id']]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(STATES.get(value, value) or '') if col == 3 else str(value or ''))
                if col in (3, 4):
                    item.setForeground(QColor(status_color(value)))
                self.history.setItem(index, col, item)

    def history_id(self):
        row = self.history.currentRow()
        if row < 0:
            raise ValueError('请先选择一条历史记录')
        return self.history.item(row, 5).text()

    def open_report(self):
        try:
            path = export_report(self.repository, self.history_id())
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            self.error(exc)

    def open_batch(self):
        try:
            from ..infrastructure.report import export_batch
            row = self.repository.get(self.history_id())
            path = export_batch(self.repository, row['batch_id'])
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            self.error(exc)

    def resume(self):
        try:
            run_id = self.history_id()
            row = self.repository.get(run_id)
            if row['state'] in ('FINISHED', 'BLOCKED'):
                raise ValueError('这条运行已结束，无需恢复补采')
            address, accepted = QInputDialog.getText(self, '恢复设备连接', '当前设备 IPv4（IP 变化时可修改，重连后核验真实身份）', text=row['ip'])
            if not accepted:
                return
            address = str(ipaddress.IPv4Address(address.strip()))
            if address not in self.devices:
                self.found({'ip': address, 'sn': row['sn']})
            self.run_devices[run_id] = address
            self.devices[address]['run_id'] = run_id
            self.service.resume(run_id, address)
            self.log('正在恢复补采：' + run_id)
        except Exception as exc:
            self.error(exc)

    def error(self, error):
        self.log(str(error))
        QMessageBox.warning(self, '操作未完成', str(error))

    def tick(self):
        while True:
            try:
                self.log(self.job_messages.get_nowait())
            except queue.Empty:
                break
        if self.scanning:
            try:
                scanned, total = self.scan_progress.get_nowait()
                self.progress.setRange(0, total)
                self.progress.setValue(scanned)
                self.progress.setFormat('已扫描 %v / %m')
            except queue.Empty:
                pass
        for future, callback, on_error in list(self.jobs):
            if future.done():
                self.jobs.remove((future, callback, on_error))
                try:
                    callback(future.result())
                except Exception as exc:
                    (on_error or self.error)(exc)
        changed = False
        while True:
            try:
                run_id, status = self.service.events.get_nowait()
            except queue.Empty:
                break
            previous = self.statuses.get(run_id, {})
            self.statuses[run_id] = status
            self.metrics.update_status(run_id, status)
            ip = self.run_devices.get(run_id)
            if ip in self.devices:
                reason = status.get('error') or status.get('connection_error') or status.get('safety_stop') or status.get('environment_message') or ''
                previous_reason = previous.get('error') or previous.get('connection_error') or previous.get('safety_stop') or previous.get('environment_message') or ''
                if (status['state'], reason) != (previous.get('state'), previous_reason):
                    self.log('%s：%s %s' % (self.devices[ip]['sn'], STATES.get(status['state'], status['state']), reason))
                if status.get('hardware') and status['hardware'] != previous.get('hardware'):
                    self.apply_device_info(dict(status['hardware'], ip=ip))
                latest = status.get('latest', {})
                temps = list(latest.get('temperature_c', {}).values())
                remaining = ', '.join('%s %.0fm' % (ITEMS.get(key, key), max(0, item.get('planned_s', 0) - item.get('effective_s', 0)) / 60)
                                       for key, item in status.get('items', {}).items() if item.get('result') is None)
                temperature = ('%.1f℃' % max(temps)) if temps else '温度未采到'
                busy = latest.get('cpu', {}).get('busy_pct')
                busy_text = ('%.1f%%' % busy) if busy is not None else '未采到'
                live = reason if status['state'] in ('DEPLOYING', 'READY') else 'CPU %s · %s\n%s' % (busy_text, temperature, remaining)
                self.device_list.cards[ip].update_status(status, live)
                if self.device_list.current_ip == ip:
                    self.show_details(ip)
            changed = True
        if changed:
            self.load_history()
        if self.closing and not self.service.busy() and not self.jobs:
            self.close()

    def closeEvent(self, event):
        if self.service.busy():
            box = QMessageBox(self)
            box.setWindowTitle('设备仍在运行')
            box.setText('关闭前选择如何处理设备运行。离线设备的停止命令需等待重连。')
            stop = box.addButton('停止并等待', QMessageBox.AcceptRole)
            detach = box.addButton('设备继续，退出后可恢复补采', QMessageBox.DestructiveRole)
            cancel = box.addButton('取消', QMessageBox.RejectRole)
            box.exec_()
            if box.clickedButton() == cancel:
                event.ignore()
                return
            self.closing = True
            self.detached = box.clickedButton() == detach
            self.service.shutdown(detach=self.detached)
        if self.service.busy() or self.jobs:
            self.closing = True
            self.log('正在结束控制线程，请稍候。')
            event.ignore()
            return
        self.timer.stop()
        self.service.shutdown(detach=True)
        self.work_pool.shutdown(wait=False)
        event.accept()
