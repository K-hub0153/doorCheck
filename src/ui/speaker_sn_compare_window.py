from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from ..network.device_info import DEVICE_TYPE_OUTDOOR_SMART_HORN, DEVICE_TYPE_SMART_HORN, DeviceInfo
from ..network.http_server import ConfigServer
from ..network.mdns_discovery import SpeakerDiscovery
from ..network.mqtt_broker import MQTTBrokerManager
from ..utils.config import Config
from ..utils.logger import logger


class ScannerInputLineEdit(QLineEdit):
    _unshifted_punctuation = {
        Qt.Key_Minus: '-',
        Qt.Key_Equal: '=',
        Qt.Key_BracketLeft: '[',
        Qt.Key_BracketRight: ']',
        Qt.Key_Backslash: '\\',
        Qt.Key_Semicolon: ';',
        Qt.Key_Apostrophe: "'",
        Qt.Key_Comma: ',',
        Qt.Key_Period: '.',
        Qt.Key_Slash: '/',
        Qt.Key_QuoteLeft: '`',
    }
    _shifted_punctuation = {
        Qt.Key_Minus: '_',
        Qt.Key_Equal: '+',
        Qt.Key_BracketLeft: '{',
        Qt.Key_BracketRight: '}',
        Qt.Key_Backslash: '|',
        Qt.Key_Semicolon: ':',
        Qt.Key_Apostrophe: '"',
        Qt.Key_Comma: '<',
        Qt.Key_Period: '>',
        Qt.Key_Slash: '?',
        Qt.Key_QuoteLeft: '~',
    }
    _shifted_digits = ')!@#$%^&*('

    def keyPressEvent(self, event):
        key = event.key()
        shifted = bool(event.modifiers() & Qt.ShiftModifier)
        char = None

        if Qt.Key_A <= key <= Qt.Key_Z:
            char = chr(key if shifted else key + (ord('a') - ord('A')))
        elif Qt.Key_0 <= key <= Qt.Key_9:
            digit = key - Qt.Key_0
            char = self._shifted_digits[digit] if shifted else str(digit)
        elif key in self._unshifted_punctuation:
            char = (self._shifted_punctuation if shifted else self._unshifted_punctuation)[key]

        if char is not None:
            self.insert(char)
            event.accept()
            return

        super().keyPressEvent(event)


