from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
                             QDateEdit, QMessageBox, QFileDialog)
from PyQt5.QtCore import Qt, QDate
from ..data.speaker_test_database import TestRecordDB
from ..utils.upload_service import UploadService
from ..utils.logger import logger
import csv


class TestResultsWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('测试结果查询')
        self.resize(1000, 600)
        self.db = TestRecordDB()
        self.upload_service = UploadService()
        self.current_records = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # 查询区域
        search_layout = QHBoxLayout()

        self.sn_input = QLineEdit()
        self.sn_input.setPlaceholderText('输入SN查询')
        search_layout.addWidget(self.sn_input)

        search_btn = QPushButton('查询')
        search_btn.clicked.connect(self.search_by_sn)
        search_layout.addWidget(search_btn)

        search_layout.addStretch()

        # 日期范围选择
        from PyQt5.QtWidgets import QLabel
        start_date_label = QLabel('起始日期:')
        search_layout.addWidget(start_date_label)
        
        self.start_date_input = QDateEdit()
        self.start_date_input.setDate(QDate.currentDate())
        self.start_date_input.setCalendarPopup(True)
        self.start_date_input.setDisplayFormat('yyyy-MM-dd')
        search_layout.addWidget(self.start_date_input)
        
        end_date_label = QLabel('结束日期:')
        search_layout.addWidget(end_date_label)
        
        self.end_date_input = QDateEdit()
        self.end_date_input.setDate(QDate.currentDate())
        self.end_date_input.setCalendarPopup(True)
        self.end_date_input.setDisplayFormat('yyyy-MM-dd')
        search_layout.addWidget(self.end_date_input)
        
        date_search_btn = QPushButton('按日期查询')
        date_search_btn.clicked.connect(self.search_by_date_range)
        search_layout.addWidget(date_search_btn)

        export_btn = QPushButton('导出CSV')
        export_btn.clicked.connect(self.export_csv)
        search_layout.addWidget(export_btn)

        upload_btn = QPushButton('上传')
        upload_btn.clicked.connect(self.upload_records)
        search_layout.addWidget(upload_btn)

        layout.addLayout(search_layout)

        # 结果表格
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(['SN', '创建时间', '测试时间', '设备类型', '测试结果', '备注'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

    def search_by_sn(self):
        sn = self.sn_input.text().strip()
        if sn:
            records = self.db.query_by_sn(sn)
        else:
            records = self.db.query_recent(50)
        self.current_records = records
        self._display_records(records)
    
    def search_by_date_range(self):
        """按日期范围查询"""
        start_date = self.start_date_input.date().toString('yyyy-MM-dd')
        end_date = self.end_date_input.date().toString('yyyy-MM-dd')
        
        # 验证日期范围
        if start_date > end_date:
            QMessageBox.warning(self, '提示', '起始日期不能晚于结束日期')
            return
        
        records = self.db.query_by_date_range(start_date, end_date)
        self.current_records = records
        self._display_records(records)
        
        if records:
            QMessageBox.information(self, '查询结果', f'找到 {len(records)} 条记录')
        else:
            QMessageBox.information(self, '提示', f'{start_date} 至 {end_date} 无测试记录')

    def upload_records(self):
        if not self.current_records:
            QMessageBox.warning(self, '提示', '没有记录可上传，请先查询')
            return

        # 获取主窗口的质检类型
        check_type = 1  # 默认生产质检
        if self.parent() and hasattr(self.parent(), 'check_type'):
            check_type = self.parent().check_type

        check_type_name = "生产质检" if check_type == 1 else "仓库质检"
        reply = QMessageBox.question(self, '确认',
                                      f'确定上传 {len(self.current_records)} 条记录？\n质检类型: {check_type_name}',
                                      QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            if self.upload_service.upload_records(self.current_records, check_type):
                QMessageBox.information(self, '成功', '上传成功')
            else:
                QMessageBox.critical(self, '失败', '上传失败，请查看日志')

    def export_csv(self):
        """导出当前查询结果为CSV"""
        if not self.current_records:
            QMessageBox.information(self, '提示', '没有记录可导出，请先查询')
            return

        start_date = self.start_date_input.date().toString('yyyy-MM-dd')
        end_date = self.end_date_input.date().toString('yyyy-MM-dd')
        
        filename, _ = QFileDialog.getSaveFileName(
            self, '导出CSV', 
            f'test_records_{start_date}_to_{end_date}.csv',
            'CSV Files (*.csv)'
        )

        if filename:
            try:
                with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    writer.writerow(['SN', '创建时间', '测试时间', '设备类型', '测试结果', '备注'])
                    for record in self.current_records:
                        writer.writerow([
                            record['sn'],
                            record['create_time'],
                            record['update_time'],
                            record['sub_type'],
                            record['results'],
                            record.get('remarks', '')
                        ])
                QMessageBox.information(self, '成功', f'已导出 {len(self.current_records)} 条记录')
            except Exception as e:
                QMessageBox.critical(self, '错误', f'导出失败: {str(e)}')

    def _display_records(self, records):
        self.table.setRowCount(len(records))
        for i, record in enumerate(records):
            self.table.setItem(i, 0, QTableWidgetItem(record['sn']))
            self.table.setItem(i, 1, QTableWidgetItem(record['create_time']))
            self.table.setItem(i, 2, QTableWidgetItem(record['update_time']))
            self.table.setItem(i, 3, QTableWidgetItem(record['sub_type']))
            self.table.setItem(i, 4, QTableWidgetItem(record['results']))
            self.table.setItem(i, 5, QTableWidgetItem(record['remarks']))
