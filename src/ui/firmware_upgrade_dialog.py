import os
import time
import threading
import shutil

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QPushButton, QLabel, QLineEdit, QTableWidget,
                             QTableWidgetItem, QCheckBox, QHeaderView, QGroupBox,
                             QTextEdit, QProgressBar, QFileDialog, QMessageBox,
                             QSpinBox, QWidget, QAbstractItemView)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer

from ..network.speaker_http_client import SpeakerHTTPClient
from ..utils.logger import logger
import re

# 升级轮询参数
POLL_INTERVAL = 10      # 每10秒查询一次版本
POLL_TIMEOUT = 180      # 超时3分钟


def _strip_v(version: str) -> str:
    """去掉版本号前缀的 v/V，用于版本比对"""
    if not version:
        return ''
    return re.sub(r'^[vV]', '', str(version).strip())


class FirmwareUploadWorker(QThread):
    """固件上传线程：将固件拷贝到托管目录并显示进度"""
    progress_signal = pyqtSignal(int)  # 进度百分比
    success_signal = pyqtSignal(str)   # 上传成功，参数是文件名
    failed_signal = pyqtSignal(str)    # 上传失败，参数是错误信息

    def __init__(self, src_path, dest_dir, file_size):
        super().__init__()
        self.src_path = src_path
        self.dest_dir = dest_dir
        self.file_size = file_size

    def run(self):
        try:
            filename = os.path.basename(self.src_path)
            dest = os.path.join(self.dest_dir, filename)

            # 按文件大小分块更新进度
            chunk_size = max(self.file_size // 20, 1024 * 1024)  # 至少1MB一块
            with open(self.src_path, 'rb') as src_file:
                with open(dest, 'wb') as dst_file:
                    copied = 0
                    while True:
                        chunk = src_file.read(chunk_size)
                        if not chunk:
                            break
                        dst_file.write(chunk)
                        copied += len(chunk)
                        progress = int(copied * 100 / self.file_size)
                        self.progress_signal.emit(progress)
                        time.sleep(0.02)  # 模拟延迟，让进度可见

            # 上传成功
            self.success_signal.emit(filename)
        except Exception as e:
            logger.error(f"上传固件失败: {e}")
            self.failed_signal.emit(str(e))


class FirmwareUpgradeWorker(QThread):
    """单设备固件升级线程：触发HTTP OTA → HTTP轮询版本比对"""
    log_signal = pyqtSignal(str, str)        # (sn, message)
    progress_signal = pyqtSignal(str, int)   # (sn, percent)
    finished_signal = pyqtSignal(str, bool, str)  # (sn, success, reason)

    def __init__(self, device, config_str, images_str, targets, config):
        super().__init__()
        self.device = device
        self.config_str = config_str
        self.images_str = images_str
        # targets: {'kernel': '1.0.0.9', 'rootfs': '1.0.0.9'} 仅含本次升级类型
        self.targets = targets
        self.config = config

    def _log(self, msg):
        self.log_signal.emit(self.device.sn, msg)

    def run(self):
        sn = self.device.sn
        try:
            self._log(f"开始升级 {sn} ({self.device.ip})")
            self._log(f"config: {self.config_str}")
            self._log(f"images: {self.images_str}")

            # 1. 触发HTTP OTA升级
            http_client = SpeakerHTTPClient(self.device.ip, port=8080)
            self._log(f"发送OTA请求到: http://{self.device.ip}:8080/api/ota/upgrade")
            resp = http_client.ota_upgrade(self.config_str, self.images_str, timeout=30)

            if not resp or resp.get('code') != 0:
                msg = resp.get('message', '无响应') if resp else '无响应'
                self._log(f"❌ 触发升级失败: {msg}")
                self._log(f"   响应详情: {resp}")
                self.finished_signal.emit(sn, False, f"触发失败: {msg}")
                return

            self._log(f"✅ 升级指令已接受 (响应: {resp})")
            self._log("   设备开始下载固件...")
            self.progress_signal.emit(sn, 5)

            # 2. HTTP轮询版本比对
            ok, reason = self._poll_version_http(sn, http_client)
            self.finished_signal.emit(sn, ok, reason)

        except Exception as e:
            logger.error(f"设备 {sn} 升级异常: {e}")
            self._log(f"❌ 升级异常: {e}")
            self.finished_signal.emit(sn, False, f"升级异常: {e}")

    def _poll_version_http(self, sn, http_client):
        """通过HTTP轮询版本，比对是否达到目标版本"""
        self._log("开始HTTP轮询版本...")
        start = time.time()
        last_query = 0

        try:
            while time.time() - start < POLL_TIMEOUT:
                elapsed = time.time() - start
                # 进度按时间映射 5%~95%
                pct = 5 + int(elapsed / POLL_TIMEOUT * 90)
                self.progress_signal.emit(sn, min(pct, 95))

                if time.time() - last_query >= POLL_INTERVAL:
                    last_query = time.time()

                    # 通过HTTP查询版本
                    result = http_client.get_version()

                    if not result or result.get('code') != 0:
                        self._log(f"[{int(elapsed)}s] 设备无响应(可能重启中)，继续等待...")
                    else:
                        # 解析HTTP响应
                        data = result.get('data', {})
                        app_version = data.get('app_version', '')
                        ab_system = data.get('ab_system', {})
                        partitions = ab_system.get('partitions', {})

                        versions = {
                            'app_version': _strip_v(app_version),
                            'kernel': _strip_v(partitions.get('kernel', '')),
                            'rootfs_a': _strip_v(partitions.get('rootfs_a', '')),
                            'rootfs_b': _strip_v(partitions.get('rootfs_b', '')),
                        }

                        self._log(f"[{int(elapsed)}s] 当前版本 "
                                  f"kernel={versions.get('kernel')} "
                                  f"rootfs_a={versions.get('rootfs_a')} "
                                  f"rootfs_b={versions.get('rootfs_b')}")

                        matched, reason = self._check_match(versions)
                        if matched:
                            self.progress_signal.emit(sn, 100)
                            self._log(f"✅ 版本比对成功，升级完成 (目标版本: {self.targets})")

                            # 室外音箱自动校时：等待5秒让设备完全准备好
                            if self.device.is_outdoor_speaker():
                                self._log(f"[自动校时] 等待5秒后开始校时...")
                                time.sleep(5)
                                self._auto_sync_datetime(sn)

                            return True, "升级成功"
                        elif reason:
                            self._log(f"版本尚未匹配: {reason}")

                time.sleep(1)

            # 超时：给出最后一次不一致原因
            self._log("❌ 升级超时(3分钟)，版本未达到预期")
            return False, "轮询超时(3分钟)，版本未更新到目标"

        except Exception as e:
            logger.error(f"HTTP轮询版本异常: {e}")
            return False, f"轮询异常: {e}"

    def _auto_sync_datetime(self, sn):
        """自动校时（仅室外音箱）"""
        try:
            self._log(f"[自动校时] 开始校时: {sn}")

            # 检查设备版本，只有 >= 1.0.0.9 的固件才支持校时
            # 使用刚刚查询到的版本信息
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

    def _check_match(self, versions):
        """校验本次升级类型的版本是否达到目标。返回 (是否全部匹配, 不一致描述)"""
        mismatches = []

        if 'kernel' in self.targets:
            target = _strip_v(self.targets['kernel'])
            actual = versions.get('kernel', '')
            if actual != target:
                mismatches.append(f"kernel: 期望{target} 实际{actual or '未知'}")

        if 'rootfs' in self.targets:
            target = _strip_v(self.targets['rootfs'])
            a = versions.get('rootfs_a', '')
            b = versions.get('rootfs_b', '')
            if a != b:
                mismatches.append(f"rootfs: A/B分区不一致(A={a} B={b})")
            elif a != target:
                mismatches.append(f"rootfs: 期望{target} 实际{a or '未知'}")

        if mismatches:
            return False, "; ".join(mismatches)
        return True, ""


class _FirmwareSlot:
    """一个固件槽位(kernel或rootfs)的输入控件集合"""
    def __init__(self, fw_type):
        self.fw_type = fw_type
        self.path = None
        self.size = 0
        self.uploaded_filename = None  # 已上传到服务器的文件名
        self.file_btn = None
        self.upload_btn = None
        self.path_label = None
        self.upload_progress = None
        self.version_edit = None
        self.md5_edit = None

    def is_used(self):
        return bool(self.path)

    def is_uploaded(self):
        """是否已上传到固件服务器"""
        return bool(self.uploaded_filename)

    def is_complete(self):
        return bool(self.uploaded_filename
                    and self.version_edit and self.version_edit.text().strip()
                    and self.md5_edit and self.md5_edit.text().strip())


class FirmwareUpgradeDialog(QDialog):
    upgrade_finished_signal = pyqtSignal(str, bool)  # (sn, success) 单台设备升级结束

    def __init__(self, devices, firmware_server, config, parent=None):
        super().__init__(parent)
        self.devices = devices
        self.firmware_server = firmware_server
        self.config = config
        self.workers = {}          # sn -> UpgradeWorker
        self.upload_workers = {}   # fw_type -> UploadWorker
        self.device_rows = {}      # sn -> (checkbox, progress_bar)
        self.slots = {
            'kernel': _FirmwareSlot('kernel'),
            'rootfs': _FirmwareSlot('rootfs'),
        }
        # 记录升级结果统计
        self.upgrade_results = {}  # sn -> success (bool)

        # 清空固件缓存目录，避免设备下载到上次遗留的旧固件
        if self.firmware_server:
            self.firmware_server.clear_firmware_cache()

        self.setWindowTitle('固件升级')
        self.resize(760, 720)
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
        self.device_table.setMinimumHeight(160)
        dev_layout.addWidget(self.device_table)
        layout.addWidget(dev_group)

        # ==== 固件区 ====
        fw_group = QGroupBox('2. 上传固件 (kernel / rootfs 可单独或同时升级)')
        fw_layout = QGridLayout(fw_group)
        fw_layout.setSpacing(8)
        self._build_slot_row(fw_layout, 0, 'kernel', 'Kernel 固件')
        self._build_slot_row(fw_layout, 5, 'rootfs', 'Rootfs 固件')
        layout.addWidget(fw_group)

        # ==== 参数与操作区 ====
        opt_group = QGroupBox('3. 升级参数')
        opt_layout = QHBoxLayout(opt_group)
        opt_layout.addWidget(QLabel('设备下载超时(秒):'))
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(60, 7200)
        self.timeout_spin.setValue(1800)
        self.timeout_spin.setToolTip('设备使用wget下载固件的超时时间，默认1800秒(30分钟)')
        opt_layout.addWidget(self.timeout_spin)
        opt_layout.addStretch()
        self.start_btn = QPushButton('开始升级')
        self.start_btn.setStyleSheet(
            "QPushButton{background-color:#27ae60;color:white;font-weight:bold;"
            "padding:8px 20px;border-radius:4px;}"
            "QPushButton:hover{background-color:#229954;}"
            "QPushButton:disabled{background-color:#bdc3c7;}")
        self.start_btn.clicked.connect(self._on_start)
        opt_layout.addWidget(self.start_btn)
        layout.addWidget(opt_group)

        # ==== 日志区 ====
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(
            "QTextEdit{background-color:#2c3e50;color:#ecf0f1;"
            "font-family:Consolas,Monaco,monospace;font-size:12px;}")
        self.log_text.setMinimumHeight(160)
        layout.addWidget(self.log_text)

    def _build_slot_row(self, grid, base_row, fw_type, title):
        slot = self.slots[fw_type]
        grid.addWidget(QLabel(f'<b>{title}</b>'), base_row, 0, 1, 4)

        slot.file_btn = QPushButton('选择文件')
        slot.file_btn.setFixedWidth(100)
        slot.file_btn.clicked.connect(lambda: self._on_pick_file(fw_type))
        grid.addWidget(slot.file_btn, base_row + 1, 0)

        slot.upload_btn = QPushButton('上传')
        slot.upload_btn.setEnabled(False)
        slot.upload_btn.setFixedWidth(80)
        slot.upload_btn.setStyleSheet("QPushButton:enabled{background-color:#3498db;color:white;padding:4px;}"
                                       "QPushButton:disabled{background-color:#bdc3c7;}")
        slot.upload_btn.clicked.connect(lambda: self._on_upload_file(fw_type))
        grid.addWidget(slot.upload_btn, base_row + 1, 1)

        slot.path_label = QLabel('未选择')
        slot.path_label.setStyleSheet('color:#7f8c8d;')
        slot.path_label.setWordWrap(False)
        slot.path_label.setMinimumWidth(200)
        slot.path_label.setMaximumWidth(400)
        from PyQt5.QtCore import Qt as QtCore_Qt
        slot.path_label.setTextFormat(QtCore_Qt.PlainText)
        slot.path_label.setTextInteractionFlags(QtCore_Qt.TextSelectableByMouse)
        grid.addWidget(slot.path_label, base_row + 1, 2, 1, 2)

        # 上传进度条
        slot.upload_progress = QProgressBar()
        slot.upload_progress.setRange(0, 100)
        slot.upload_progress.setValue(0)
        slot.upload_progress.setVisible(False)
        slot.upload_progress.setMaximumHeight(20)
        grid.addWidget(slot.upload_progress, base_row + 2, 0, 1, 4)

        grid.addWidget(QLabel('版本:'), base_row + 3, 0)
        slot.version_edit = QLineEdit()
        slot.version_edit.setPlaceholderText('如 1.0.0.9')
        slot.version_edit.setFixedWidth(120)
        grid.addWidget(slot.version_edit, base_row + 3, 1)

        grid.addWidget(QLabel('MD5:'), base_row + 3, 2)
        slot.md5_edit = QLineEdit()
        slot.md5_edit.setPlaceholderText('固件MD5(按输入原样传递)')
        grid.addWidget(slot.md5_edit, base_row + 3, 3)

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

    # ==================== 固件文件 ====================
    def _on_pick_file(self, fw_type):
        path, _ = QFileDialog.getOpenFileName(self, f'选择{fw_type}固件', '', '所有文件 (*.*)')
        if not path:
            return
        slot = self.slots[fw_type]
        slot.path = path
        slot.size = os.path.getsize(path)
        slot.uploaded_filename = None  # 重新选择文件，清除上传状态
        size_mb = slot.size / (1024 * 1024)
        slot.path_label.setText(f'{os.path.basename(path)}  ({slot.size} 字节 / {size_mb:.2f} MB)')
        slot.path_label.setStyleSheet('color:#e67e22;')  # 橙色表示待上传
        slot.upload_btn.setEnabled(True)
        slot.upload_progress.setVisible(False)
        slot.upload_progress.setValue(0)

    def _on_upload_file(self, fw_type):
        """上传固件到托管服务器"""
        slot = self.slots[fw_type]
        if not slot.path:
            return

        if not self.firmware_server or not self.firmware_server.httpd:
            QMessageBox.critical(self, '错误', '固件HTTP服务未启动(端口8000)，无法托管固件')
            return

        # 如果已有上传任务在运行，先停止
        if fw_type in self.upload_workers:
            old_worker = self.upload_workers[fw_type]
            if old_worker.isRunning():
                old_worker.wait()

        # 禁用按钮，显示进度条
        slot.upload_btn.setEnabled(False)
        slot.file_btn.setEnabled(False)
        slot.upload_progress.setVisible(True)
        slot.upload_progress.setValue(0)

        filename = os.path.basename(slot.path)
        # 如果文件名太长，只显示前30字符
        display_name = filename if len(filename) <= 35 else filename[:32] + '...'
        slot.path_label.setText(f'{display_name} - 上传中...')
        slot.path_label.setStyleSheet('color:#3498db;')

        # 创建上传Worker
        worker = FirmwareUploadWorker(slot.path, self.firmware_server.serve_dir, slot.size)
        worker.progress_signal.connect(lambda p: slot.upload_progress.setValue(p))
        worker.success_signal.connect(lambda fname: self._on_upload_success(fw_type, fname))
        worker.failed_signal.connect(lambda err: self._on_upload_failed(fw_type, err))
        self.upload_workers[fw_type] = worker
        worker.start()

    def _on_upload_success(self, fw_type, filename):
        """上传成功回调"""
        slot = self.slots[fw_type]
        slot.uploaded_filename = filename
        slot.upload_progress.setValue(100)

        # 如果文件名太长，只显示前30字符
        display_name = filename if len(filename) <= 35 else filename[:32] + '...'
        slot.path_label.setText(f'{display_name} - ✅ 已上传')
        slot.path_label.setStyleSheet('color:#27ae60;')  # 绿色表示已上传
        slot.file_btn.setEnabled(True)
        logger.info(f"固件已上传: {filename}")
        QMessageBox.information(self, '上传成功', f'{fw_type} 固件上传完成\n{filename}')

    def _on_upload_failed(self, fw_type, error_msg):
        """上传失败回调"""
        slot = self.slots[fw_type]
        slot.upload_progress.setVisible(False)

        filename = os.path.basename(slot.path)
        display_name = filename if len(filename) <= 35 else filename[:32] + '...'
        slot.path_label.setText(f'{display_name} - ❌ 上传失败')
        slot.path_label.setStyleSheet('color:#e74c3c;')
        slot.upload_btn.setEnabled(True)
        slot.file_btn.setEnabled(True)
        logger.error(f"上传固件失败: {error_msg}")
        QMessageBox.critical(self, '上传失败', f'{fw_type} 固件上传失败\n{error_msg}')

    # ==================== 开始升级 ====================
    def _on_start(self):
        selected = self._selected_devices()
        if not selected:
            QMessageBox.warning(self, '提示', '请至少选择一个设备')
            return

        used_slots = [s for s in self.slots.values() if s.is_used()]
        if not used_slots:
            QMessageBox.warning(self, '提示', '请至少选择一个固件(kernel或rootfs)')
            return

        # 检查是否所有固件都已上传
        not_uploaded = [s.fw_type for s in used_slots if not s.is_uploaded()]
        if not_uploaded:
            QMessageBox.warning(self, '提示',
                f'以下固件尚未上传，请先点击"上传"按钮：\n' + ', '.join(not_uploaded))
            return

        for slot in used_slots:
            if not slot.is_complete():
                QMessageBox.warning(self, '提示', f'{slot.fw_type} 固件缺少版本或MD5，请补全')
                return

        if not self.firmware_server or not self.firmware_server.httpd:
            QMessageBox.critical(self, '错误', '固件HTTP服务未启动(端口8000)，无法托管固件')
            return

        # 组装 images 字符串（使用已上传的固件）
        server_ip = self.firmware_server.get_server_ip()
        timeout = self.timeout_spin.value()
        config_str = f"wget_server=http://{server_ip}:8000,timeout={timeout}"

        image_parts = []
        targets = {}
        for slot in used_slots:
            # 使用已上传的文件名，不再重复托管
            filename = slot.uploaded_filename
            version = slot.version_edit.text().strip()
            md5 = slot.md5_edit.text().strip()
            # 顺序: 版本,类型,md5,文件大小,文件名
            image_parts.append(f"{version},{slot.fw_type},{md5},{slot.size},{filename}")
            targets[slot.fw_type] = version
        images_str = ";".join(image_parts)

        self.log_text.append("=" * 50)
        self.log_text.append(f"开始升级 {len(selected)} 个设备")
        self.log_text.append(f"固件服务器: http://{server_ip}:8000")

        self.start_btn.setEnabled(False)
        self.select_all_cb.setEnabled(False)

        # 清空升级结果统计
        self.upgrade_results.clear()

        # 重置进度并逐设备启动Worker
        for device in selected:
            checkbox, progress = self.device_rows[device.sn]
            checkbox.setEnabled(False)
            progress.setValue(0)
            # 升级中设置为橙色
            progress.setStyleSheet("QProgressBar::chunk{background-color:#e67e22;}")

            worker = FirmwareUpgradeWorker(device, config_str, images_str, dict(targets), self.config)
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

    def _on_worker_finished(self, sn, success, reason):
        row = self.device_rows.get(sn)
        if row:
            checkbox, progress = row
            checkbox.setEnabled(True)
            if success:
                progress.setValue(100)
                # 升级成功 - 绿色
                progress.setStyleSheet("QProgressBar::chunk{background-color:#27ae60;}")
            else:
                # 升级失败 - 红色
                progress.setStyleSheet("QProgressBar::chunk{background-color:#e74c3c;}")
        status = "✅ 升级成功" if success else f"❌ 升级失败: {reason}"
        self.log_text.append(f"[{sn}] {status}")

        # 记录升级结果
        self.upgrade_results[sn] = success

        self.workers.pop(sn, None)
        # 通知父窗口该设备升级已结束（无论成败）
        self.upgrade_finished_signal.emit(sn, success)

        # 全部完成后恢复按钮并显示结果弹窗
        if not self.workers:
            self.start_btn.setEnabled(True)
            self.select_all_cb.setEnabled(True)
            self.log_text.append("=" * 50)
            self.log_text.append("全部设备升级流程结束")

            # 统计升级结果
            total_count = len(self.upgrade_results)
            success_count = sum(1 for s in self.upgrade_results.values() if s)
            failed_count = total_count - success_count

            # 只有全部设备都升级成功时，才弹出提示弹窗
            if total_count > 0 and success_count == total_count:
                # 使用 QTimer 延迟显示弹窗，避免与主窗口的设备列表更新冲突
                QTimer.singleShot(100, lambda: QMessageBox.information(
                    self.parent(),  # 使用主窗口作为父窗口，避免模态遮罩渲染错误
                    '固件升级完成',
                    f'✅ 所有设备固件升级成功 (共{total_count}台)\n\n'
                    f'⚠️ 重要提示：\n'
                    f'固件升级完成后4分钟内，请勿执行以下操作：\n'
                    f'  • 音箱掉电\n'
                    f'  • 音箱重启\n\n'
                    f'否则可能导致固件损坏，请耐心等待。'
                ))
            elif failed_count > 0:
                # 有失败的设备，不弹窗，仅在日志中记录
                self.log_text.append(f"升级结果: 成功 {success_count} 台, 失败 {failed_count} 台")

    def closeEvent(self, event):
        running = [sn for sn, w in self.workers.items() if w.isRunning()]
        if running:
            reply = QMessageBox.question(
                self, '确认', f'仍有 {len(running)} 个设备正在升级，确定关闭？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                event.ignore()
                return
            for w in self.workers.values():
                w.quit()
                w.wait(2000)
        event.accept()

