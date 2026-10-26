"""
打印机选择对话框
支持扫描网络打印机和列出本地打印机
"""
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                             QTableWidget, QTableWidgetItem, QLabel, QMessageBox,
                             QHeaderView, QRadioButton, QButtonGroup, QProgressBar,
                             QComboBox)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from src.master_control.utils.logger import logger

try:
    import win32print
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False


class PrinterScanWorker(QThread):
    """打印机扫描线程"""
    progress = pyqtSignal(int, int)  # (当前进度, 总数)
    found = pyqtSignal(dict)          # 发现的打印机信息
    finished = pyqtSignal()
    
    def __init__(self, subnet: str, port: int = 9100):
        super().__init__()
        self.subnet = subnet
        self.port = port
        self._cancelled = False
    
    def cancel(self):
        self._cancelled = True
    
    def run(self):
        """扫描打印机（优先 CUPS lpinfo，其次端口扫描）"""
        # 1. 尝试 CUPS lpinfo（Linux/WSL/CUPS for Windows）
        cups_printers = self._scan_with_cups()
        if cups_printers:
            logger.info(f"通过 CUPS 发现 {len(cups_printers)} 台打印机")
            total = len(cups_printers)
            for i, printer in enumerate(cups_printers):
                if self._cancelled:
                    break
                self.progress.emit(i + 1, total)
                self.found.emit(printer)
            self.finished.emit()
            return
        
        # 2. Fallback 到端口扫描
        logger.info("CUPS 不可用，使用端口扫描")
        self._scan_with_socket()
        self.finished.emit()
    
    def _scan_with_cups(self):
        """通过 CUPS lpinfo 命令发现打印机"""
        try:
            import subprocess
            result = subprocess.run(
                ['lpinfo', '-v', '--include-schemes', 'socket,ipp,lpd'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                logger.debug(f"lpinfo 不可用: {result.stderr}")
                return []
            
            printers = []
            for line in result.stdout.splitlines():
                # 解析格式: network socket://192.168.1.100:9100
                #           network ipp://192.168.1.101/ipp/print
                #           network lpd://192.168.1.102/queue
                if 'socket://' in line or 'ipp://' in line or 'lpd://' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        uri = parts[1]
                        
                        # 提取 IP 和端口
                        if '://' in uri:
                            scheme, rest = uri.split('://', 1)
                            host_port = rest.split('/')[0]
                            
                            if ':' in host_port:
                                ip, port = host_port.rsplit(':', 1)
                                port = int(port)
                            else:
                                ip = host_port
                                port = 631 if scheme == 'ipp' else 9100
                            
                            printers.append({
                                'ip': ip,
                                'port': port,
                                'type': 'network',
                                'scheme': scheme
                            })
            
            return printers
            
        except FileNotFoundError:
            logger.debug("lpinfo 命令不存在")
            return []
        except Exception as e:
            logger.debug(f"CUPS 扫描失败: {e}")
            return []
    
    def _scan_with_socket(self):
        """通过端口扫描发现打印机"""
        try:
            import ipaddress
            network = ipaddress.IPv4Network(self.subnet, strict=False)
            hosts = list(network.hosts())
        except Exception as e:
            logger.error(f"解析子网失败: {e}")
            return
        
        total = len(hosts)
        scanned = 0
        
        def check_printer(ip_str: str):
            if self._cancelled:
                return None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                result = sock.connect_ex((ip_str, self.port))
                sock.close()
                
                if result == 0:
                    return {'ip': ip_str, 'port': self.port, 'type': 'network'}
            except:
                pass
            return None
        
        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = {executor.submit(check_printer, str(ip)): ip for ip in hosts}
            
            for future in as_completed(futures):
                if self._cancelled:
                    break
                
                scanned += 1
                self.progress.emit(scanned, total)
                
                result = future.result()
                if result:
                    self.found.emit(result)


class PrinterSelectionDialog(QDialog):
    """打印机选择对话框"""
    
    def __init__(self, current_config: dict, parent=None):
        super().__init__(parent)
        self.current_config = current_config
        self.selected_printer = None
        self.scan_worker = None
        
        self.setWindowTitle("选择打印机")
        self.setMinimumSize(700, 500)
        self.init_ui()
        
        # 自动加载打印机列表
        self.load_local_printers()
    
    def init_ui(self):
        layout = QVBoxLayout()
        
        # 打印机类型选择
        type_layout = QHBoxLayout()
        self.type_group = QButtonGroup()
        
        self.radio_network = QRadioButton("网络打印机")
        self.radio_local = QRadioButton("本地打印机")
        self.type_group.addButton(self.radio_network, 1)
        self.type_group.addButton(self.radio_local, 2)
        
        self.radio_local.setChecked(True)  # 默认本地打印机
        self.radio_network.toggled.connect(self.on_type_changed)
        
        type_layout.addWidget(QLabel("打印机类型:"))
        type_layout.addWidget(self.radio_local)
        type_layout.addWidget(self.radio_network)
        type_layout.addStretch()
        
        layout.addLayout(type_layout)
        
        # 扫描控制
        scan_layout = QHBoxLayout()
        self.scan_btn = QPushButton("扫描网络打印机")
        self.scan_btn.clicked.connect(self.start_scan)
        self.scan_btn.setEnabled(False)  # 默认禁用（本地模式）
        self.stop_btn = QPushButton("停止扫描")
        self.stop_btn.clicked.connect(self.stop_scan)
        self.stop_btn.setEnabled(False)
        
        scan_layout.addWidget(self.scan_btn)
        scan_layout.addWidget(self.stop_btn)
        scan_layout.addStretch()
        
        layout.addLayout(scan_layout)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # 打印机列表
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["类型", "名称/IP", "端口", "协议", "推荐DPI"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemDoubleClicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        layout.addWidget(self.table)
        
        # 提示信息
        self.info_label = QLabel("提示: 选择打印机后可测试打印；双击表格行快速确认")
        self.info_label.setStyleSheet("color: gray;")
        layout.addWidget(self.info_label)
        
        # 按钮
        btn_layout = QHBoxLayout()
        
        self.test_btn = QPushButton("🖨️ 测试打印")
        self.test_btn.clicked.connect(self.test_print)
        self.test_btn.setEnabled(False)
        
        ok_btn = QPushButton("确定")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.test_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        
        layout.addLayout(btn_layout)
        self.setLayout(layout)
    
    def on_type_changed(self):
        """切换打印机类型"""
        is_network = self.radio_network.isChecked()
        self.scan_btn.setEnabled(is_network)
        
        if not is_network:
            # 切换到本地打印机，加载列表
            self.load_local_printers()
    
    def on_selection_changed(self):
        """选择改变时启用/禁用测试按钮"""
        has_selection = len(self.table.selectionModel().selectedRows()) > 0
        self.test_btn.setEnabled(has_selection)
    
    # 与 UniversalPrinter._PROTOCOL_RULES 保持一致
    _PROTOCOL_RULES = [
        ('zpl',  ['zdesigner', 'zebra', 'zpl']),
        ('tspl', ['xprinter', 'xp-t', 'xp-n', 'xp-d', 'tsc', 'tspl']),
    ]

    def _guess_protocol(self, name: str) -> str:
        """根据打印机名称推断协议，与 UniversalPrinter 使用相同规则。
        优先返回已保存配置中的协议（名称完全一致时）。"""
        # 1. 已保存配置匹配：名称相同则直接复用保存的协议
        saved_name = self.current_config.get('printer_name', '')
        saved_protocol = self.current_config.get('protocol', '')
        if saved_name and saved_protocol and saved_name == name:
            return saved_protocol.upper()

        # 2. 关键词匹配
        name_lower = name.lower()
        for protocol, keywords in self._PROTOCOL_RULES:
            if any(kw in name_lower for kw in keywords):
                return protocol.upper()

        return 'ZPL'

    def _make_protocol_combo(self, default: str = "ZPL") -> QComboBox:
        """创建协议下拉框，选项 ZPL / TSPL"""
        combo = QComboBox()
        combo.addItems(["ZPL", "TSPL"])
        combo.setCurrentText(default.upper() if default.upper() in ("ZPL", "TSPL") else "ZPL")
        return combo
    
    def load_local_printers(self):
        """加载本地 Windows 打印机"""
        self.table.setRowCount(0)
        
        if not WIN32_AVAILABLE:
            self.info_label.setText("提示: 未安装 pywin32，无法列出本地打印机")
            return
        
        try:
            printers = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL |
                                              win32print.PRINTER_ENUM_CONNECTIONS)
            
            for printer in printers:
                printer_name = printer[2]
                row = self.table.rowCount()
                self.table.insertRow(row)
                
                self.table.setItem(row, 0, QTableWidgetItem("本地"))
                self.table.setItem(row, 1, QTableWidgetItem(printer_name))
                self.table.setItem(row, 2, QTableWidgetItem("-"))
                
                # 根据名称推断协议，优先复用已保存配置
                protocol = self._guess_protocol(printer_name)
                self.table.setCellWidget(row, 3, self._make_protocol_combo(protocol))
                
                # 显示推荐 DPI
                recommended_dpi = 600 if protocol.upper() == 'ZPL' else 203
                self.table.setItem(row, 4, QTableWidgetItem(str(recommended_dpi)))
            
            self.info_label.setText(f"找到 {len(printers)} 台本地打印机")
            
        except Exception as e:
            logger.error(f"加载本地打印机失败: {e}")
            self.info_label.setText(f"错误: {e}")
    
    def start_scan(self):
        """开始扫描网络打印机"""
        # 自动检测当前电脑的网段
        from src.master_control.core.discovery import get_all_interfaces
        interfaces = get_all_interfaces()
        
        if not interfaces:
            QMessageBox.warning(self, "错误", "未检测到可用网卡")
            return
        
        # 如果有多个网卡，让用户选择
        if len(interfaces) > 1:
            from PyQt5.QtWidgets import QInputDialog
            options = [f"{iface['name']} - {iface['ip']} ({iface['subnet']})" 
                      for iface in interfaces]
            choice, ok = QInputDialog.getItem(
                self, "选择网卡", 
                "检测到多个网卡，请选择要扫描的网段：",
                options, 0, False
            )
            if not ok:
                return
            idx = options.index(choice)
            subnet = interfaces[idx]['subnet']
        else:
            subnet = interfaces[0]['subnet']
        
        port = self.current_config.get('printer_port', 9100)
        
        self.table.setRowCount(0)
        self.scan_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.info_label.setText(f"正在扫描子网 {subnet}...")
        
        self.scan_worker = PrinterScanWorker(subnet, port)
        self.scan_worker.progress.connect(self.on_scan_progress)
        self.scan_worker.found.connect(self.on_printer_found)
        self.scan_worker.finished.connect(self.on_scan_finished)
        self.scan_worker.start()
    
    def stop_scan(self):
        """停止扫描"""
        if self.scan_worker:
            self.scan_worker.cancel()
    
    def on_scan_progress(self, current: int, total: int):
        """更新扫描进度"""
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
    
    def on_printer_found(self, printer_info: dict):
        """发现打印机"""
        row = self.table.rowCount()
        self.table.insertRow(row)
        
        scheme = printer_info.get('scheme', 'socket')
        self.table.setItem(row, 0, QTableWidgetItem("网络"))
        self.table.setItem(row, 1, QTableWidgetItem(printer_info['ip']))
        self.table.setItem(row, 2, QTableWidgetItem(str(printer_info['port'])))
        
        # 网络打印机：根据 IP 对应的已保存配置或默认 ZPL（IP 无法推断型号）
        saved_ip = self.current_config.get('printer_ip', '')
        if saved_ip and saved_ip == printer_info['ip']:
            protocol = self._guess_protocol('')  # 触发已保存配置路径需要用 printer_name
            saved_protocol = self.current_config.get('protocol', 'zpl')
            protocol = saved_protocol.upper()
        else:
            protocol = 'ZPL'
        self.table.setCellWidget(row, 3, self._make_protocol_combo(protocol))
        
        # 显示推荐 DPI
        recommended_dpi = 600 if protocol.upper() == 'ZPL' else 203
        self.table.setItem(row, 4, QTableWidgetItem(str(recommended_dpi)))
        
        count = self.table.rowCount()
        scheme_label = f"[{scheme}]" if scheme != 'socket' else ""
        self.info_label.setText(f"已找到 {count} 台网络打印机 {scheme_label}")
    
    def on_scan_finished(self):
        """扫描完成"""
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        
        count = self.table.rowCount()
        if count == 0:
            self.info_label.setText("未找到网络打印机")
        else:
            self.info_label.setText(f"扫描完成，共找到 {count} 台网络打印机")
    
    def test_print(self):
        """测试打印当前选中的打印机"""
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "未选择", "请先选择一台打印机")
            return
        
        row = selected_rows[0].row()
        printer_type = self.table.item(row, 0).text()
        name_or_ip = self.table.item(row, 1).text()
        port_str = self.table.item(row, 2).text()
        
        # 从 QComboBox 读取协议
        combo = self.table.cellWidget(row, 3)
        if combo:
            protocol_text = combo.currentText().upper()
        else:
            item = self.table.item(row, 3)
            protocol_text = item.text().upper() if item else "ZPL"
        
        protocol = 'tspl' if protocol_text == 'TSPL' else 'zpl'
        dpi = 600 if protocol == 'zpl' else 203
        
        # 构建测试配置
        test_config = {
            'enabled': True,
            'protocol': protocol,
            'dpi': dpi,
            'paper_width_mm': 50,
            'paper_height_mm': 30,
        }
        
        if printer_type == "网络":
            test_config['printer_ip'] = name_or_ip
            test_config['printer_port'] = int(port_str)
            test_config['printer_name'] = None
        else:
            test_config['printer_ip'] = None
            test_config['printer_port'] = None
            test_config['printer_name'] = name_or_ip
        
        # 执行测试打印
        try:
            from src.master_control.utils.universal_printer import UniversalPrinter
            printer = UniversalPrinter(test_config)
            
            self.info_label.setText("正在测试打印...")
            success = printer.print_label('TEST123456', '512G')
            
            if success:
                self.info_label.setText("测试打印成功")
                QMessageBox.information(self, "成功", "测试打印成功！请检查打印机输出。")
            else:
                self.info_label.setText("测试打印失败")
                QMessageBox.warning(self, "失败", "测试打印失败，请检查打印机连接和配置。")
        except Exception as e:
            logger.error(f"测试打印异常: {e}")
            self.info_label.setText(f"测试打印异常: {e}")
            QMessageBox.critical(self, "错误", f"测试打印异常:\n{e}")
    
    def accept(self):
        """确认选择"""
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "未选择", "请先选择一台打印机")
            return
        
        row = selected_rows[0].row()
        printer_type = self.table.item(row, 0).text()
        name_or_ip = self.table.item(row, 1).text()
        port_str = self.table.item(row, 2).text()
        
        # 从 QComboBox 读取协议（setCellWidget 设置的）
        combo = self.table.cellWidget(row, 3)
        if combo:
            protocol_text = combo.currentText().upper()
        else:
            # 兼容 QTableWidgetItem 的情况
            item = self.table.item(row, 3)
            protocol_text = item.text().upper() if item else "ZPL"
        
        # 白名单过滤，只允许 zpl/tspl
        protocol = 'tspl' if protocol_text == 'TSPL' else 'zpl'
        
        # 使用推荐 DPI（从表格读取或根据协议推断）
        dpi_item = self.table.item(row, 4)
        if dpi_item:
            try:
                dpi = int(dpi_item.text())
            except (ValueError, AttributeError):
                dpi = 600 if protocol == 'zpl' else 203
        else:
            dpi = 600 if protocol == 'zpl' else 203
        
        if printer_type == "网络":
            self.selected_printer = {
                'type': 'network',
                'printer_ip': name_or_ip,
                'printer_port': int(port_str),
                'printer_name': None,
                'protocol': protocol,
                'dpi': dpi
            }
        else:
            self.selected_printer = {
                'type': 'local',
                'printer_ip': None,
                'printer_port': None,
                'printer_name': name_or_ip,
                'protocol': protocol,
                'dpi': dpi
            }
        
        super().accept()
    
    def get_selected_printer(self) -> dict:
        """获取选择的打印机配置"""
        return self.selected_printer
