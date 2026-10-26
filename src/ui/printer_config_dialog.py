from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                             QComboBox, QPushButton, QGroupBox, QFormLayout,
                             QSpinBox, QMessageBox)
from PyQt5.QtCore import Qt
import win32print
import yaml
import os
import logging

logger = logging.getLogger(__name__)


class PrinterConfigDialog(QDialog):
    """打印机配置对话框"""

    # 协议检测规则（与 UniversalPrinter 保持一致）
    _PROTOCOL_RULES = [
        ('zpl',  ['zdesigner', 'zebra', 'zpl']),
        ('tspl', ['xprinter', 'xp-t', 'xp-n', 'xp-d', 'tsc', 'tspl']),
    ]

    def __init__(self, config_path: str, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.current_config = self._load_config()
        
        self.setWindowTitle('打印机配置')
        self.setMinimumWidth(500)
        self.init_ui()
        self.load_printers()

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

        # 打印机选择组
        printer_group = QGroupBox('打印机选择')
        printer_layout = QFormLayout()

        self.printer_combo = QComboBox()
        self.printer_combo.currentTextChanged.connect(self.on_printer_changed)
        printer_layout.addRow('打印机:', self.printer_combo)

        self.refresh_btn = QPushButton('刷新列表')
        self.refresh_btn.clicked.connect(self.load_printers)
        printer_layout.addRow('', self.refresh_btn)

        printer_group.setLayout(printer_layout)
        layout.addWidget(printer_group)

        # 协议配置组
        protocol_group = QGroupBox('协议配置')
        protocol_layout = QFormLayout()

        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(['AUTO（自动检测）', 'ZPL (Zebra)', 'TSPL (Xprinter/TSC)'])
        protocol_layout.addRow('打印协议:', self.protocol_combo)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(203, 600)
        self.dpi_spin.setSingleStep(1)
        self.dpi_spin.setValue(600)
        self.dpi_spin.setSuffix(' DPI')
        protocol_layout.addRow('分辨率:', self.dpi_spin)

        protocol_group.setLayout(protocol_layout)
        layout.addWidget(protocol_group)

        # 检测信息标签
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet('color: #666; padding: 10px;')
        layout.addWidget(self.info_label)

        # 按钮组
        button_layout = QHBoxLayout()
        
        self.test_btn = QPushButton('测试打印')
        self.test_btn.clicked.connect(self.test_print)
        button_layout.addWidget(self.test_btn)

        button_layout.addStretch()

        self.save_btn = QPushButton('保存')
        self.save_btn.clicked.connect(self.save_config)
        button_layout.addWidget(self.save_btn)

        self.cancel_btn = QPushButton('取消')
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)

        layout.addLayout(button_layout)
        self.setLayout(layout)

        # 加载当前配置
        self._load_current_settings()

    def _load_current_settings(self):
        """加载当前保存的设置"""
        printer_cfg = self.current_config.get('printer', {})
        
        # 加载 DPI
        dpi = printer_cfg.get('dpi', 600)
        self.dpi_spin.setValue(dpi)
        
        # 加载协议
        protocol = printer_cfg.get('protocol', 'auto')
        if protocol == 'auto':
            self.protocol_combo.setCurrentIndex(0)
        elif protocol == 'zpl':
            self.protocol_combo.setCurrentIndex(1)
        elif protocol == 'tspl':
            self.protocol_combo.setCurrentIndex(2)

    def load_printers(self):
        """扫描并加载系统打印机列表"""
        self.printer_combo.clear()
        
        try:
            printers = [printer[2] for printer in win32print.EnumPrinters(2)]
            default_printer = win32print.GetDefaultPrinter()
            
            # 优先显示已保存的打印机
            saved_printer = self.current_config.get('printer', {}).get('printer_name', '')
            
            if saved_printer and saved_printer in printers:
                # 已保存的打印机排在第一位
                printers.remove(saved_printer)
                printers.insert(0, saved_printer)
                self.printer_combo.addItems(printers)
                self.printer_combo.setCurrentIndex(0)
            elif default_printer in printers:
                # 默认打印机排在第一位
                printers.remove(default_printer)
                printers.insert(0, default_printer)
                self.printer_combo.addItems(printers)
                self.printer_combo.setCurrentIndex(0)
            else:
                self.printer_combo.addItems(printers)
            
            if printers:
                self.on_printer_changed(self.printer_combo.currentText())
            else:
                self.info_label.setText('⚠️ 未找到可用的打印机')
                
        except Exception as e:
            logger.error(f"扫描打印机失败: {e}")
            QMessageBox.warning(self, '错误', f'扫描打印机失败:\n{e}')

    def on_printer_changed(self, printer_name: str):
        """打印机选择改变时的处理"""
        if not printer_name:
            return

        # 自动检测协议
        detected_protocol = self._guess_protocol(printer_name)

        # 推荐DPI
        recommended_dpi = self._recommend_dpi(printer_name, detected_protocol)

        # 自动更新 DPI 控件为推荐值
        self.dpi_spin.setValue(recommended_dpi)

        # 更新信息标签
        info_parts = []
        info_parts.append(f"📌 打印机: {printer_name}")
        info_parts.append(f"🔍 检测协议: {detected_protocol}")
        info_parts.append(f"💡 推荐DPI: {recommended_dpi}")

        self.info_label.setText('\n'.join(info_parts))

        # 如果当前选择的是自动检测，更新协议显示
        if self.protocol_combo.currentIndex() == 0:
            if detected_protocol == 'ZPL':
                self.protocol_combo.setCurrentText('AUTO（自动检测）')
            elif detected_protocol == 'TSPL':
                self.protocol_combo.setCurrentText('AUTO（自动检测）')

    def _guess_protocol(self, name: str) -> str:
        """猜测打印机协议（优先使用已保存的配置）"""
        # 优先级：已保存的配置 → 名称检测 → 默认ZPL
        saved_name = self.current_config.get('printer', {}).get('printer_name', '')
        saved_proto = self.current_config.get('printer', {}).get('protocol', '')
        
        if saved_name and saved_proto and saved_name == name:
            return saved_proto.upper()
        
        # 名称检测
        name_lower = name.lower()
        for protocol, keywords in self._PROTOCOL_RULES:
            if any(kw in name_lower for kw in keywords):
                return protocol.upper()
        
        return 'ZPL'

    def _recommend_dpi(self, printer_name: str, protocol: str) -> int:
        """推荐DPI设置"""
        name_lower = printer_name.lower()

        # 从打印机名称提取DPI信息
        if '600' in name_lower or '600dpi' in name_lower:
            return 600
        elif '300' in name_lower or '300dpi' in name_lower:
            return 300
        elif '203' in name_lower or '203dpi' in name_lower:
            return 203

        # 按协议推荐默认DPI（统一默认 600 DPI）
        # 现代标签打印机通常支持 600 DPI，更清晰
        return 600

    def test_print(self):
        """测试打印"""
        printer_name = self.printer_combo.currentText()
        if not printer_name:
            QMessageBox.warning(self, '警告', '请先选择打印机')
            return
        
        protocol_text = self.protocol_combo.currentText()
        if protocol_text.startswith('AUTO'):
            protocol = 'auto'
        elif 'ZPL' in protocol_text:
            protocol = 'zpl'
        else:
            protocol = 'tspl'
        
        dpi = self.dpi_spin.value()
        
        # 构建临时配置
        test_config = {
            'printer': {
                'printer_name': printer_name,
                'protocol': protocol,
                'dpi': dpi,
                'paper_width': self.current_config.get('printer', {}).get('paper_width', 50),
                'paper_height': self.current_config.get('printer', {}).get('paper_height', 30),
                'tspl_layout': self.current_config.get('printer', {}).get('tspl_layout', {}),
                'zpl_layout': self.current_config.get('printer', {}).get('zpl_layout', {}),
            }
        }
        
        try:
            from ..hardware.universal_printer import UniversalPrinter
            printer = UniversalPrinter(test_config)
            success = printer.print_label('TEST123456', '', 'TEST')
            
            if success:
                QMessageBox.information(self, '成功', '测试打印已发送！\n请检查打印机输出。')
            else:
                QMessageBox.warning(self, '失败', '打印失败，请检查打印机状态。')
        except Exception as e:
            logger.error(f"测试打印失败: {e}")
            QMessageBox.critical(self, '错误', f'测试打印失败:\n{e}')

    def save_config(self):
        """保存配置到 YAML 文件"""
        printer_name = self.printer_combo.currentText()
        if not printer_name:
            QMessageBox.warning(self, '警告', '请先选择打印机')
            return
        
        protocol_text = self.protocol_combo.currentText()
        if protocol_text.startswith('AUTO'):
            protocol = 'auto'
        elif 'ZPL' in protocol_text:
            protocol = 'zpl'
        else:
            protocol = 'tspl'
        
        dpi = self.dpi_spin.value()
        
        # 更新配置
        if 'printer' not in self.current_config:
            self.current_config['printer'] = {}
        
        self.current_config['printer']['printer_name'] = printer_name
        self.current_config['printer']['protocol'] = protocol
        self.current_config['printer']['dpi'] = dpi
        
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.current_config, f, allow_unicode=True, sort_keys=False)
            
            logger.info(f"打印机配置已保存: {printer_name}, {protocol}, {dpi} DPI")
            QMessageBox.information(self, '成功', '打印机配置已保存！')
            self.accept()
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            QMessageBox.critical(self, '错误', f'保存配置失败:\n{e}')
