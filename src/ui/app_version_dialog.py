import time
import re

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QPushButton, QLabel, QLineEdit, QTableWidget,
                             QTableWidgetItem, QCheckBox, QHeaderView, QGroupBox,
                             QTextEdit, QProgressBar, QMessageBox, QWidget,
                             QAbstractItemView)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

from ..network.speaker_http_client import SpeakerHTTPClient
from ..utils.logger import logger

# 重启后等待设备上线的超时时间
REBOOT_TIMEOUT = 60


def _strip_v(version: str) -> str:
    """去掉版本号前缀的 v/V，用于版本比对"""
    if not version:
        return ''
    return re.sub(r'^[vV]', '', str(version).strip())


class AppVersionWriteWorker(QThread):
    """单设备版本写入线程：写入版本 → 重启 → HTTP查询新版本"""
    log_signal = pyqtSignal(str, str)        # (sn, message)
    progress_signal = pyqtSignal(str, int)   # (sn, percent)
    finished_signal = pyqtSignal(str, bool, str, str)  # (sn, success, reason, new_version)

    def __init__(self, device, version, config):
        super().__init__()
        self.device = device
        self.version = version
        self.config = config

    def _log(self, msg):
        self.log_signal.emit(self.device.sn, msg)

    def run(self):
        sn = self.device.sn
        try:
            self._log(f"开始版本写入 {sn} ({self.device.ip})")
            self._log(f"目标版本: {self.version}")

            # 1. 写入版本
            self.progress_signal.emit(sn, 10)
            http_client = SpeakerHTTPClient(self.device.ip, port=8080)
            resp = http_client.version_set(self.version)

            if not resp or resp.get('code') != 0:
                msg = resp.get('message', '无响应') if resp else '无响应'
                self._log(f"❌ 版本写入失败: {msg}")
                self.finished_signal.emit(sn, False, f"版本写入失败: {msg}", "")
                return

            self._log("✅ 版本写入成功")
            self.progress_signal.emit(sn, 60)

            # 版本写入成功后，读取并验证版本
            self._log("正在读取当前版本...")
            time.sleep(1)  # 等待设备写入完成

            verify_resp = http_client.get_version()
            if verify_resp and verify_resp.get('code') == 0:
                data = verify_resp.get('data', {})
                app_version = data.get('app_version', '')
                current_version = _strip_v(app_version)

                self._log(f"当前版本: {current_version}")
                self.progress_signal.emit(sn, 100)

                if current_version == _strip_v(self.version):
                    self._log("✅ 版本验证成功")
                    self.finished_signal.emit(sn, True, "版本写入并验证成功", current_version)
                else:
                    self._log(f"⚠ 版本不一致: 期望 {self.version}, 实际 {current_version}")
                    self.finished_signal.emit(sn, True, f"版本已写入但不一致(期望{_strip_v(self.version)})", current_version)
            else:
                self._log("⚠ 读取版本失败，但版本已写入")
                self.progress_signal.emit(sn, 100)
                self.finished_signal.emit(sn, True, "版本写入成功(未验证)", self.version)

        except Exception as e:
            logger.error(f"设备 {sn} 版本写入异常: {e}")
            self._log(f"❌ 版本写入异常: {e}")
            self.finished_signal.emit(sn, False, f"版本写入异常: {e}", "")

    def _auto_sync_datetime(self, sn):
        """自动校时（仅室外音箱）"""
        try:
            self._log(f"[自动校时] 开始校时: {sn}")

            # 检查设备版本，只有 >= 1.0.0.9 的固件才支持校时
            from ..network.speaker_http_client import SpeakerHTTPClient
            http_client = SpeakerHTTPClient(self.device.ip, port=8080)
            version_resp = http_client.get_version()

            if version_resp and version_resp.get('code') == 0:
                data = version_resp.get('data', {})
                kernel_ver = data.get('kernel', '')
                rootfs_ver = data.get('rootfs', '')

                # 检查版本号
                def version_check(ver_str: str) -> bool:
                    """检查版本是否 >= 1.0.0.9"""
                    try:
                        ver_str = ver_str.lstrip('vV')
                        parts = ver_str.split('.')
                        if len(parts) != 4:
                            return False
                        ver_tuple = tuple(int(p) for p in parts)
                        return ver_tuple >= (1, 0, 0, 9)
                    except:
                        return False

                if not version_check(kernel_ver) and not version_check(rootfs_ver):
                    self._log(f"[自动校时] 跳过校时 (固件版本 < 1.0.0.9，不支持校时功能)")
                    self._log(f"[自动校时] 当前版本: kernel={kernel_ver}, rootfs={rootfs_ver}")
                    return

            # 导入校时模块
            from ..network.speaker_mqtt_datetime_sync import sync_speaker_datetime

            # 执行校时
            result = sync_speaker_datetime(
                broker=self.config.mqtt_broker,
                port=self.config.mqtt_port,
                product_id=self.device.get_product_id(),
                device_sn=sn,
                device_model=self.device.model or ""
            )

            if result:
                self._log(f"[自动校时] ✅ 校时成功: {sn}")
            else:
                self._log(f"[自动校时] ⚠️ 校时失败: {sn}")

        except Exception as e:
            logger.error(f"[自动校时] 校时异常: {sn} -> {e}")
            self._log(f"[自动校时] ❌ 校时异常: {e}")

    def _wait_and_query_version_http(self, sn, http_client):
        """等待设备重启并通过HTTP查询版本，返回新版本号或 None"""
        start = time.time()

        # 先断开一段时间，让设备完全重启
        time.sleep(10)

        while time.time() - start < REBOOT_TIMEOUT:
            elapsed = time.time() - start
            # 进度从 50% 到 95%
            pct = 50 + int((elapsed / REBOOT_TIMEOUT) * 45)
            self.progress_signal.emit(sn, min(pct, 95))

            try:
                # 通过HTTP查询版本
                result = http_client.get_version()

                if result and result.get('code') == 0:
                    # 解析HTTP响应
                    data = result.get('data', {})
                    app_version = data.get('app_version', '')

                    if app_version:
                        self._log(f"HTTP查询成功，app_version: {app_version}")
                        return _strip_v(app_version)
                    else:
                        self._log(f"[{int(elapsed)}s] 版本信息不完整，继续等待...")
                else:
                    self._log(f"[{int(elapsed)}s] 设备无响应，继续等待...")

            except Exception as e:
                logger.debug(f"HTTP查询版本异常: {e}")
                self._log(f"[{int(elapsed)}s] HTTP查询失败，继续等待...")

            time.sleep(5)

        return None


