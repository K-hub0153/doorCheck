"""
打印机配置对话框
支持扫描系统打印机、自动检测协议、配置和保存
"""

try:
    import win32print
except ImportError:
    win32print = None
import yaml
import os
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QComboBox, QGroupBox, QFormLayout,
                             QSpinBox, QMessageBox, QTextEdit, QRadioButton,
                             QButtonGroup)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

from ..utils.logger import logger


def _printer_section_bounds(lines):
    """定位 printer: 段的行范围

    Returns:
        (start, end) —— start 是 'printer:' 那一行的下标，end 是段结束（不含）的下标。
        段内只认缩进更深的行，遇到顶格的新键或文件结尾就结束。
        找不到 printer: 段时返回 (None, None)。
    """
    start = None
    for i, line in enumerate(lines):
        stripped = line.rstrip('\n')
        # 顶格的 printer: 才是段落起点（排除 '# printer:' 之类的注释）
        if stripped.startswith('printer:') and not stripped.lstrip().startswith('#'):
            start = i
            continue
        if start is not None:
            # 空行与注释不算段落结束
            if not stripped.strip():
                continue
            if stripped.startswith('#'):
                continue
            # 顶格的其它键 → 段落结束
            if not line[0].isspace():
                return start, i
    if start is not None:
        return start, len(lines)
    return None, None


def _dump_scalar(value):
    """把一个 Python 标量转成 yaml 字面量（去掉 safe_dump 附带的 '...' 文档结束符）"""
    text = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True)
    text = text.strip()
    if text.endswith('...'):
        text = text[:-3].strip()
    return text


def _update_printer_section(lines, updates):
    """在 printer: 段内就地替换给定键的值，保留注释与键顺序

    只替换行尾的值部分（保留原有的行内注释），不改动其它任何行。

    Returns:
        (applied_keys, new_lines)
    """
    start, end = _printer_section_bounds(lines)
    if start is None:
        return set(), lines

    lines = list(lines)
    applied = set()

    for i in range(start + 1, end):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        # 只处理 printer 的直接子键（缩进一级），不碰 tspl_layout/zpl_layout 内部
        if len(line) - len(line.lstrip()) != 2:
            continue

        key_part, sep, _ = line.partition(':')
        if not sep:
            continue
        key = key_part.strip()
        if key not in updates:
            continue

        indent = line[:len(line) - len(line.lstrip())]
        newline = line[len(line.rstrip('\n')):] or '\n'

        # 保留行内注释：'dpi: 203  # 注释'
        _, _, after = line.partition(':')
        comment = ''
        if '#' in after:
            comment = '  ' + after[after.index('#'):].rstrip('\n')

        lines[i] = f"{indent}{key}: {_dump_scalar(updates[key])}{comment}{newline}"
        applied.add(key)

    return applied, lines


def _append_to_printer_section(lines, items):
    """把缺失的键追加到 printer: 段末尾（保持二级缩进）"""
    start, end = _printer_section_bounds(lines)
    if start is None:
        # 没有 printer: 段，整个补一个
        lines = list(lines)
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append('printer:\n')
        for k, v in items.items():
            lines.append(f"  {k}: {_dump_scalar(v)}\n")
        return lines

    lines = list(lines)
    # 插到段末尾最后一个非空行之后
    insert_at = end
    while insert_at - 1 > start and not lines[insert_at - 1].strip():
        insert_at -= 1

    block = [f"  {k}: {_dump_scalar(v)}\n" for k, v in items.items()]
    lines[insert_at:insert_at] = block
    return lines


