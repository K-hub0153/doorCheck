#!/usr/bin/env python3
"""
打印机检测和配置测试工具
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import win32print
from src.hardware.universal_printer import UniversalPrinter


def list_printers():
    """列出所有系统打印机"""
    print("=" * 60)
    print("系统打印机列表")
    print("=" * 60)
    
    try:
        printers = [printer[2] for printer in win32print.EnumPrinters(2)]
        default = win32print.GetDefaultPrinter()
        
        for i, name in enumerate(printers, 1):
            is_default = " (默认)" if name == default else ""
            
            # 检测协议
            config = {'printer': {'printer_name': name, 'protocol': 'auto', 'dpi': 600}}
            p = UniversalPrinter(config)
            
            print(f"{i}. {name}{is_default}")
            print(f"   协议: {p.protocol.upper()}")
            print()
            
    except Exception as e:
        print(f"错误: {e}")


def test_zpl_commands():
    """测试ZPL指令生成"""
    print("=" * 60)
    print("ZPL 指令测试 (Zebra 600 DPI)")
    print("=" * 60)
    
    config = {
        'printer': {
            'printer_name': 'ZDesigner Test',
            'protocol': 'zpl',
            'dpi': 600,
            'paper_width': 50,
            'paper_height': 30,
            'zpl_layout': {
                'qrcode': {'x': 723, 'y': 204, 'magnification': 15},
                'date': {'x': 354, 'y': 366, 'font_height': 80, 'font_width': 80},
                'sn': {'x': 100, 'y': 605, 'font_height': 80, 'font_width': 80},
            }
        }
    }
    
    p = UniversalPrinter(config)
    commands = p._build_zpl_commands('TEST123456')
    print(commands)


def test_tspl_commands():
    """测试TSPL指令生成"""
    print("=" * 60)
    print("TSPL 指令测试 (Xprinter 203 DPI)")
    print("=" * 60)
    
    config = {
        'printer': {
            'printer_name': 'Xprinter Test',
            'protocol': 'tspl',
            'dpi': 203,
            'paper_width': 50,
            'paper_height': 30,
            'tspl_layout': {
                'qrcode': {'x': 245, 'y': 69, 'cell_width': 6},
                'date': {'x': 120, 'y': 124, 'font': '1', 'rotation': 0, 'x_scale': 1, 'y_scale': 1},
                'sn': {'x': 34, 'y': 178, 'font': '1', 'rotation': 0, 'x_scale': 1, 'y_scale': 1},
            }
        }
    }
    
    p = UniversalPrinter(config)
    commands = p._build_tspl_commands('TEST123456')
    print(commands)


if __name__ == '__main__':
    list_printers()
    print()
    test_zpl_commands()
    print()
    test_tspl_commands()
