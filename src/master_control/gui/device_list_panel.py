from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QFrame, QScrollArea, QGridLayout, QApplication)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from src.master_control.core.device import MasterDevice
from src.master_control.core.test_result import TestStatus
from src.master_control.utils.logger import logger
import re


def extract_disk_info(disk_model: str) -> str:
    """从硬盘型号中提取品牌和标称容量"""
    if not disk_model:
        return "未知"

    # 提取标称容量（如 512GB, 1TB）
    size_match = re.search(r'(\d+)\s*(TB|GB)', disk_model, re.IGNORECASE)
    # 提取品牌（第一个单词）
    brand_match = re.match(r'^(\S+)', disk_model)

    brand = brand_match.group(1) if brand_match else ""
    size = size_match.group(1) + size_match.group(2)[0].upper() if size_match else ""

    if brand and size:
        return f"{brand} {size}"
    return disk_model


class DeviceCard(QFrame):
    
    clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)
    reconnect_requested = pyqtSignal(str)
    
    def __init__(self, device: MasterDevice, parent=None):
        super().__init__(parent)
        self.device = device
        self.selected = False
        self.init_ui()
    
    def init_ui(self):
        self.setFrameStyle(QFrame.Box | QFrame.Raised)
        self.setLineWidth(2)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(120)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)

        sn_layout = QHBoxLayout()
        sn_label = QLabel(f"SN: {self.device.sn}")
        sn_label.setFont(QFont("Arial", 12, QFont.Bold))
        sn_label.setStyleSheet("color: black;")
        sn_layout.addWidget(sn_label)

        copy_btn = QPushButton("复制")
        copy_btn.setFixedSize(45, 20)
        copy_btn.setStyleSheet("background-color: #607D8B; color: white; border-radius: 3px; font-size: 9px;")
        copy_btn.clicked.connect(self.copy_sn)
        sn_layout.addWidget(copy_btn)
        sn_layout.addStretch()

        layout.addLayout(sn_layout)
        
        ip_label = QLabel(f"IP: {self.device.ip}")
        ip_label.setFont(QFont("Arial", 10))
        ip_label.setStyleSheet("color: black;")
        layout.addWidget(ip_label)

        disk_info = extract_disk_info(self.device.disk_model) if self.device.disk_model else "未知"
        model_label = QLabel(f"硬盘: {disk_info}")
        model_label.setFont(QFont("Arial", 10))
        model_label.setStyleSheet("color: black;")
        layout.addWidget(model_label)
        
        status_layout = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setFont(QFont("Arial", 9))
        status_layout.addWidget(self.status_label)
        
        delete_btn = QPushButton("删除")
        delete_btn.setFixedWidth(50)
        delete_btn.setStyleSheet("background-color: #ff6b6b; color: white; border-radius: 3px;")
        delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.device.sn))
        status_layout.addWidget(delete_btn)
        
        layout.addLayout(status_layout)
        
        self.setLayout(layout)
        # 初始化状态显示
        self.update_status(self.device.status)
        self.update_style()
    
    def update_style(self):
        if self.selected:
            self.setStyleSheet("""
                DeviceCard {
                    background-color: #e3f2fd;
                    border: 2px solid #2196F3;
                    border-radius: 8px;
                }
            """)
        else:
            self.setStyleSheet("""
                DeviceCard {
                    background-color: white;
                    border: 1px solid #ddd;
                    border-radius: 8px;
                }
                DeviceCard:hover {
                    background-color: #f5f5f5;
                    border: 1px solid #2196F3;
                }
            """)
    
    def set_selected(self, selected: bool):
        self.selected = selected
        self.update_style()
    
    def update_status(self, status: str):
        self.device.status = status
        
        # 设备连接状态显示
        if status == "discovered":
            self.status_label.setText(f"状态: 已发现")
            self.status_label.setStyleSheet("color: #9E9E9E; font-weight: normal;")
        elif status == "connected":
            self.status_label.setText(f"状态: 已连接")
            self.status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
        elif status == "disconnected":
            self.status_label.setText(f"状态: 已断开")
            self.status_label.setStyleSheet("color: #FF9800; font-weight: normal;")
        # 测试结果状态显示
        elif status == "通过":
            self.status_label.setText(f"状态: {status}")
            self.status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
        elif status == "失败":
            self.status_label.setText(f"状态: {status}")
            self.status_label.setStyleSheet("color: #f44336; font-weight: bold;")
        elif status == "测试中":
            self.status_label.setText(f"状态: {status}")
            self.status_label.setStyleSheet("color: #ff9800; font-weight: bold;")
        else:
            self.status_label.setText(f"状态: {status}")
            self.status_label.setStyleSheet("color: #666;")

    def copy_sn(self):
        QApplication.clipboard().setText(self.device.sn)
    
    def mousePressEvent(self, event):
        self.clicked.emit(self.device.sn)
        super().mousePressEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        """双击事件：断开状态下触发重连"""
        if self.device.status == "disconnected":
            logger.info(f"双击断开设备卡片，请求重连: {self.device.sn}")
            self.reconnect_requested.emit(self.device.sn)
        super().mouseDoubleClickEvent(event)


