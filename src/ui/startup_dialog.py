from PyQt5.QtWidgets import QDialog, QVBoxLayout, QPushButton, QLabel, QGridLayout
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QIcon
import os
import sys


class StartupDialog(QDialog):
    """启动选择对话框"""

    def __init__(self):
        super().__init__()
        self.selected_mode = None
        self.init_ui()

    def _get_icon_path(self):
        """获取图标路径"""
        if getattr(sys, 'frozen', False):
            return os.path.join(sys._MEIPASS, 'vdian.ico')
        else:
            return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'src', 'ui', 'icon',
                                'vdian.ico')

    def init_ui(self):
        self.setWindowTitle('智能设备产测工具 - 选择模式')
        self.setFixedSize(760, 600)

        # 设置窗口图标
        icon_path = self._get_icon_path()
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # 设置对话框样式
        self.setStyleSheet("""
            QDialog {
                background-color: #F5F7FA;
            }
            QPushButton {
                background-color: #1976D2;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 20px;
                font-size: 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1565C0;
            }
            QPushButton:pressed {
                background-color: #0D47A1;
            }
        """)

        layout = QVBoxLayout()
        layout.setSpacing(16)
        layout.setContentsMargins(32, 28, 32, 28)

        # 标题
        title = QLabel('智能设备产测工具')
        title.setFont(QFont('Microsoft YaHei', 14, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet('color: #1976D2; margin-bottom: 10px;')
        layout.addWidget(title)

        # 副标题
        subtitle = QLabel('请选择产测工具类型')
        subtitle.setFont(QFont('Microsoft YaHei', 9))
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet('color: #757575; margin-bottom: 20px;')
        layout.addWidget(subtitle)

        tools_layout = QGridLayout()
        tools_layout.setHorizontalSpacing(16)
        tools_layout.setVerticalSpacing(16)
        tools = (
            ('🖥️  主控设备产测工具', 'master_control'),
            ('🚪  智能门控产测工具', 'door'),
            ('🏠  智能室内音箱产测工具', 'speaker_indoor'),
            ('🌳  智能室外音箱产测工具', 'speaker_outdoor'),
            ('📦  扫码打印工具', 'qrcode_print'),
            ('🔎  音箱设备序列号比对工具', 'speaker_sn_compare'),
        )
        for index, (label, mode) in enumerate(tools):
            button = QPushButton(label)
            button.setFont(QFont('Microsoft YaHei', 12))
            button.setMinimumHeight(88)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, selected=mode: self.select_mode(selected))
            tools_layout.addWidget(button, index // 2, index % 2)
        layout.addLayout(tools_layout)

        self.setLayout(layout)

    def select_mode(self, mode):
        """选择模式并关闭对话框"""
        self.selected_mode = mode
        self.accept()
