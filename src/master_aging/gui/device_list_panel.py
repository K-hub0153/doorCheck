"""独立设备卡片：勾选用于批量操作，点击卡片用于查看详情。"""
from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QScrollArea, QToolButton,
                             QVBoxLayout, QWidget)
from .styles import STATES, status_color


def action_icon(kind):
    """使用 Qt 绘制线性图标，保持不同平台和高分屏上的显示一致。"""
    icon = QIcon()
    for mode, color in ((QIcon.Normal, '#78909c'),
                        (QIcon.Active, '#e53935' if kind == 'delete' else '#1976d2'),
                        (QIcon.Disabled, '#bdc9ce')):
        pixmap = QPixmap(32, 32)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(color), 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if kind == 'copy':
            painter.drawRoundedRect(6, 6, 8, 8, 1, 1)
            painter.drawLine(3, 10, 3, 3)
            painter.drawLine(3, 3, 10, 3)
        else:
            painter.drawLine(2, 4, 14, 4)
            painter.drawLine(6, 2, 10, 2)
            painter.drawLine(4, 6, 4, 14)
            painter.drawLine(4, 14, 12, 14)
            painter.drawLine(12, 14, 12, 6)
            painter.drawLine(7, 7, 7, 11)
            painter.drawLine(9, 7, 9, 11)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


class DeviceCard(QFrame):
    activated = pyqtSignal(str)
    selection_changed = pyqtSignal()
    remove_requested = pyqtSignal(str)
    copied = pyqtSignal(str)

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
        self.check.setToolTip('勾选后可批量开始、停止或删除设备')
        self.check.toggled.connect(self.selection_changed)
        row.addWidget(self.check)
        self.sn = QLabel()
        self.sn.setTextFormat(Qt.PlainText)
        self.sn.setWordWrap(True)
        self.sn.setObjectName('cardSn')
        row.addWidget(self.sn, 1)
        row.setSpacing(4)
        self.copy_button = self.action_button('copy', '复制 SN')
        self.copy_button.clicked.connect(self.copy_sn)
        row.addWidget(self.copy_button)
        self.delete_button = self.action_button('delete', '从列表删除设备')
        self.delete_button.clicked.connect(lambda: self.remove_requested.emit(self.ip))
        row.addWidget(self.delete_button)
        layout.addLayout(row)
        self.address = QLabel(('VSM  ' + device['sn']) if self.ip.startswith('vsm:') else 'IP  ' + self.ip)
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

    def action_button(self, kind, label):
        button = QToolButton(self)
        button.setObjectName('deviceAction')
        button.setProperty('action', kind)
        button.setAutoRaise(True)
        button.setFixedSize(26, 26)
        button.setIconSize(QSize(16, 16))
        button.setIcon(action_icon(kind))
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setCursor(Qt.PointingHandCursor)
        return button

    def update_device(self, device):
        self.serial_number = device.get('sn') or ''
        self.copy_button.setEnabled(bool(self.serial_number))
        self.sn.setText(device.get('sn') or '身份待读取')
        self.sn.setToolTip(device.get('discovery_note', ''))
        self.hardware.setText(device.get('hardware_text') or device.get('chip') or device.get('model') or '机型待读取')
        self.storage.setText(device.get('storage_text') or device.get('disk_model') or '内存 / 存储待读取')
        if not device.get('run_id'):
            self.status.setText(device.get('discovery_note') or '待配置')

    def copy_sn(self):
        QApplication.clipboard().setText(self.serial_number)
        self.copied.emit(self.serial_number)

    def update_status(self, status, live='', confirmation_pending=False):
        state, result = status['state'], status.get('result')
        label = STATES.get(state, state)
        if confirmation_pending and state in ('RUNNING', 'WAIT_OPERATOR') and not result:
            label = '电源确认已提交，等待设备响应'
        self.status.setText(label + (' · ' + result if result else ''))
        self.status.setStyleSheet('color:%s;font-weight:bold' % status_color(result or state))
        self.status.setToolTip(status.get('local_log_error') or status.get('error') or status.get('connection_error') or status.get('safety_stop') or status.get('environment_message') or '')
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
    remove_requested = pyqtSignal(str)
    copied = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards, self.current_ip = {}, None
        content = QWidget()
        self.cards_layout = QVBoxLayout(content)
        self.cards_layout.setContentsMargins(8, 8, 8, 8)
        self.cards_layout.setSpacing(10)
        self.empty = QLabel('暂无设备\n\n点击「子网扫描」发现局域网设备\n或在 macOS 点击「手动添加」通过 VSM 接入')
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
        card.remove_requested.connect(self.remove_requested)
        card.copied.connect(self.copied)
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

    def remove(self, ip):
        card = self.cards.pop(ip)
        self.cards_layout.removeWidget(card)
        card.deleteLater()
        if self.current_ip == ip:
            self.current_ip = None
            if self.cards:
                self.activate(next(iter(self.cards)))
        self.empty.setVisible(not self.cards)
        self.selection_changed.emit()