class DeviceListPanel(QWidget):
    
    device_selected = pyqtSignal(str)
    device_deleted = pyqtSignal(str)
    device_reconnect_requested = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.device_cards = {}
        self.current_selected_card = None
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        
        header = QLabel("设备列表")
        header.setFont(QFont("Arial", 14, QFont.Bold))
        header.setStyleSheet("padding: 10px; background-color: #2196F3; color: white;")
        layout.addWidget(header)
        
        button_layout = QHBoxLayout()
        
        self.refresh_btn = QPushButton("刷新发现")
        self.refresh_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        button_layout.addWidget(self.refresh_btn)
        
        self.scan_btn = QPushButton("子网扫描")
        self.scan_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #0b7dda;
            }
        """)
        button_layout.addWidget(self.scan_btn)
        
        layout.addLayout(button_layout)
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout()
        self.cards_layout.setSpacing(10)
        self.cards_layout.setContentsMargins(10, 10, 10, 10)
        self.cards_layout.addStretch()
        self.cards_container.setLayout(self.cards_layout)
        
        scroll_area.setWidget(self.cards_container)
        layout.addWidget(scroll_area)
        
        self.setLayout(layout)
    
    def add_device(self, device: MasterDevice):
        logger.info(f"设备列表面板接收到添加设备请求: {device.sn}")
        
        if device.sn in self.device_cards:
            logger.info(f"设备已存在，跳过添加: {device.sn}")
            return
        
        logger.info(f"创建设备卡片: {device.sn}")
        card = DeviceCard(device)
        card.clicked.connect(self.on_device_clicked)
        card.delete_clicked.connect(self.on_device_delete)
        card.reconnect_requested.connect(self.on_device_reconnect)
        
        logger.info(f"将设备卡片插入布局: {device.sn}")
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
        self.device_cards[device.sn] = card
        logger.info(f"设备卡片添加完成: {device.sn}, 当前设备数量: {len(self.device_cards)}")
    
    def remove_device(self, sn: str):
        if sn in self.device_cards:
            card = self.device_cards.pop(sn)
            if self.current_selected_card is card:
                self.current_selected_card = None
            self.cards_layout.removeWidget(card)
            card.deleteLater()
    
    def on_device_clicked(self, sn: str):
        if self.current_selected_card:
            self.current_selected_card.set_selected(False)
        
        card = self.device_cards.get(sn)
        if card:
            card.set_selected(True)
            self.current_selected_card = card
        
        self.device_selected.emit(sn)
    
    def on_device_delete(self, sn: str):
        self.device_deleted.emit(sn)
    
    def on_device_reconnect(self, sn: str):
        """转发重连请求信号"""
        logger.info(f"设备列表面板转发重连请求: {sn}")
        self.device_reconnect_requested.emit(sn)
    
    def update_device_status(self, sn: str, status: str):
        if sn in self.device_cards:
            self.device_cards[sn].update_status(status)
    
    def clear_devices(self):
        for card in list(self.device_cards.values()):
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self.device_cards.clear()
        self.current_selected_card = None
