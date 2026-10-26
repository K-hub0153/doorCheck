from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QTextEdit,
                             QComboBox, QSpinBox, QGroupBox, QTableWidget,
                             QTableWidgetItem, QHeaderView, QMessageBox, QCheckBox,
                             QFileDialog)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QFont
from datetime import datetime
import csv

from src.qrcode_print.scanner_listener import ScannerInputListener
from src.qrcode_print.template_engine import TemplateEngine
from src.qrcode_print.config import QRCodePrintConfig
from src.utils.logger import logger
from src.qrcode_print.validators import validate_qrcode, sanitize_input, extract_sn
from src.qrcode_print.print_preview_dialog import PrintPreviewDialog
from src.qrcode_print.template_editor_dialog import TemplateEditorDialog


class QRCodePrintWindow(QMainWindow):
    """扫码打印主窗口"""

    # 协议对应的打印机名称关键词（与 universal_printer.py 保持一致）
    _PROTO_KEYWORDS = {
        'tspl': ['xprinter', 'xp-t', 'xp-n', 'xp-d', 'tsc', 'tspl'],
        'zpl':  ['zdesigner', 'zebra', 'zpl'],
    }

    # 推断顺序：zpl 优先，与 UniversalPrinter._PROTOCOL_RULES 一致
    # （名称同时含两类关键词时，两处必须得出同一结论）
    _PROTO_ORDER = ('zpl', 'tspl')

    @classmethod
    def _guess_protocol(cls, printer_name: str) -> str:
        """按打印机名推断协议；认不出来时默认 zpl（与 UniversalPrinter 一致）"""
        if printer_name:
            name = printer_name.lower()
            for proto in cls._PROTO_ORDER:
                if any(kw in name for kw in cls._PROTO_KEYWORDS.get(proto, [])):
                    return proto
        return 'zpl'

    # 信号定义
    qrcode_scanned = pyqtSignal(str)  # 扫码信号

    def __init__(self, config: QRCodePrintConfig):
        super().__init__()
        self.config = config

        # 初始化模板引擎
        self.template_engine = TemplateEngine(config)

        # 打印历史
        self.print_history = []

        # 自动打印开关（从配置读取初始值）
        self.auto_print = config.get('qrcode_print.auto_print', True)

        # 初始化UI
        self.init_ui()

        # 初始化扫码监听器（传递self作为parent，限制只监听本窗口）
        self.scanner_listener = ScannerInputListener(
            callback=self.on_qrcode_scanned,
            timeout=0.05,  # 相邻字符间隔阈值（秒），与载荷长度无关
            parent=self
        )

        # 连接信号
        self.qrcode_scanned.connect(self.handle_qrcode)

        # 启动监听
        self.scanner_listener.start()

        # 检测打印机状态
        self.check_printer_status()

        logger.info("扫码打印窗口已启动")

    def init_ui(self):
        """初始化UI"""
        self.setWindowTitle("扫码打印工具")
        self.setGeometry(100, 100, 900, 700)

        # 中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        main_layout = QVBoxLayout(central_widget)

        # 1. 状态栏
        status_group = self.create_status_group()
        main_layout.addWidget(status_group)

        # 2. 扫码输入区
        input_group = self.create_input_group()
        main_layout.addWidget(input_group)

        # 3. 打印控制区
        control_group = self.create_control_group()
        main_layout.addWidget(control_group)

        # 4. 打印历史区
        history_group = self.create_history_group()
        main_layout.addWidget(history_group)

        # 5. 日志区
        log_group = self.create_log_group()
        main_layout.addWidget(log_group)

    def create_status_group(self):
        """创建状态栏"""
        group = QGroupBox("系统状态")
        layout = QHBoxLayout()

        # 打印机状态
        self.printer_status_label = QLabel("打印机: 未知")
        layout.addWidget(self.printer_status_label)

        # 扫码枪状态
        self.scanner_status_label = QLabel("扫码枪: 监听中")
        layout.addWidget(self.scanner_status_label)

        # 打印计数
        self.print_count_label = QLabel("已打印: 0")
        layout.addWidget(self.print_count_label)

        layout.addStretch()

        group.setLayout(layout)
        return group

    def create_input_group(self):
        """创建输入区"""
        group = QGroupBox("扫码输入")
        layout = QVBoxLayout()

        # 提示标签
        tip_label = QLabel("请使用扫码枪扫描二维码")
        layout.addWidget(tip_label)

        # 输入框（大字体）
        self.qrcode_input = QLineEdit()
        self.qrcode_input.setPlaceholderText("等待扫码...")
        self.qrcode_input.setFont(QFont("Arial", 16))
        self.qrcode_input.returnPressed.connect(self.on_manual_input)

        # 禁用输入法，防止扫码枪输入被转换
        self.qrcode_input.setAttribute(Qt.WA_InputMethodEnabled, False)

        # 在输入框上也安装事件过滤器（双重保护）
        self.qrcode_input.installEventFilter(self)

        layout.addWidget(self.qrcode_input)

        group.setLayout(layout)
        return group

    def eventFilter(self, obj, event):
        """输入框事件过滤器 - 只允许ASCII字符"""
        if obj == self.qrcode_input and event.type() == 6:  # QEvent.KeyPress = 6
            text = event.text()
            # 只允许ASCII字符通过
            if text and not text.isascii():
                return True  # 拦截非ASCII字符
        return super().eventFilter(obj, event)

    def create_control_group(self):
        """创建控制区"""
        group = QGroupBox("打印控制")
        layout = QVBoxLayout()

        # 第一行：打印机选择
        row0 = QHBoxLayout()
        row0.addWidget(QLabel("打印机:"))
        self.printer_combo = QComboBox()
        self.printer_combo.setMinimumWidth(200)
        self.printer_combo.currentTextChanged.connect(self._on_printer_changed)
        row0.addWidget(self.printer_combo)

        row0.addWidget(QLabel("协议:"))
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(['AUTO（自动）', 'TSPL (Xprinter/TSC)', 'ZPL (Zebra)'])
        self.protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        row0.addWidget(self.protocol_combo)

        row0.addWidget(QLabel("DPI:"))
        self.dpi_combo = QComboBox()
        self.dpi_combo.addItems(['203', '300', '600'])
        self.dpi_combo.currentTextChanged.connect(self._on_dpi_changed)
        row0.addWidget(self.dpi_combo)

        refresh_btn = QPushButton("刷新")
        refresh_btn.setMaximumWidth(50)
        refresh_btn.clicked.connect(self._load_printers)
        row0.addWidget(refresh_btn)

        row0.addStretch()
        layout.addLayout(row0)
        self._load_printers()

        # 第一行：模板和份数
        row1 = QHBoxLayout()

        # 模板选择
        row1.addWidget(QLabel("模板:"))
        self.template_combo = QComboBox()
        for name, config in self.template_engine.templates.items():
            desc = config.get('description', name)
            self.template_combo.addItem(f"{name} - {desc}", name)
        row1.addWidget(self.template_combo)

        # 打印份数
        row1.addWidget(QLabel("份数:"))
        self.copies_spinbox = QSpinBox()
        self.copies_spinbox.setMinimum(1)
        self.copies_spinbox.setMaximum(10)
        self.copies_spinbox.setValue(1)
        row1.addWidget(self.copies_spinbox)

        # 自动打印开关
        self.auto_print_checkbox = QCheckBox("自动打印")
        self.auto_print_checkbox.setChecked(self.auto_print)
        self.auto_print_checkbox.toggled.connect(self._on_auto_print_toggled)
        row1.addWidget(self.auto_print_checkbox)

        row1.addStretch()
        layout.addLayout(row1)

        # 第二行：操作按钮
        row2 = QHBoxLayout()

        # 打印按钮
        self.manual_print_btn = QPushButton("打印")
        self.manual_print_btn.clicked.connect(self.on_manual_print)
        row2.addWidget(self.manual_print_btn)

        # 预览按钮
        preview_btn = QPushButton("打印预览")
        preview_btn.clicked.connect(self.show_preview)
        row2.addWidget(preview_btn)

        # 模板编辑按钮
        edit_tmpl_btn = QPushButton("编辑模板")
        edit_tmpl_btn.clicked.connect(self.show_template_editor)
        row2.addWidget(edit_tmpl_btn)

        # 清空按钮
        clear_btn = QPushButton("清空输入")
        clear_btn.clicked.connect(lambda: self.qrcode_input.clear())
        row2.addWidget(clear_btn)

        row2.addStretch()
        layout.addLayout(row2)

        group.setLayout(layout)
        return group

    def create_history_group(self):
        """创建历史记录区"""
        group = QGroupBox("打印历史")
        layout = QVBoxLayout()

        # 历史表格
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(4)
        self.history_table.setHorizontalHeaderLabels(["时间", "二维码", "模板", "状态"])
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.history_table)

        # 按钮布局
        button_layout = QHBoxLayout()

        # 导出按钮
        export_btn = QPushButton("导出CSV")
        export_btn.clicked.connect(self.export_history)
        button_layout.addWidget(export_btn)

        # 清空历史按钮
        clear_history_btn = QPushButton("清空历史")
        clear_history_btn.clicked.connect(self.clear_history)
        button_layout.addWidget(clear_history_btn)

        button_layout.addStretch()

        layout.addLayout(button_layout)

        group.setLayout(layout)
        return group

    def create_log_group(self):
        """创建日志区"""
        group = QGroupBox("操作日志")
        layout = QVBoxLayout()

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        layout.addWidget(self.log_text)

        group.setLayout(layout)
        return group

    def _load_printers(self):
        """扫描系统打印机列表"""
        import subprocess, sys
        self._all_printers = []
        try:
            if sys.platform == 'win32':
                import win32print
                self._all_printers = [p[2] for p in win32print.EnumPrinters(2)]
            else:
                result = subprocess.run(['lpstat', '-p'], capture_output=True, text=True, timeout=5)
                for line in result.stdout.splitlines():
                    if line.startswith('printer '):
                        self._all_printers.append(line.split()[1])
        except Exception as e:
            logger.warning(f"扫描打印机失败: {e}")

        # 恢复已保存的协议
        saved_proto = self.config.get('printer.protocol', 'auto')
        self.protocol_combo.blockSignals(True)
        if saved_proto == 'tspl':
            self.protocol_combo.setCurrentIndex(1)
        elif saved_proto == 'zpl':
            self.protocol_combo.setCurrentIndex(2)
        else:
            self.protocol_combo.setCurrentIndex(0)
        self.protocol_combo.blockSignals(False)

        # 恢复已保存的 DPI
        saved_dpi = str(self.config.get('printer.zpl_dpi', 203))
        self.dpi_combo.blockSignals(True)
        idx = self.dpi_combo.findText(saved_dpi)
        self.dpi_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.dpi_combo.blockSignals(False)

        # 根据协议过滤并填充打印机列表
        self._refresh_printer_combo()

    def _refresh_printer_combo(self):
        """根据当前协议筛选打印机并更新下拉框"""
        proto_map = {0: 'auto', 1: 'tspl', 2: 'zpl'}
        proto = proto_map.get(self.protocol_combo.currentIndex(), 'auto')
        all_printers = getattr(self, '_all_printers', [])

        # 按协议过滤
        if proto != 'auto' and all_printers:
            keywords = self._PROTO_KEYWORDS.get(proto, [])
            filtered = [p for p in all_printers if any(kw in p.lower() for kw in keywords)]
            printers = filtered if filtered else all_printers  # 无匹配时显示全部
        else:
            printers = all_printers

        saved_name = self.config.get('printer.printer_name', '')

        self.printer_combo.blockSignals(True)
        self.printer_combo.clear()
        if printers:
            self.printer_combo.addItems(printers)
            if saved_name in printers:
                self.printer_combo.setCurrentText(saved_name)
        else:
            self.printer_combo.addItem('模拟打印机 (macOS/Linux)')
        self.printer_combo.blockSignals(False)

    def _on_printer_changed(self, printer_name: str):
        """打印机切换时保存配置并重新初始化引擎"""
        if not printer_name:
            return
        self.config.save_printer_settings(printer_name=printer_name)
        self.template_engine = TemplateEngine(self.config)
        self.add_log(f"打印机已切换: {printer_name}")
        self.check_printer_status()

    def _on_protocol_changed(self, index: int):
        """协议切换时保存配置、重新初始化引擎、并按协议过滤打印机列表"""
        proto_map = {0: 'auto', 1: 'tspl', 2: 'zpl'}
        proto = proto_map.get(index, 'auto')
        self.config.save_printer_settings(protocol=proto)
        self.template_engine = TemplateEngine(self.config)
        self._refresh_printer_combo()
        self.add_log(f"打印协议已切换: {proto.upper()}")

    def _on_dpi_changed(self, dpi_text: str):
        """DPI 切换时保存配置并重新初始化引擎"""
        try:
            dpi = int(dpi_text)
        except ValueError:
            return
        self.config.save_printer_settings(zpl_dpi=dpi)
        self.template_engine = TemplateEngine(self.config)
        self.add_log(f"ZPL DPI 已设置为: {dpi}")

    def on_qrcode_scanned(self, qrcode_data: str):
        """
        扫码回调（在监听器线程中执行）
        使用信号-槽机制转到主线程
        """
        logger.debug(f"on_qrcode_scanned 被调用: {qrcode_data}")
        self.qrcode_scanned.emit(qrcode_data)

    def handle_qrcode(self, qrcode_data: str):
        """
        处理扫码数据（在主线程中执行）
        """
        logger.info(f"handle_qrcode 被调用: {qrcode_data}")

        # 清理输入
        qrcode_data = sanitize_input(qrcode_data)
        logger.debug(f"清理后的数据: {qrcode_data}")

        # 从 URL 形式的载荷中提取设备 SN（纯 SN 原样返回）
        sn, err = extract_sn(qrcode_data)
        if sn is None:
            self.add_log(f"❌ 解析失败: {err}")
            QMessageBox.warning(self, "解析失败", err)
            return
        if sn != qrcode_data:
            logger.info(f"从 URL 提取设备 SN: {sn}")
            self.add_log(f"识别到绑定码，提取 SN: {sn}")

        # 验证格式（针对提取后的 SN）
        valid, error_msg = validate_qrcode(sn)
        if not valid:
            self.add_log(f"❌ 验证失败: {error_msg}")
            QMessageBox.warning(self, "验证失败", f"二维码格式不正确:\n{error_msg}")
            return

        # 先清空输入框（清除可能已经输入的错误字符）
        self.qrcode_input.clear()

        # 显示在输入框（SN，而非原始 URL；打印与预览都基于此）
        logger.info(f"设置输入框文本: {sn}")
        self.qrcode_input.setText(sn)

        # 记录日志
        self.add_log(f"扫描到二维码: {sn}")

        # 自动打印
        if self.auto_print:
            self.print_qrcode(sn)

    def _on_auto_print_toggled(self, enabled: bool):
        self.auto_print = enabled
        state = "开启" if enabled else "关闭"
        self.add_log(f"自动打印已{state}")
        self._save_auto_print(enabled)

    def _save_auto_print(self, enabled: bool):
        if not self.config.save_auto_print(enabled):
            logger.warning("保存自动打印设置失败")

    def on_manual_input(self):
        """手动输入回车"""
        qrcode_data = self.qrcode_input.text().strip()
        if qrcode_data:
            self.add_log(f"手动输入: {qrcode_data}")
            self.print_qrcode(qrcode_data)

    def on_manual_print(self):
        """手动打印按钮"""
        qrcode_data = self.qrcode_input.text().strip()
        if not qrcode_data:
            QMessageBox.warning(self, "警告", "请先输入二维码内容")
            return
        self.print_qrcode(qrcode_data)

    def print_qrcode(self, qrcode_data: str):
        """
        打印二维码标签
        """
        # 统一在此处提取 SN，覆盖扫码、手动输入、手动打印三条路径
        # （纯 SN 原样返回，重复调用无副作用）
        qrcode_data, err = extract_sn(qrcode_data)
        if qrcode_data is None:
            self.add_log(f"❌ 解析失败: {err}")
            QMessageBox.warning(self, "解析失败", err)
            return

        try:
            # 获取模板
            template_name = self.template_combo.currentData()

            # 获取份数
            copies = self.copies_spinbox.value()

            # 打印
            self.add_log(f"开始打印: {qrcode_data} (模板: {template_name or '自动'}, 份数: {copies})")

            # 直接取界面当前选择，不再依赖配置里存的值
            proto_map = {0: 'auto', 1: 'tspl', 2: 'zpl'}
            ui_printer = self.printer_combo.currentText()
            ui_protocol = proto_map.get(self.protocol_combo.currentIndex(), 'auto')
            # 选 AUTO 时按打印机名推断，避免把 'auto' 直传下去被当成 TSPL
            if ui_protocol == 'auto':
                ui_protocol = self._guess_protocol(ui_printer)
            try:
                ui_dpi = int(self.dpi_combo.currentText())
            except ValueError:
                ui_dpi = None

            success = self.template_engine.print_label(
                qrcode_data=qrcode_data,
                template_name=template_name,
                copies=copies,
                printer_name=ui_printer,
                protocol=ui_protocol,
                dpi=ui_dpi,
            )

            if success:
                self.add_log(f"✅ 打印任务已发送")
                self.add_history(qrcode_data, template_name or "自动", "已发送")
                self.update_print_count()
            else:
                self.add_log(f"❌ 打印失败")
                self.add_history(qrcode_data, template_name or "自动", "失败")
                QMessageBox.critical(self, "错误", "打印失败，请检查打印机状态")

        except Exception as e:
            self.add_log(f"❌ 打印异常: {e}")
            self.add_history(qrcode_data, "未知", f"异常: {e}")
            QMessageBox.critical(self, "错误", f"打印异常:\n{e}")

    def add_log(self, message: str):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")

        # 自动滚动到底部
        self.log_text.moveCursor(self.log_text.textCursor().End)

    def add_history(self, qrcode_data: str, template: str, status: str):
        """添加打印历史"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        row = self.history_table.rowCount()
        self.history_table.insertRow(row)

        self.history_table.setItem(row, 0, QTableWidgetItem(timestamp))
        self.history_table.setItem(row, 1, QTableWidgetItem(qrcode_data))
        self.history_table.setItem(row, 2, QTableWidgetItem(template))
        self.history_table.setItem(row, 3, QTableWidgetItem(status))

        # 滚动到最新记录
        self.history_table.scrollToBottom()

    def clear_history(self):
        """清空历史"""
        self.history_table.setRowCount(0)
        self.add_log("已清空打印历史")

    def update_print_count(self):
        """更新打印计数"""
        count = self.history_table.rowCount()
        self.print_count_label.setText(f"已打印: {count}")

    def show_template_editor(self):
        """打开模板编辑器"""
        # 暂停扫码监听，避免编辑器内的拖拽事件触发 eventFilter 递归
        self.scanner_listener.stop()
        try:
            dialog = TemplateEditorDialog(self.config, self)
            dialog.exec_()
        finally:
            # 无论编辑器如何关闭都恢复监听
            self.scanner_listener.start()
        # 编辑器关闭后重新加载模板引擎，确保使用最新配置
        self.config.load_config()
        self.template_engine = TemplateEngine(self.config)
        # 同步更新模板下拉框
        self.template_combo.clear()
        for name, tmpl in self.template_engine.templates.items():
            desc = tmpl.get('description', name)
            self.template_combo.addItem(f"{name} - {desc}", name)
        self.add_log("模板已更新")

    def show_preview(self):
        """显示打印预览"""
        qrcode_data = self.qrcode_input.text().strip()
        if not qrcode_data:
            QMessageBox.warning(self, "警告", "请先输入二维码内容")
            return

        # 提取 SN（与打印路径一致，预览所见即打印所得）
        qrcode_data, err = extract_sn(qrcode_data)
        if qrcode_data is None:
            QMessageBox.warning(self, "解析失败", err)
            return

        # 验证格式
        valid, error_msg = validate_qrcode(qrcode_data)
        if not valid:
            QMessageBox.warning(self, "验证失败", f"二维码格式不正确:\n{error_msg}")
            return

        try:
            # 获取模板名称
            template_name = self.template_combo.currentData()
            if not template_name:
                template_name = self.template_engine.select_template(qrcode_data)

            # 显示预览对话框
            dialog = PrintPreviewDialog(qrcode_data, template_name, self)
            dialog.exec_()

        except Exception as e:
            self.add_log(f"❌ 预览失败: {e}")
            QMessageBox.critical(self, "错误", f"预览失败:\n{e}")

    def export_history(self):
        """导出打印历史到CSV"""
        if self.history_table.rowCount() == 0:
            QMessageBox.information(self, "提示", "打印历史为空，无需导出")
            return

        # 选择保存位置
        default_filename = f"print_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "导出打印历史",
            default_filename,
            "CSV Files (*.csv)"
        )

        if not filename:
            return

        try:
            with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                # 写入表头
                writer.writerow(['时间', '二维码', '模板', '状态'])

                # 写入数据
                for row in range(self.history_table.rowCount()):
                    row_data = []
                    for col in range(self.history_table.columnCount()):
                        item = self.history_table.item(row, col)
                        row_data.append(item.text() if item else '')
                    writer.writerow(row_data)

            self.add_log(f"✅ 历史记录已导出: {filename}")
            QMessageBox.information(self, "成功", f"历史记录已导出到:\n{filename}")

        except Exception as e:
            self.add_log(f"❌ 导出失败: {e}")
            QMessageBox.critical(self, "错误", f"导出失败:\n{e}")

    def closeEvent(self, event):
        """窗口关闭事件"""
        # 停止扫码监听
        self.scanner_listener.stop()
        logger.info("扫码打印窗口已关闭")
        event.accept()

    def check_printer_status(self):
        """检测打印机状态"""
        try:
            import platform
            if platform.system() == 'Windows':
                try:
                    import win32print
                    # 获取默认打印机
                    printer_name = win32print.GetDefaultPrinter()

                    # 尝试打开打印机句柄
                    hPrinter = win32print.OpenPrinter(printer_name)
                    win32print.ClosePrinter(hPrinter)

                    # 打印机可用
                    self.printer_status_label.setText(f"打印机: ✅ {printer_name}")
                    self.printer_status_label.setStyleSheet("color: #4CAF50;")
                    logger.info(f"检测到打印机: {printer_name}")

                except Exception as e:
                    # 打印机不可用
                    self.printer_status_label.setText("打印机: ❌ 未连接")
                    self.printer_status_label.setStyleSheet("color: #F44336;")
                    logger.warning(f"打印机检测失败: {e}")
            else:
                # 非Windows系统（macOS/Linux模拟模式）
                self.printer_status_label.setText("打印机: 🔧 模拟模式")
                self.printer_status_label.setStyleSheet("color: #FF9800;")
                logger.info("非Windows系统，使用模拟模式")

        except Exception as e:
            self.printer_status_label.setText("打印机: ⚠️ 检测失败")
            self.printer_status_label.setStyleSheet("color: #FF9800;")
            logger.error(f"打印机状态检测异常: {e}")
