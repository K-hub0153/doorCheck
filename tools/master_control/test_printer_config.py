#!/usr/bin/env python3
"""
打印机配置对话框测试工具
演示 Zebra ZPL 打印机识别和配置功能
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget, QTextEdit
from src.master_control.gui.printer_selection_dialog import PrinterSelectionDialog
from src.master_control.utils.config import Config
from src.master_control.utils.logger import logger


class TestWindow(QMainWindow):
    """测试窗口"""
    
    def __init__(self):
        super().__init__()
        self.config = Config()
        self.init_ui()
    
    def init_ui(self):
        self.setWindowTitle("打印机配置测试工具")
        self.setGeometry(200, 200, 600, 400)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        # 日志输出
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)
        
        # 打开配置按钮
        config_btn = QPushButton("🖨️ 打开打印机配置对话框")
        config_btn.clicked.connect(self.open_printer_config)
        layout.addWidget(config_btn)
        
        # 显示当前配置
        self.show_current_config()
    
    def show_current_config(self):
        """显示当前打印机配置"""
        printer_cfg = self.config.printer_config
        self.log_text.clear()
        self.log_text.append("=== 当前打印机配置 ===")
        self.log_text.append(f"打印机名称: {printer_cfg.get('printer_name', '未设置')}")
        self.log_text.append(f"协议: {printer_cfg.get('protocol', '未设置')}")
        self.log_text.append(f"DPI: {printer_cfg.get('dpi', '未设置')}")
        self.log_text.append(f"打印机IP: {printer_cfg.get('printer_ip', '未设置')}")
        self.log_text.append(f"端口: {printer_cfg.get('printer_port', '未设置')}")
        self.log_text.append("\n")
    
    def open_printer_config(self):
        """打开打印机配置对话框"""
        self.log_text.append("正在打开打印机配置对话框...")
        
        current_cfg = {
            'printer_name': self.config.printer_config.get('printer_name', ''),
            'protocol': self.config.printer_config.get('protocol', ''),
            'printer_port': self.config.printer_config.get('printer_port', 9100),
        }
        
        dialog = PrinterSelectionDialog(current_cfg, self)
        
        if dialog.exec_() == dialog.Accepted:
            selected = dialog.get_selected_printer()
            if selected:
                self.log_text.append("\n=== 选择的打印机 ===")
                self.log_text.append(f"类型: {selected['type']}")
                if selected['type'] == 'network':
                    self.log_text.append(f"IP: {selected['printer_ip']}")
                    self.log_text.append(f"端口: {selected['printer_port']}")
                else:
                    self.log_text.append(f"名称: {selected['printer_name']}")
                self.log_text.append(f"协议: {selected['protocol'].upper()}")
                self.log_text.append(f"DPI: {selected['dpi']}")
                
                # 检测是否是 Zebra 打印机
                printer_name = selected.get('printer_name', '')
                if printer_name:
                    name_lower = printer_name.lower()
                    if 'zebra' in name_lower or 'zdesigner' in name_lower:
                        self.log_text.append("\n✓ 检测到 Zebra ZPL 打印机！")
                
                self.log_text.append("\n配置已更新（注意：需要保存到 config.yaml 才能持久化）")
        else:
            self.log_text.append("用户取消了选择")


def main():
    """主函数"""
    app = QApplication(sys.argv)
    
    print("=== 打印机配置测试工具 ===")
    print("功能：")
    print("1. 自动识别 Zebra ZPL 打印机（关键词：zebra, zdesigner, zpl）")
    print("2. 自动识别 Xprinter/TSC TSPL 打印机（关键词：xprinter, tsc, tspl）")
    print("3. 显示推荐 DPI（ZPL: 600, TSPL: 203）")
    print("4. 支持测试打印")
    print("5. 支持网络和本地打印机")
    print()
    
    window = TestWindow()
    window.show()
    
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
