"""独立设备卡片：勾选用于批量操作，点击卡片用于查看详情。"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QScrollArea,
                             QVBoxLayout, QWidget)
from .styles import STATES, status_color


class DeviceCard(QFrame):
    activated = pyqtSignal(str)
    selection_changed = pyqtSignal()

    def __init__(self, device, parent=None):
        super().__init__(parent)
        self.ip = device['ip']
        self.setObjectName('deviceCard')
        self.setProperty('active', False)
        self.setCursor(Qt.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(7)
        row = QHBoxLayout()
        self.check = QCheckBox()
        self.check.setToolTip('勾选后可批量开始或停止设备')
        self.check.toggled.connect(self.selection_changed)
        row.addWidget(self.check)
        self.sn = QLabel()
        self.sn.setTextFormat(Qt.PlainText)
        self.sn.setWordWrap(True)
        self.sn.setObjectName('cardSn')
        row.addWidget(self.sn, 1)
        layout.addLayout(row)
        self.address = QLabel('IP  ' + self.ip)
        self.hardware = QLabel()
        self.storage = QLabel()
        self.status = QLabel('待配置')
        self.live = QLabel('')
        for label in (self.address, self.hardware, self.storage, self.status, self.live):
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            layout.addWidget(label)
        self.sn.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.address.setObjectName('muted')
        self.storage.setObjectName('muted')
        self.live.setObjectName('muted')
        self.update_device(device)

    def update_device(self, device):
        self.sn.setText(device.get('sn') or '身份待读取')
        self.hardware.setText(device.get('hardware_text') or device.get('chip') or device.get('model') or '机型待读取')
        self.storage.setText(device.get('storage_text') or device.get('disk_model') or '内存 / 存储待读取')

    def update_status(self, status, live=''):
        state, result = status['state'], status.get('result')
        self.status.setText(STATES.get(state, state) + (' · ' + result if result else ''))
        self.status.setStyleSheet('color:%s;font-weight:bold' % status_color(result or state))
        self.status.setToolTip(status.get('error') or status.get('connection_error') or status.get('safety_stop') or '')
        self.live.setText(live)

    def set_active(self, active):
        self.setProperty('active', active)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.activated.emit(self.ip)
        super().mousePressEvent(event)


class DeviceListPanel(QScrollArea):
    activated = pyqtSignal(str)
    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards, self.current_ip = {}, None
        content = QWidget()
        self.cards_layout = QVBoxLayout(content)
        self.cards_layout.setContentsMargins(8, 8, 8, 8)
        self.cards_layout.setSpacing(10)
        self.empty = QLabel('暂无设备\n\n点击「子网扫描」发现局域网设备')
        self.empty.setObjectName('muted')
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.cards_layout.addWidget(self.empty)
        self.cards_layout.addStretch()
        self.setWidget(content)

    def upsert(self, device):
        ip = device['ip']
        if ip in self.cards:
            self.cards[ip].update_device(device)
            return
        self.empty.hide()
        card = DeviceCard(device)
        card.activated.connect(self.activate)
        card.selection_changed.connect(self.selection_changed)
        self.cards[ip] = card
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
        if self.current_ip is None:
            self.activate(ip)

    def activate(self, ip):
        self.current_ip = ip
        for address, card in self.cards.items():
            card.set_active(address == ip)
        self.activated.emit(ip)

    def clear(self):
        for card in self.cards.values():
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self.cards.clear()
        self.current_ip = None
        self.empty.show()
        self.selection_changed.emit()