class PrinterConfigDialog(QDialog):
    """打印机配置对话框"""
    
    # 协议检测关键字规则表（与UniversalPrinter保持一致）
    _PROTOCOL_RULES = [
        ('zpl',  ['zdesigner', 'zebra', 'zpl']),
        ('tspl', ['xprinter', 'xp-t', 'xp-n', 'xp-d', 'tsc', 'tspl']),
    ]
    
    def __init__(self, current_config, parent=None):
        super().__init__(parent)
        self.current_config = current_config
        self.printer_list = []
        self.setWindowTitle('打印机配置')
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        self.init_ui()
        self.load_current_settings()
    
    def init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout()
        layout.setSpacing(15)
        self.setLayout(layout)
        
        # 标题
        title_label = QLabel('🖨️ 打印机配置')
        title_label.setFont(QFont('Microsoft YaHei', 14, QFont.Bold))
        title_label.setStyleSheet('color: #2c3e50; padding: 10px;')
        layout.addWidget(title_label)
        
        # 打印机选择组
        printer_group = QGroupBox('打印机选择')
        printer_group.setFont(QFont('Microsoft YaHei', 10, QFont.Bold))
        printer_layout = QVBoxLayout()
        printer_group.setLayout(printer_layout)
        
        # 打印机下拉框和刷新按钮
        select_layout = QHBoxLayout()
        self.printer_combo = QComboBox()
        self.printer_combo.setMinimumHeight(32)
        self.printer_combo.currentIndexChanged.connect(self.on_printer_selected)
        select_layout.addWidget(QLabel('打印机:'))
        select_layout.addWidget(self.printer_combo, 1)
        
        self.scan_button = QPushButton('🔄 扫描打印机')
        self.scan_button.clicked.connect(self.scan_printers)
        select_layout.addWidget(self.scan_button)
        printer_layout.addLayout(select_layout)
        
        # 检测到的打印机信息
        info_layout = QHBoxLayout()
        info_layout.addWidget(QLabel('检测协议:'))
        self.protocol_label = QLabel('未知')
        self.protocol_label.setStyleSheet('font-weight: bold; color: #3498db;')
        info_layout.addWidget(self.protocol_label)
        info_layout.addStretch()
        printer_layout.addLayout(info_layout)
        
        layout.addWidget(printer_group)
        
        # 协议选择组
        protocol_group = QGroupBox('协议设置')
        protocol_group.setFont(QFont('Microsoft YaHei', 10, QFont.Bold))
        protocol_layout = QVBoxLayout()
        protocol_group.setLayout(protocol_layout)
        
        self.protocol_button_group = QButtonGroup()
        protocol_radio_layout = QHBoxLayout()
        
        self.auto_radio = QRadioButton('自动检测')
        self.auto_radio.setChecked(True)
        self.protocol_button_group.addButton(self.auto_radio, 0)
        protocol_radio_layout.addWidget(self.auto_radio)
        
        self.zpl_radio = QRadioButton('ZPL (Zebra)')
        self.protocol_button_group.addButton(self.zpl_radio, 1)
        protocol_radio_layout.addWidget(self.zpl_radio)
        
        self.tspl_radio = QRadioButton('TSPL (Xprinter/TSC)')
        self.protocol_button_group.addButton(self.tspl_radio, 2)
        protocol_radio_layout.addWidget(self.tspl_radio)
        
        protocol_radio_layout.addStretch()
        protocol_layout.addLayout(protocol_radio_layout)
        
        layout.addWidget(protocol_group)
        
        # DPI设置组
        dpi_group = QGroupBox('打印参数')
        dpi_group.setFont(QFont('Microsoft YaHei', 10, QFont.Bold))
        dpi_layout = QFormLayout()
        dpi_group.setLayout(dpi_layout)
        
        self.dpi_combo = QComboBox()
        self.dpi_combo.addItems(['203', '300', '600'])
        self.dpi_combo.setCurrentText('600')
        dpi_layout.addRow('DPI:', self.dpi_combo)
        
        self.width_spin = QSpinBox()
        self.width_spin.setRange(10, 200)
        self.width_spin.setValue(50)
        self.width_spin.setSuffix(' mm')
        dpi_layout.addRow('标签宽度:', self.width_spin)
        
        self.height_spin = QSpinBox()
        self.height_spin.setRange(10, 200)
        self.height_spin.setValue(30)
        self.height_spin.setSuffix(' mm')
        dpi_layout.addRow('标签高度:', self.height_spin)
        
        layout.addWidget(dpi_group)
        
        # 预览信息
        preview_group = QGroupBox('当前配置预览')
        preview_group.setFont(QFont('Microsoft YaHei', 10, QFont.Bold))
        preview_layout = QVBoxLayout()
        preview_group.setLayout(preview_layout)
        
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumHeight(120)
        self.preview_text.setStyleSheet('''
            QTextEdit {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 8px;
                font-family: Consolas, monospace;
                font-size: 11px;
            }
        ''')
        preview_layout.addWidget(self.preview_text)
        
        layout.addWidget(preview_group)
        
        # 按钮
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.test_button = QPushButton('🧪 测试打印')
        self.test_button.clicked.connect(self.test_print)
        button_layout.addWidget(self.test_button)
        
        self.save_button = QPushButton('💾 保存配置')
        self.save_button.clicked.connect(self.save_config)
        self.save_button.setStyleSheet('''
            QPushButton {
                background-color: #27ae60;
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #229954;
            }
        ''')
        button_layout.addWidget(self.save_button)
        
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button)
        
        layout.addLayout(button_layout)
        
        # 初始扫描打印机
        self.scan_printers()
    
    def scan_printers(self):
        """扫描系统打印机"""
        try:
            self.printer_combo.clear()
            self.printer_list = []
            
            if win32print is None:
                self.printer_combo.addItem('当前系统不支持 Windows 本地打印机')
                return

            # 获取所有打印机
            printers = [printer[2] for printer in win32print.EnumPrinters(2)]
            
            if not printers:
                QMessageBox.warning(self, '警告', '未检测到打印机！')
                return
            
            self.printer_list = printers
            self.printer_combo.addItems(printers)
            
            logger.info(f"扫描到 {len(printers)} 台打印机")
            
            # 尝试选中当前配置的打印机
            saved_name = self.current_config.get('printer', {}).get('printer_name', '')
            if saved_name and saved_name in printers:
                self.printer_combo.setCurrentText(saved_name)
            
        except Exception as e:
            logger.error(f"扫描打印机失败: {e}")
            QMessageBox.critical(self, '错误', f'扫描打印机失败: {e}')
    
    def on_printer_selected(self, index):
        """打印机选择变化"""
        if index < 0 or index >= len(self.printer_list):
            return
        
        printer_name = self.printer_list[index]
        detected_protocol = self._guess_protocol(printer_name)
        
        # 更新检测到的协议
        protocol_text = {
            'ZPL': 'ZPL (Zebra)',
            'TSPL': 'TSPL (Xprinter/TSC)',
            'AUTO': '未知'
        }.get(detected_protocol, '未知')
        
        self.protocol_label.setText(protocol_text)
        
        # 根据检测结果设置推荐DPI
        if detected_protocol == 'ZPL':
            self.dpi_combo.setCurrentText('600')
        elif detected_protocol == 'TSPL':
            self.dpi_combo.setCurrentText('203')
        
        self.update_preview()
    
    def _guess_protocol(self, name: str) -> str:
        """根据打印机名称猜测协议"""
        saved_name = self.current_config.get('printer', {}).get('printer_name', '')
        saved_proto = self.current_config.get('printer', {}).get('protocol', '')
        
        # 如果是保存过的打印机，使用保存的协议
        if saved_name and saved_proto and saved_name == name:
            return saved_proto.upper()
        
        # 否则根据关键字检测
        name_lower = name.lower()
        for protocol, keywords in self._PROTOCOL_RULES:
            if any(kw in name_lower for kw in keywords):
                return protocol.upper()
        
        return 'AUTO'
    
    def load_current_settings(self):
        """加载当前配置"""
        printer_cfg = self.current_config.get('printer', {})
        
        # 加载DPI
        dpi = printer_cfg.get('dpi', 600)
        self.dpi_combo.setCurrentText(str(dpi))
        
        # 加载标签尺寸
        width = printer_cfg.get('paper_width', 50)
        height = printer_cfg.get('paper_height', 30)
        self.width_spin.setValue(width)
        self.height_spin.setValue(height)
        
        # 加载协议设置
        protocol = printer_cfg.get('protocol', 'auto')
        if protocol == 'auto':
            self.auto_radio.setChecked(True)
        elif protocol == 'zpl':
            self.zpl_radio.setChecked(True)
        elif protocol == 'tspl':
            self.tspl_radio.setChecked(True)
        
        self.update_preview()
    
    def update_preview(self):
        """更新配置预览"""
        printer_name = self.printer_combo.currentText() if self.printer_combo.currentIndex() >= 0 else '未选择'
        protocol = 'auto'
        if self.zpl_radio.isChecked():
            protocol = 'zpl'
        elif self.tspl_radio.isChecked():
            protocol = 'tspl'
        
        dpi = self.dpi_combo.currentText()
        width = self.width_spin.value()
        height = self.height_spin.value()
        
        preview = f"""打印机名称: {printer_name}
协议模式: {protocol}
DPI: {dpi}
标签尺寸: {width} x {height} mm
"""
        self.preview_text.setText(preview)
    
    def test_print(self):
        """测试打印"""
        if self.printer_combo.currentIndex() < 0:
            QMessageBox.warning(self, '警告', '请先选择打印机！')
            return
        
        try:
            # 临时创建打印机实例测试
            from ..utils.universal_printer import UniversalPrinter
            
            protocol = 'auto'
            if self.zpl_radio.isChecked():
                protocol = 'zpl'
            elif self.tspl_radio.isChecked():
                protocol = 'tspl'
            
            test_config = {
                'enabled': True,
                'printer_name': self.printer_combo.currentText(),
                'dpi': int(self.dpi_combo.currentText()),
                'protocol': protocol,
                'paper_width_mm': self.width_spin.value(),
                'paper_height_mm': self.height_spin.value(),
                'tspl_layout': self.current_config.get('printer', {}).get('tspl_layout', {}),
                'zpl_layout': self.current_config.get('printer', {}).get('zpl_layout', {})
            }
            
            printer = UniversalPrinter(test_config)
            success = printer.print_label('TEST123456', '512G')
            
            if success:
                QMessageBox.information(self, '成功', '测试打印已发送！\n请检查打印机输出。')
            else:
                QMessageBox.warning(self, '失败', '测试打印失败，请检查打印机连接。')
            
        except Exception as e:
            logger.error(f"测试打印失败: {e}")
            QMessageBox.critical(self, '错误', f'测试打印失败: {e}')
    
    def save_config(self):
        """保存配置到config.yaml"""
        if self.printer_combo.currentIndex() < 0:
            QMessageBox.warning(self, '警告', '请先选择打印机！')
            return

        try:
            from ..utils.config import config as app_config
            # 与程序实际读取的文件保持同一份，避免存到相对路径 config/config.yaml 而读的是别的文件
            config_path = str(app_config.config_path)

            # 读取现有配置
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            # 更新打印机配置
            protocol = 'auto'
            if self.zpl_radio.isChecked():
                protocol = 'zpl'
            elif self.tspl_radio.isChecked():
                protocol = 'tspl'

            if 'printer' not in config:
                config['printer'] = {}

            config['printer']['printer_name'] = self.printer_combo.currentText()
            config['printer']['dpi'] = int(self.dpi_combo.currentText())
            config['printer']['protocol'] = protocol
            config['printer']['paper_width'] = self.width_spin.value()
            config['printer']['paper_height'] = self.height_spin.value()
            config['printer']['paper_width_mm'] = self.width_spin.value()
            config['printer']['paper_height_mm'] = self.height_spin.value()

            # 保存配置：逐行只改需要动的标量，保住文件里的注释和键顺序
            # 不能用 yaml.dump 整体重写 —— 那会把用户写的注释全部抹掉、键顺序打乱
            updates = {
                'printer_name': self.printer_combo.currentText(),
                'dpi': int(self.dpi_combo.currentText()),
                'protocol': protocol,
                'paper_width': self.width_spin.value(),
                'paper_height': self.height_spin.value(),
                'paper_width_mm': self.width_spin.value(),
                'paper_height_mm': self.height_spin.value(),
            }

            with open(config_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            applied, lines = _update_printer_section(lines, updates)

            missing = set(updates) - applied
            if missing:
                # printe 段里缺的键，追加到该段末尾
                lines = _append_to_printer_section(lines, {k: updates[k] for k in missing})

            with open(config_path, 'w', encoding='utf-8') as f:
                f.writelines(lines)

            # 立刻让内存中的单例重新读盘，改完即生效，无需重启
            app_config.load_config(force=True)

            logger.info(f"打印机配置已保存: {self.printer_combo.currentText()} -> {config_path}")
            QMessageBox.information(self, '成功', '配置已保存并立即生效！')
            self.accept()
            
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            QMessageBox.critical(self, '错误', f'保存配置失败: {e}')
