from PyQt5.QtWidgets import QDialog, QVBoxLayout, QPushButton, QLabel
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
            return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'src', 'ui', 'icon', 'vdian.ico')

    def init_ui(self):
        self.setWindowTitle('智能设备产测工具 - 选择模式')
        self.setFixedSize(450, 450)

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
                padding: 15px;
                font-size: 14px;
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
        layout.setSpacing(12)  # 减小间距
        layout.setContentsMargins(30, 30, 30, 30)

        # 标题
        title = QLabel('智能设备产测工具')
        title.setFont(QFont('Microsoft YaHei', 12, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet('color: #1976D2; margin-bottom: 10px;')
        layout.addWidget(title)

        # 副标题
        subtitle = QLabel('请选择产测工具类型')
        subtitle.setFont(QFont('Microsoft YaHei', 8))
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet('color: #757575; margin-bottom: 15px;')
        layout.addWidget(subtitle)

        # 主控按钮，与门控和音箱入口保持相同图标风格
        master_btn = QPushButton('🖥️  主控设备产测工具')
        master_btn.setFont(QFont('Microsoft YaHei', 13))
        master_btn.setMinimumHeight(60)
        master_btn.setCursor(Qt.PointingHandCursor)
        master_btn.clicked.connect(lambda: self.select_mode('master_control'))
        layout.addWidget(master_btn)

        # 门控按钮
        door_btn = QPushButton('🚪  智能门控产测工具')
        door_btn.setFont(QFont('Microsoft YaHei', 13))
        door_btn.setMinimumHeight(60)
        door_btn.setCursor(Qt.PointingHandCursor)
        door_btn.clicked.connect(lambda: self.select_mode('door'))
        layout.addWidget(door_btn)

        # 室内音箱按钮
        indoor_speaker_btn = QPushButton('🏠  智能室内音箱产测工具')
        indoor_speaker_btn.setFont(QFont('Microsoft YaHei', 13))
        indoor_speaker_btn.setMinimumHeight(60)
        indoor_speaker_btn.setCursor(Qt.PointingHandCursor)
        indoor_speaker_btn.clicked.connect(lambda: self.select_mode('speaker_indoor'))
        layout.addWidget(indoor_speaker_btn)

        # 室外音箱按钮
        outdoor_speaker_btn = QPushButton('🌳  智能室外音箱产测工具')
        outdoor_speaker_btn.setFont(QFont('Microsoft YaHei', 13))
        outdoor_speaker_btn.setMinimumHeight(60)
        outdoor_speaker_btn.setCursor(Qt.PointingHandCursor)
        outdoor_speaker_btn.clicked.connect(lambda: self.select_mode('speaker_outdoor'))
        layout.addWidget(outdoor_speaker_btn)

        self.setLayout(layout)

    def select_mode(self, mode):
        """选择模式并关闭对话框"""
        self.selected_mode = mode
        self.accept()
