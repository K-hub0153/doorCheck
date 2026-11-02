import platform

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ..network.speaker_sn_connection import (
    SPEAKER_TYPES, SpeakerConnectionWorker, compare_serial_numbers,
)
from ..utils.config import Config


class SpeakerSNCompareWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.identity = None
        self.epoch = None
        self.body_confirmed = False
        self.box_confirmed = False
        self.closing = False
        self._init_ui()
        allow_direct_dhcp = False
        if platform.system() == 'Windows':
            answer = QMessageBox.question(
                self,
                'RJ45 直连 DHCP',
                '如果 RJ45 没有 IPv4，工具将临时把该网卡设为 172.31.250.1，启动仅绑定此网卡的 DHCP 服务，退出时恢复为自动获取 IP。\n\n'
                '请仅在 RJ45 直连一台音箱时继续；不要连接交换机或办公网络。允许配置吗？',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            allow_direct_dhcp = answer == QMessageBox.Yes
            if not allow_direct_dhcp:
                self._set_result('未允许 DHCP 配置；只会监听现有以太网地址')
        self.worker = SpeakerConnectionWorker(
            Config().mdns_service_type, allow_direct_dhcp=allow_direct_dhcp, parent=self)
        self.worker.state_changed.connect(self._connection_state)
        self.worker.start()

    def _init_ui(self):
        self.setWindowTitle('音箱设备序列号比对工具')
        self.resize(720, 500)
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(36, 30, 36, 30)
        layout.setSpacing(18)

        title = QLabel('音箱设备序列号比对')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet('font-size: 24px; font-weight: bold; color: #1976D2;')
        layout.addWidget(title)

        self.network_label = QLabel('正在查找以太网接口…')
        self.network_label.setWordWrap(True)
        self.network_label.setStyleSheet('color: #475467; font-size: 14px;')
        layout.addWidget(self.network_label)

        self.status_label = QLabel('正在等待音箱广播…')
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(48)
        self.status_label.setStyleSheet('padding: 10px; background: #F2F4F7; border-radius: 6px;')
        layout.addWidget(self.status_label)

        device_form = QFormLayout()
        self.device_sn_label = QLabel('等待设备识别')
        self.device_type_label = QLabel('—')
        self.device_ip_label = QLabel('—')
        device_form.addRow('设备 SN', self.device_sn_label)
        device_form.addRow('音箱类型', self.device_type_label)
        device_form.addRow('设备 IP', self.device_ip_label)
        layout.addLayout(device_form)

        self.body_edit = QLineEdit()
        self.body_edit.setPlaceholderText('请扫描音箱机身 SN')
        self.body_edit.setMinimumHeight(48)
        self.box_edit = QLineEdit()
        self.box_edit.setPlaceholderText('请扫描彩盒贴纸 SN')
        self.box_edit.setMinimumHeight(48)
        self.body_edit.setEnabled(False)
        self.box_edit.setEnabled(False)
        self.body_edit.textChanged.connect(self._body_changed)
        self.box_edit.textChanged.connect(self._box_changed)
        self.body_edit.returnPressed.connect(self._submit_body)
        self.body_edit.editingFinished.connect(self._submit_body)
        self.box_edit.returnPressed.connect(self._submit_box)
        self.box_edit.editingFinished.connect(self._submit_box)
        layout.addWidget(self.body_edit)
        layout.addWidget(self.box_edit)

        result_row = QHBoxLayout()
        self.result_label = QLabel('等待设备识别及扫码')
        self.result_label.setAlignment(Qt.AlignCenter)
        self.result_label.setStyleSheet('font-size: 23px; font-weight: bold; color: #667085;')
        result_row.addWidget(self.result_label, 1)
        self.reset_button = QPushButton('清空扫码 / 下一台')
        self.reset_button.clicked.connect(self._reset_scans)
        result_row.addWidget(self.reset_button)
        layout.addLayout(result_row)

        hint = QLabel('扫码枪请设置回车或 Tab 后缀；机身 SN、彩盒 SN 均扫描完成后自动比对。')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #667085;')
        layout.addWidget(hint)
        self.setCentralWidget(root)

    def _set_result(self, text, color='#667085'):
        self.result_label.setText(text)
        self.result_label.setStyleSheet(
            f'font-size: 23px; font-weight: bold; color: {color};')

    def _clear_scans(self):
        self.body_confirmed = False
        self.box_confirmed = False
        self.body_edit.blockSignals(True)
        self.box_edit.blockSignals(True)
        self.body_edit.clear()
        self.box_edit.clear()
        self.body_edit.blockSignals(False)
        self.box_edit.blockSignals(False)
        self._set_result('等待机身 SN 和彩盒 SN 扫码')

    def _body_changed(self):
        self.body_confirmed = False
        self.box_confirmed = False
        self.box_edit.clear()
        self._set_result('等待扫描完成')

    def _box_changed(self):
        self.box_confirmed = False
        self._set_result('等待彩盒 SN 扫描完成')

    def _submit_body(self):
        self.body_confirmed = bool(self.body_edit.text().strip())
        if self.body_confirmed:
            self.box_edit.setFocus()
            self.box_edit.selectAll()
            self._compare_if_ready()

    def _submit_box(self):
        self.box_confirmed = bool(self.box_edit.text().strip())
        self._compare_if_ready()

    def _compare_if_ready(self):
        if not self.identity or not self.body_confirmed or not self.box_confirmed:
            return
        body_sn = self.body_edit.text().strip()
        box_sn = self.box_edit.text().strip()
        if compare_serial_numbers(self.identity.sn, body_sn, box_sn):
            self._set_result('PASS · 三码一致', '#16803C')
        else:
            failures = []
            if body_sn != self.identity.sn:
                failures.append('机身 SN')
            if box_sn != self.identity.sn:
                failures.append('彩盒贴纸 SN')
            self._set_result('FAIL · ' + '、'.join(failures) + ' 不一致', '#C62828')

    def _reset_scans(self):
        self._clear_scans()
        if self.identity:
            self.body_edit.setFocus()

    def _connection_state(self, state):
        devices, message, epoch, interface_name, addresses = state
        if epoch != self.epoch:
            self.epoch = epoch
            self._clear_scans()
        if interface_name:
            address_text = ', '.join(addresses) if addresses else '尚无 IPv4 地址'
            self.network_label.setText(f'监听网卡：{interface_name} · {address_text}')
        else:
            self.network_label.setText('未找到已连接的物理以太网；不会扫描 Wi-Fi。')
        self.status_label.setText(message)

        identity = devices[0] if len(devices) == 1 else None
        if identity != self.identity:
            self.identity = identity
            self._clear_scans()
        self.device_sn_label.setText(identity.sn if identity else '等待设备识别')
        self.device_type_label.setText(SPEAKER_TYPES.get(identity.device_type, '未知')
                                       if identity else '—')
        self.device_ip_label.setText(identity.ip if identity else '—')
        self.body_edit.setEnabled(identity is not None)
        self.box_edit.setEnabled(identity is not None)
        if '多台' in message:
            self._set_result('发现多台设备，不能比对', '#C62828')
        elif not identity:
            self._set_result('等待设备识别，不能比对')

    def closeEvent(self, event):
        self.closing = True
        self.worker.stop()
        if self.worker.isRunning():
            self.status_label.setText('正在停止 DHCP 并恢复 RJ45 网络配置，请稍候…')
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        if self.worker.cleanup_error:
            try:
                if self.worker.active_provisioner:
                    self.worker.active_provisioner.restore()
                    self.worker.cleanup_error = None
                else:
                    QMessageBox.critical(
                        self, '网络配置恢复失败',
                        f'{self.worker.cleanup_error}\n\n恢复记录已保留：请联系管理员检查 RJ45 网络配置。')
                    event.accept()
                    return
            except Exception as error:
                QMessageBox.critical(
                    self, '网络配置恢复失败',
                    f'{error}\n\n恢复记录已保留，请联系管理员恢复 RJ45 的 DHCP 设置后再关闭工具。')
                event.ignore()
                self.closing = False
                return
        event.accept()
