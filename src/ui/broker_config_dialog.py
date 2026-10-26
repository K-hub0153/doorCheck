from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QGroupBox, QFormLayout,
                             QSpinBox, QMessageBox)
from PyQt5.QtCore import Qt
import yaml
import os
import logging

logger = logging.getLogger(__name__)


class BrokerConfigDialog(QDialog):
    """远程MQTT Broker配置对话框"""

    def __init__(self, config_path: str, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.current_config = self._load_config()

        self.setWindowTitle('远程Broker配置')
        self.setMinimumWidth(450)
        self.init_ui()
        self.load_current_config()

    def _load_config(self) -> dict:
        """加载当前配置"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
        return {}

    def init_ui(self):
        layout = QVBoxLayout()

        # 远程Broker配置组
        broker_group = QGroupBox('远程Broker配置')
        broker_layout = QFormLayout()

        # Broker地址输入
        self.broker_input = QLineEdit()
        self.broker_input.setPlaceholderText('例如: 192.168.1.100 或 22.0.0.10')
        broker_layout.addRow('Broker地址:', self.broker_input)

        # Broker端口输入
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(1881)
        broker_layout.addRow('Broker端口:', self.port_spin)

        broker_group.setLayout(broker_layout)
        layout.addWidget(broker_group)

        # 说明信息
        info_label = QLabel(
            '提示：\n'
            '1. 请输入主控端MQTT Broker的IP地址和端口\n'
            '2. 切换到远程Broker模式后需要重启软件生效'
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet('color: #666; padding: 10px; background-color: #f5f5f5; border-radius: 4px;')
        layout.addWidget(info_label)

        # 按钮组
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.save_btn = QPushButton('保存')
        self.save_btn.clicked.connect(self.save_config)
        button_layout.addWidget(self.save_btn)

        self.cancel_btn = QPushButton('取消')
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)

        layout.addLayout(button_layout)
        self.setLayout(layout)

    def load_current_config(self):
        """加载当前配置到界面"""
        mqtt_config = self.current_config.get('mqtt', {})

        # 加载broker地址
        broker = mqtt_config.get('broker', '22.0.0.1')
        self.broker_input.setText(broker)

        # 加载端口
        port = mqtt_config.get('port', 1881)
        self.port_spin.setValue(port)

    def save_config(self):
        """保存配置到文件"""
        broker = self.broker_input.text().strip()

        # 验证输入
        if not broker:
            QMessageBox.warning(self, '输入错误', 'Broker地址不能为空')
            return

        # 简单的IP地址格式验证
        parts = broker.split('.')
        if len(parts) == 4:
            try:
                for part in parts:
                    num = int(part)
                    if not (0 <= num <= 255):
                        raise ValueError
            except ValueError:
                QMessageBox.warning(self, '输入错误', 'Broker地址格式不正确，请输入有效的IP地址')
                return

        port = self.port_spin.value()

        try:
            # 确保mqtt节点存在
            if 'mqtt' not in self.current_config:
                self.current_config['mqtt'] = {}

            # 更新配置
            self.current_config['mqtt']['broker'] = broker
            self.current_config['mqtt']['port'] = port

            # 写回配置文件
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.current_config, f, allow_unicode=True, default_flow_style=False)

            logger.info(f"远程Broker配置已保存: {broker}:{port}")
            QMessageBox.information(
                self, '保存成功',
                f'远程Broker配置已保存：\n'
                f'地址：{broker}\n'
                f'端口：{port}\n\n'
                f'切换到远程Broker模式后需要重启软件生效'
            )
            self.accept()

        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            QMessageBox.critical(self, '保存失败', f'保存配置失败: {str(e)}')