class SpeakerSnCompareWindow(QMainWindow):
    device_found_signal = pyqtSignal(object)
    device_removed_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.devices = {}
        self.discovery_service = None
        self.config_server = None
        self.mqtt_broker = None
        self._selected_sn = None
        self._confirmed_scans = {}
        self._closing = False
        self.device_found_signal.connect(self._add_device)
        self.device_removed_signal.connect(self._remove_device)
        self._init_ui()
        self._start_discovery()

    def _init_ui(self):
        self.setWindowTitle('音箱设备序列号比对工具')
        self.resize(760, 560)
        self.setMinimumSize(620, 460)
        self.setStyleSheet('''
            QMainWindow, QWidget { background: #F5F7FA; }
            QLabel { color: #263238; font-size: 14px; }
            QComboBox, QLineEdit { background: white; border: 1px solid #CFD8DC; border-radius: 6px; padding: 11px; font-size: 15px; }
            QPushButton { background: #1976D2; color: white; border: 0; border-radius: 6px; padding: 11px 22px; font-weight: bold; }
        ''')
        root = QVBoxLayout()
        root.setContentsMargins(48, 38, 48, 38)
        root.setSpacing(20)
        title = QLabel('音箱设备序列号比对')
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont('Microsoft YaHei', 20, QFont.Bold))
        root.addWidget(title)
        root.addWidget(QLabel('选择已发现的设备；机身 SN 与彩盒贴纸 SN 均扫码完成后自动判定。'))

        form = QFormLayout()
        form.setSpacing(18)
        self.device_combo = QComboBox()
        self.device_combo.addItem('正在搜索室内/室外音箱…', None)
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        form.addRow('发现的音箱：', self.device_combo)
        self.device_sn_label = QLabel('等待发现设备')
        self.device_sn_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow('设备序列号 SN：', self.device_sn_label)
        self.body_sn_input = ScannerInputLineEdit()
        self.body_sn_input.setFocusPolicy(Qt.StrongFocus)
        self.body_sn_input.setInputMethodHints(Qt.ImhLatinOnly | Qt.ImhNoPredictiveText)
        self.body_sn_input.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.body_sn_input.setPlaceholderText('请扫描音箱机身 SN，扫码后按回车')
        self.body_sn_input.setClearButtonEnabled(True)
        self.body_sn_input.textChanged.connect(lambda: self._scan_edited(self.body_sn_input))
        self.body_sn_input.returnPressed.connect(lambda: self._accept_scan(self.body_sn_input))
        form.addRow('机身 SN：', self.body_sn_input)
        self.box_sn_input = ScannerInputLineEdit()
        self.box_sn_input.setFocusPolicy(Qt.StrongFocus)
        self.box_sn_input.setInputMethodHints(Qt.ImhLatinOnly | Qt.ImhNoPredictiveText)
        self.box_sn_input.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.box_sn_input.setPlaceholderText('请扫描彩盒贴纸 SN，扫码后按回车')
        self.box_sn_input.setClearButtonEnabled(True)
        self.box_sn_input.textChanged.connect(lambda: self._scan_edited(self.box_sn_input))
        self.box_sn_input.returnPressed.connect(lambda: self._accept_scan(self.box_sn_input))
        form.addRow('彩盒贴纸 SN：', self.box_sn_input)
        root.addLayout(form)

        self.result_label = QLabel('等待扫描：设备 SN、机身 SN、彩盒贴纸 SN')
        self.result_label.setAlignment(Qt.AlignCenter)
        self.result_label.setMinimumHeight(100)
        self.result_label.setFont(QFont('Microsoft YaHei', 22, QFont.Bold))
        self.result_label.setStyleSheet('background: white; border-radius: 10px; color: #607D8B;')
        root.addWidget(self.result_label)
        self.reset_button = QPushButton('清空本次扫码')
        self.reset_button.clicked.connect(self._reset_scans)
        root.addWidget(self.reset_button, alignment=Qt.AlignRight)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)
        self._device_changed()

    def _start_discovery(self):
        try:
            if self.config.broker_mode == 'local':
                self.mqtt_broker = MQTTBrokerManager(
                    host='0.0.0.0', port=self.config.mqtt_port, ssl_enabled=True,
                )
                self.mqtt_broker.start()
            self.config_server = ConfigServer(
                host='0.0.0.0',
                port=self.config.http_port,
                mqtt_broker=self.config.mqtt_broker,
                mqtt_port=self.config.mqtt_port,
                secret_key=self.config.device_psk,
                broker_mode=self.config.broker_mode,
            )
            self.config_server.start()
            self.discovery_service = SpeakerDiscovery(
                service_type=self.config.mdns_service_type,
                http_port=self.config.http_port,
                on_device_found=lambda device: self.device_found_signal.emit(device),
                on_device_removed=lambda sn: self.device_removed_signal.emit(sn),
                device_types=[DEVICE_TYPE_SMART_HORN, DEVICE_TYPE_OUTDOOR_SMART_HORN],
            )
        except Exception as exc:
            logger.error(f'音箱设备发现启动失败: {exc}', exc_info=True)
            QMessageBox.warning(self, '设备发现失败', f'无法启动音箱设备发现：{exc}')

    def _add_device(self, device: DeviceInfo):
        if self._closing:
            return
        self.devices[device.sn] = device
        self._refresh_device_list()

    def _remove_device(self, sn):
        if self._closing:
            return
        self.devices.pop(sn, None)
        self._refresh_device_list()

    def _refresh_device_list(self):
        selected = self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if not self.devices:
            self.device_combo.addItem('未发现音箱，请确认设备与网络', None)
        else:
            for device in sorted(self.devices.values(), key=lambda item: item.sn):
                kind = '室外音箱' if device.type == DEVICE_TYPE_OUTDOOR_SMART_HORN else '室内音箱'
                self.device_combo.addItem(f'{kind} | {device.sn} | {device.ip}', device)
            index = next((i for i in range(self.device_combo.count()) if selected and self.device_combo.itemData(i).sn == selected.sn), 0)
            self.device_combo.setCurrentIndex(index)
        self.device_combo.blockSignals(False)
        self._device_changed()

    def _device_changed(self, *_):
        device = self.device_combo.currentData()
        self.device_sn_label.setText(device.sn if device else '等待发现设备')
        self.body_sn_input.setEnabled(device is not None)
        self.box_sn_input.setEnabled(device is not None)
        selected_sn = device.sn if device else None
        if selected_sn != self._selected_sn:
            self._selected_sn = selected_sn
            self._reset_scans()
            if device:
                QTimer.singleShot(0, self.body_sn_input.setFocus)
        self._evaluate()

    def _scan_edited(self, field):
        self._confirmed_scans.pop(field, None)
        self._evaluate()

    def _accept_scan(self, field):
        field.setText(field.text().strip())
        if field.text():
            self._confirmed_scans[field] = field.text()
            self._evaluate()
            if field is self.body_sn_input:
                self.box_sn_input.setFocus()
                self.box_sn_input.selectAll()

    def showEvent(self, event):
        super().showEvent(event)
        if self.device_combo.currentData():
            QTimer.singleShot(0, self.body_sn_input.setFocus)

    def _evaluate(self):
        device = self.device_combo.currentData()
        body_sn = self._confirmed_scans.get(self.body_sn_input)
        box_sn = self._confirmed_scans.get(self.box_sn_input)
        if not device or not body_sn or not box_sn:
            self.result_label.setText('等待扫描：设备 SN、机身 SN、彩盒贴纸 SN')
            self.result_label.setStyleSheet('background: white; border-radius: 10px; color: #607D8B;')
            return
        passed = device.sn == body_sn == box_sn
        self.result_label.setText('PASS  三个序列号一致' if passed else 'FAIL  序列号不一致')
        color = '#2E7D32' if passed else '#C62828'
        self.result_label.setStyleSheet(f'background: white; border: 2px solid {color}; border-radius: 10px; color: {color};')

    def _reset_scans(self):
        self._confirmed_scans.clear()
        self.body_sn_input.clear()
        self.box_sn_input.clear()
        self._evaluate()
        if self.body_sn_input.isEnabled() and self.isVisible():
            QTimer.singleShot(0, self.body_sn_input.setFocus)

    def closeEvent(self, event):
        self._closing = True
        for service, method in (
            (self.discovery_service, 'close'),
            (self.config_server, 'stop'),
            (self.mqtt_broker, 'stop'),
        ):
            if service:
                try:
                    getattr(service, method)()
                except Exception as exc:
                    logger.warning(f'关闭音箱接入服务失败: {exc}')
        event.accept()