class AppVersionDialog(QDialog):
    version_write_finished_signal = pyqtSignal(str, bool, str)  # (sn, success, new_version) 单台设备版本写入结束

    def __init__(self, devices, config, parent=None):
        super().__init__(parent)
        self.devices = devices
        self.config = config
        self.workers = {}          # sn -> Worker
        self.device_rows = {}      # sn -> (checkbox, progress_bar)

        self.setWindowTitle('App 版本写入')
        self.resize(700, 600)
        self._build_ui()
        self._refresh_device_table()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ==== 设备区 ====
        dev_group = QGroupBox('1. 选择设备')
        dev_layout = QVBoxLayout(dev_group)

        header_row = QHBoxLayout()
        self.select_all_cb = QCheckBox('全选')
        self.select_all_cb.stateChanged.connect(self._on_select_all)
        header_row.addWidget(self.select_all_cb)
        header_row.addStretch()
        refresh_btn = QPushButton('刷新设备')
        refresh_btn.clicked.connect(self._refresh_device_table)
        header_row.addWidget(refresh_btn)
        dev_layout.addLayout(header_row)

        self.device_table = QTableWidget()
        self.device_table.setColumnCount(5)
        self.device_table.setHorizontalHeaderLabels(['选择', '设备SN', '型号', 'IP地址', '进度'])
        self.device_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.device_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.device_table.verticalHeader().setVisible(False)
        h = self.device_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.device_table.setMinimumHeight(200)
        dev_layout.addWidget(self.device_table)
        layout.addWidget(dev_group)

        # ==== 版本输入区 ====
        version_group = QGroupBox('2. 输入版本号')
        version_layout = QHBoxLayout(version_group)
        version_layout.addWidget(QLabel('App 版本:'))
        self.version_edit = QLineEdit()
        self.version_edit.setPlaceholderText('如 1.0.0.36')
        version_layout.addWidget(self.version_edit)
        layout.addWidget(version_group)

        # ==== 操作区 ====
        opt_layout = QHBoxLayout()
        opt_layout.addStretch()
        self.start_btn = QPushButton('开始写入')
        self.start_btn.setStyleSheet(
            "QPushButton{background-color:#27ae60;color:white;font-weight:bold;"
            "padding:8px 20px;border-radius:4px;}"
            "QPushButton:hover{background-color:#229954;}"
            "QPushButton:disabled{background-color:#bdc3c7;}")
        self.start_btn.clicked.connect(self._on_start)
        opt_layout.addWidget(self.start_btn)
        layout.addLayout(opt_layout)

        # ==== 日志区 ====
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(
            "QTextEdit{background-color:#2c3e50;color:#ecf0f1;"
            "font-family:Consolas,Monaco,monospace;font-size:12px;}")
        self.log_text.setMinimumHeight(200)
        layout.addWidget(self.log_text)

    # ==================== 设备表 ====================
    def _refresh_device_table(self):
        self.device_table.setRowCount(len(self.devices))
        self.device_rows.clear()
        for i, device in enumerate(self.devices):
            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb_layout.setAlignment(Qt.AlignCenter)
            checkbox = QCheckBox()
            cb_layout.addWidget(checkbox)
            self.device_table.setCellWidget(i, 0, cb_widget)

            self.device_table.setItem(i, 1, QTableWidgetItem(device.sn))
            self.device_table.setItem(i, 2, QTableWidgetItem(device.model or ''))
            self.device_table.setItem(i, 3, QTableWidgetItem(device.ip))

            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(0)
            progress.setTextVisible(True)
            self.device_table.setCellWidget(i, 4, progress)

            self.device_rows[device.sn] = (checkbox, progress)

    def _on_select_all(self, state):
        checked = state == Qt.Checked
        for checkbox, _ in self.device_rows.values():
            checkbox.setChecked(checked)

    def _selected_devices(self):
        result = []
        for device in self.devices:
            row = self.device_rows.get(device.sn)
            if row and row[0].isChecked():
                result.append(device)
        return result

    # ==================== 开始写入 ====================
    def _on_start(self):
        selected = self._selected_devices()
        if not selected:
            QMessageBox.warning(self, '提示', '请至少选择一个设备')
            return

        version = self.version_edit.text().strip()
        if not version:
            QMessageBox.warning(self, '提示', '请输入版本号')
            return

        # 版本号正则校验：只允许数字和点
        import re
        if not re.match(r'^[\d.]+$', version):
            QMessageBox.warning(
                self, '版本号格式错误',
                '版本号只能包含数字和点(.)\n'
                '例如: 1.0.0.36 或 1.0.0.1'
            )
            return

        # 额外检查：避免连续的点或以点开头/结尾
        if '..' in version or version.startswith('.') or version.endswith('.'):
            QMessageBox.warning(
                self, '版本号格式错误',
                '版本号格式不正确\n'
                '不能以点开头/结尾，不能包含连续的点\n'
                '例如: 1.0.0.36'
            )
            return

        self.log_text.append("=" * 50)
        self.log_text.append(f"开始版本写入 {len(selected)} 个设备")
        self.log_text.append(f"目标版本: {version}")

        self.start_btn.setEnabled(False)
        self.select_all_cb.setEnabled(False)
        self.version_edit.setEnabled(False)

        # 重置进度并逐设备启动Worker
        for device in selected:
            checkbox, progress = self.device_rows[device.sn]
            checkbox.setEnabled(False)
            progress.setValue(0)

            worker = AppVersionWriteWorker(device, version, self.config)
            worker.log_signal.connect(self._on_worker_log)
            worker.progress_signal.connect(self._on_worker_progress)
            worker.finished_signal.connect(self._on_worker_finished)
            self.workers[device.sn] = worker
            worker.start()

    def _on_worker_log(self, sn, msg):
        self.log_text.append(f"[{sn}] {msg}")

    def _on_worker_progress(self, sn, pct):
        row = self.device_rows.get(sn)
        if row:
            row[1].setValue(pct)

    def _on_worker_finished(self, sn, success, reason, new_version):
        row = self.device_rows.get(sn)
        if row:
            checkbox, progress = row
            checkbox.setEnabled(True)
            if success:
                progress.setValue(100)
                progress.setStyleSheet("QProgressBar::chunk{background-color:#27ae60;}")
            else:
                progress.setStyleSheet("QProgressBar::chunk{background-color:#e74c3c;}")

        if success:
            status = f"✅ 版本写入成功"
            if new_version:
                status += f" (当前版本: {new_version})"
        else:
            status = f"❌ 版本写入失败: {reason}"

        self.log_text.append(f"[{sn}] {status}")

        self.workers.pop(sn, None)

        # 通知父窗口该设备版本写入已结束（无论成败）
        self.version_write_finished_signal.emit(sn, success, new_version if success else "")

        # 全部完成后恢复按钮
        if not self.workers:
            self.start_btn.setEnabled(True)
            self.select_all_cb.setEnabled(True)
            self.version_edit.setEnabled(True)
            self.log_text.append("=" * 50)
            self.log_text.append("全部设备版本写入流程结束")

    def closeEvent(self, event):
        running = [sn for sn, w in self.workers.items() if w.isRunning()]
        if running:
            reply = QMessageBox.question(
                self, '确认', f'仍有 {len(running)} 个设备正在处理，确定关闭？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                event.ignore()
                return
            for w in self.workers.values():
                w.quit()
                w.wait(2000)
        event.accept()
