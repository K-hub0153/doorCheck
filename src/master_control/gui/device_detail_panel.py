from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QTextEdit, QGridLayout, QGroupBox, QScrollArea, QSizePolicy)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from src.master_control.core.test_result import TestStatus, DeviceTestResults


class TestItemWidget(QWidget):
    
    test_clicked = pyqtSignal(str)
    
    def __init__(self, test_name: str, display_name: str, parent=None):
        super().__init__(parent)
        self.test_name = test_name
        self.display_name = display_name
        self.init_ui()
    
    def init_ui(self):
        layout = QHBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)

        self.test_btn = QPushButton(self.display_name)
        self.test_btn.setFixedWidth(80)
        self.test_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #0b7dda;
            }
        """)
        self.test_btn.clicked.connect(lambda: self.test_clicked.emit(self.test_name))
        layout.addWidget(self.test_btn)

        self.status_label = QLabel("未测试")
        self.status_label.setFont(QFont("Arial", 9))
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFixedWidth(60)
        layout.addWidget(self.status_label)

        self.message_label = QLabel("")
        self.message_label.setFont(QFont("Arial", 8))
        # 允许换行并在过窄时收缩，避免长文本撑破面板
        self.message_label.setWordWrap(True)
        self.message_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.message_label.setMinimumWidth(120)
        layout.addWidget(self.message_label, 1)

        self.setLayout(layout)
    
    def update_result(self, status: TestStatus, message: str = "", detail: str = ""):
        if status == TestStatus.PASSED:
            self.status_label.setText("✓ 通过")
            self.status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
        elif status == TestStatus.FAILED:
            self.status_label.setText("✗ 失败")
            self.status_label.setStyleSheet("color: #f44336; font-weight: bold;")
        elif status == TestStatus.TESTING:
            self.status_label.setText("⟳ 测试中")
            self.status_label.setStyleSheet("color: #ff9800; font-weight: bold;")
        else:
            self.status_label.setText("未测试")
            self.status_label.setStyleSheet("color: #999;")

        # 显示检测详细信息（message字段）
        display_text = message if message else ""
        self.message_label.setText(display_text)
        # 文本过长时设置 tooltip，鼠标悬停可查看全文
        if display_text and len(display_text) > 40:
            self.message_label.setToolTip(display_text)
        else:
            self.message_label.setToolTip("")


class DeviceDetailPanel(QWidget):

    test_item_clicked = pyqtSignal(str, str, str)
    auto_test_clicked = pyqtSignal(str, str)
    test_results_clicked = pyqtSignal()
    print_label_clicked = pyqtSignal(str)
    shutdown_device_clicked = pyqtSignal(str)
    switch_normal_mode_clicked = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_device_sn = None
        self.current_device_ip = None
        # 各检测项的最新状态，用于判断是否可以打印标签
        self.test_statuses = {}
        # 打印前是否必须全部检测项通过，由 MainWindow 按配置同步
        self.require_all_passed = True
        self.init_ui()
        self._refresh_print_button()
    
    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        
        header = QLabel("设备详情")
        header.setFont(QFont("Arial", 14, QFont.Bold))
        header.setStyleSheet("padding: 10px; background-color: #2196F3; color: white;")
        layout.addWidget(header)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        content_widget = QWidget()
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(15, 15, 15, 15)
        
        info_layout = QHBoxLayout()
        self.device_info_label = QLabel("请选择设备")
        self.device_info_label.setFont(QFont("Arial", 12))
        self.device_info_label.setStyleSheet("padding: 10px; background-color: #f5f5f5; border-radius: 5px;")
        info_layout.addWidget(self.device_info_label)

        # 系统版本与 SN/IP 同一行，只显示版本号
        self.os_version_label = QLabel("-")
        self.os_version_label.setFont(QFont("Arial", 12))
        self.os_version_label.setStyleSheet("padding: 10px; background-color: #f5f5f5; border-radius: 5px; color: #555;")
        info_layout.addWidget(self.os_version_label)
        info_layout.addStretch()

        content_layout.addLayout(info_layout)

        test_group = QGroupBox("检测项目")
        test_group.setFont(QFont("Arial", 11, QFont.Bold))
        test_layout = QVBoxLayout()
        test_layout.setSpacing(4)

        auto_test_layout = QHBoxLayout()
        auto_test_layout.setContentsMargins(5, 5, 5, 5)

        self.auto_test_btn = QPushButton("一键测试")
        self.auto_test_btn.setFixedWidth(80)
        self.auto_test_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF5722;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #E64A19;
            }
            QPushButton:disabled {
                background-color: #BDBDBD;
                color: #757575;
            }
        """)
        # self.auto_test_btn.setEnabled(False)
        self.auto_test_btn.clicked.connect(self.on_auto_test_clicked)
        auto_test_layout.addWidget(self.auto_test_btn)

        self.auto_test_status = QLabel("")
        self.auto_test_status.setFont(QFont("Arial", 9))
        self.auto_test_status.setAlignment(Qt.AlignCenter)
        self.auto_test_status.setFixedWidth(60)
        auto_test_layout.addWidget(self.auto_test_status)

        auto_test_layout.addStretch()
        test_layout.addLayout(auto_test_layout)

        self.test_widgets = {}
        test_items = [
            ("poe", "POE 网口"),
            ("disk", "硬盘"),
            ("fan", "风扇"),
            ("hdmi", "HDMI"),
            ("lte_4g", "4G 模块"),
            ("battery", "电池"),
            ("wifi", "WiFi"),
            ("bluetooth", "蓝牙"),
            ("nearlink", "星闪"),
            ("mac_write", "烧写MAC")
        ]
        
        for test_name, display_name in test_items:
            widget = TestItemWidget(test_name, display_name)
            widget.test_clicked.connect(self.on_test_item_clicked)
            test_layout.addWidget(widget)
            self.test_widgets[test_name] = widget
        
        test_group.setLayout(test_layout)
        content_layout.addWidget(test_group)
        
        log_group = QGroupBox("测试日志")
        log_group.setFont(QFont("Arial", 11, QFont.Bold))
        log_layout = QVBoxLayout()

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Courier", 8))
        self.log_text.setMaximumHeight(150)
        log_layout.addWidget(self.log_text)
        
        log_group.setLayout(log_layout)
        content_layout.addWidget(log_group)
        
        button_layout = QHBoxLayout()

        # self.results_btn = QPushButton("测试结果")
        # self.results_btn.setStyleSheet("""
        #     QPushButton {
        #         background-color: #4CAF50;
        #         color: white;
        #         border: none;
        #         padding: 10px 20px;
        #         border-radius: 4px;
        #         font-size: 12px;
        #     }
        #     QPushButton:hover {
        #         background-color: #45a049;
        #     }
        # """)
        # self.results_btn.clicked.connect(self.test_results_clicked.emit)
        # button_layout.addWidget(self.results_btn)
        
        self.print_btn = QPushButton("打印标签")
        self.print_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #e68900;
            }
            QPushButton:disabled {
                background-color: #BDBDBD;
                color: #757575;
            }
        """)
        self.print_btn.clicked.connect(self.on_print_label)
        self.print_btn.setEnabled(False)
        button_layout.addWidget(self.print_btn)

        self.shutdown_btn = QPushButton("关机")
        self.shutdown_btn.setStyleSheet("""
            QPushButton {
                background-color: #E53935;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #C62828;
            }
            QPushButton:disabled {
                background-color: #BDBDBD;
                color: #757575;
            }
        """)
        self.shutdown_btn.clicked.connect(self.on_shutdown_device)
        self.shutdown_btn.setEnabled(False)
        button_layout.addWidget(self.shutdown_btn)

        self.normal_mode_btn = QPushButton("生产模式")
        self.normal_mode_btn.setStyleSheet("""
            QPushButton {
                background-color: #9C27B0;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #7B1FA2;
            }
        """)
        self.normal_mode_btn.clicked.connect(self.on_switch_normal_mode)
        self.normal_mode_btn.setEnabled(False)
        self.normal_mode_btn.setVisible(False)
        button_layout.addWidget(self.normal_mode_btn)
        
        content_layout.addLayout(button_layout)
        content_layout.addStretch()
        
        content_widget.setLayout(content_layout)
        scroll.setWidget(content_widget)
        layout.addWidget(scroll)
        
        self.setLayout(layout)
    
    def set_device(self, sn: str, ip: str):
        self.current_device_sn = sn
        self.current_device_ip = ip
        self.device_info_label.setText(f"SN: {sn}   IP: {ip}")
        self.normal_mode_btn.setEnabled(True)
        self.shutdown_btn.setEnabled(True)
        self.os_version_label.setText("获取中...")
        self.clear_results()

    def set_required_tests(self, test_names: list):
        """设置该设备需要全部通过的检测项（如不支持 HDMI 的芯片不包含 hdmi）"""
        self.required_tests = list(test_names)
        self._refresh_print_button()

    def set_os_version(self, version: str):
        """只显示版本号，与 SN/IP 同一行"""
        self.os_version_label.setText(version or "-")

    def enable_auto_test(self):
        """启用一键测试按钮"""
        self.auto_test_btn.setEnabled(True)
    
    def on_test_item_clicked(self, test_name: str):
        if self.current_device_sn and self.current_device_ip:
            self.test_item_clicked.emit(test_name, self.current_device_sn, self.current_device_ip)

    def on_auto_test_clicked(self):
        if self.current_device_sn and self.current_device_ip:
            self.auto_test_clicked.emit(self.current_device_sn, self.current_device_ip)
    
    def on_print_label(self):
        if self.current_device_sn:
            self.print_label_clicked.emit(self.current_device_sn)

    def on_shutdown_device(self):
        if self.current_device_sn:
            self.shutdown_device_clicked.emit(self.current_device_sn)

    def on_switch_normal_mode(self):
        if self.current_device_sn:
            self.switch_normal_mode_clicked.emit(self.current_device_sn)
    
    def load_history_result(self, test_name: str, status: TestStatus, message: str = ""):
        """仅用于显示数据库中的历史记录，不参与打印标签的门控

        产测要求本次现场检测全部通过后才允许打印，历史记录只做参考展示。
        """
        if test_name in self.test_widgets:
            self.test_widgets[test_name].update_result(status, message, "")

    def update_test_result(self, test_name: str, status: TestStatus, message: str = "", detail: str = ""):
        if test_name in self.test_widgets:
            self.test_widgets[test_name].update_result(status, message, detail)
        # 记录最新状态（测试中的中间态不覆盖已有结果）
        if status != TestStatus.TESTING:
            self.test_statuses[test_name] = status
        self._refresh_print_button()

    def _get_required(self) -> list:
        """获取必须全部通过的检测项列表"""
        required = getattr(self, 'required_tests', None)
        if required is None:
            # 未显式指定时，取面板上全部检测项
            required = list(self.test_widgets.keys())
        return required

    def _get_not_passed(self) -> list:
        """返回未通过的检测项名称列表"""
        return [
            name for name in self._get_required()
            if self.test_statuses.get(name) != TestStatus.PASSED
        ]

    def all_tests_passed(self) -> bool:
        """当前主控的检测项是否全部通过"""
        required = self._get_required()
        return bool(required) and not self._get_not_passed()

    def get_not_passed_display_names(self) -> list:
        """返回未通过检测项的中文显示名列表"""
        names = []
        for name in self._get_not_passed():
            widget = self.test_widgets.get(name)
            names.append(widget.display_name if widget else name)
        return names

    def _refresh_print_button(self):
        """根据配置决定打印按钮是否可用

        默认要求所有必需检测项都通过；当 config.yaml 里
        printer.require_all_passed 设为 false 时，不做门控，始终可打印。
        """
        if not self._get_required():
            self.print_btn.setEnabled(False)
            self.print_btn.setToolTip("无可检测项")
            return

        if not getattr(self, 'require_all_passed', True):
            # 免检模式：不校验检测项是否通过
            self.print_btn.setEnabled(True)
            not_passed = self.get_not_passed_display_names()
            if not_passed:
                self.print_btn.setToolTip(
                    "已关闭检测项校验（printer.require_all_passed=false），可直接打印标签。\n"
                    f"当前未通过：{'、'.join(not_passed)}")
            else:
                self.print_btn.setToolTip("所有检测项已通过，可打印标签")
            return

        if self.all_tests_passed():
            self.print_btn.setEnabled(True)
            self.print_btn.setToolTip("所有检测项已通过，可打印标签")
        else:
            self.print_btn.setEnabled(False)
            missing = "、".join(self.get_not_passed_display_names())
            self.print_btn.setToolTip(f"以下检测项未通过，无法打印标签：{missing}")

    def set_require_all_passed(self, required: bool):
        """设置打印前是否必须全部检测项通过（配置热加载时调用）"""
        self.require_all_passed = bool(required)
        self._refresh_print_button()
    
    def append_log(self, message: str):
        self.log_text.append(message)
    
    def clear_log(self):
        self.log_text.clear()
    
    def clear_results(self):
        for widget in self.test_widgets.values():
            widget.update_result(TestStatus.NOT_TESTED)
        self.test_statuses = {}
        self._refresh_print_button()
        self.clear_log()
