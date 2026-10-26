from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                             QTableWidget, QTableWidgetItem, QHeaderView,
                             QTextEdit, QMessageBox, QSplitter, QLabel)
from PyQt5.QtCore import Qt
from ..utils.test_log_capture import TestLogManager
from ..utils.logger import logger


class TestLogsWindow(QDialog):
    """测试日志查看窗口"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('查看日志')
        self.resize(1000, 700)
        self.current_logs = []
        self._setup_ui()
        self._load_logs()

    def _setup_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # 顶部操作栏
        top_layout = QHBoxLayout()
        refresh_btn = QPushButton('🔄 刷新')
        refresh_btn.clicked.connect(self._load_logs)
        top_layout.addWidget(refresh_btn)

        self.count_label = QLabel('共 0 条记录')
        top_layout.addWidget(self.count_label)
        top_layout.addStretch()

        export_btn = QPushButton('📋 导出当前日志')
        export_btn.clicked.connect(self._export_current_log)
        top_layout.addWidget(export_btn)

        layout.addLayout(top_layout)

        # 分割器：上方列表 + 下方详情
        splitter = QSplitter(Qt.Vertical)

        # 上方：日志记录列表
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['设备SN', '测试项', '测试时间', '测试结果', '文件名'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.table)

        # 下方：日志详情
        detail_widget = QVBoxLayout()
        detail_label = QLabel('📄 日志详情（点击上方记录查看）')
        detail_label.setStyleSheet('font-weight: bold; padding: 5px;')
        detail_widget.addWidget(detail_label)

        self.log_detail = QTextEdit()
        self.log_detail.setReadOnly(True)
        self.log_detail.setStyleSheet(
            "QTextEdit{background-color:#2c3e50;color:#ecf0f1;"
            "font-family:Consolas,Monaco,monospace;font-size:12px;}")
        self.log_detail.setPlaceholderText('选择一条记录查看详细日志...')
        detail_widget.addWidget(self.log_detail)

        detail_container = QVBoxLayout()
        from PyQt5.QtWidgets import QWidget
        detail_w = QWidget()
        detail_w.setLayout(detail_widget)
        splitter.addWidget(detail_w)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

    def _load_logs(self):
        """加载测试日志列表"""
        try:
            self.current_logs = TestLogManager.list_test_logs()
            self.count_label.setText(f'共 {len(self.current_logs)} 条记录')
            self._display_logs()
        except Exception as e:
            QMessageBox.critical(self, '错误', f'加载日志失败: {e}')
            logger.error(f"加载测试日志失败: {e}")

    def _display_logs(self):
        """显示日志列表"""
        self.table.setRowCount(len(self.current_logs))
        for i, log in enumerate(self.current_logs):
            self.table.setItem(i, 0, QTableWidgetItem(log['sn']))
            self.table.setItem(i, 1, QTableWidgetItem(log.get('test_type', '')))
            self.table.setItem(i, 2, QTableWidgetItem(log['start_time']))

            # 结果列加颜色
            result_item = QTableWidgetItem(log['result'])
            if log['result'] == 'PASS':
                result_item.setForeground(Qt.darkGreen)
            elif log['result'] == 'FAIL':
                result_item.setForeground(Qt.red)
            self.table.setItem(i, 3, result_item)

            self.table.setItem(i, 4, QTableWidgetItem(log['filename']))

    def _on_selection_changed(self):
        """选中记录时显示详细日志"""
        selected = self.table.selectedItems()
        if not selected:
            return

        row = self.table.currentRow()
        if row < 0 or row >= len(self.current_logs):
            return

        log_record = self.current_logs[row]
        try:
            content = TestLogManager.read_log_content(log_record['filepath'])
            self.log_detail.setPlainText(content)
        except Exception as e:
            self.log_detail.setPlainText(f"读取日志失败: {e}")
            logger.error(f"读取日志文件失败: {e}")

    def _export_current_log(self):
        """导出当前显示的日志"""
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, '提示', '请先选择一条日志记录')
            return

        content = self.log_detail.toPlainText()
        if not content:
            QMessageBox.warning(self, '提示', '当前没有可导出的日志内容')
            return

        from PyQt5.QtWidgets import QFileDialog
        log_record = self.current_logs[row]
        default_filename = f"{log_record['sn']}_{log_record['start_time'].replace(':', '').replace(' ', '_')}.log"

        filename, _ = QFileDialog.getSaveFileName(
            self, '导出日志',
            default_filename,
            'Log Files (*.log);;Text Files (*.txt)'
        )

        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(content)
                QMessageBox.information(self, '成功', f'日志已导出到:\n{filename}')
            except Exception as e:
                QMessageBox.critical(self, '错误', f'导出失败: {e}')
