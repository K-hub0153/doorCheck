from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QLineEdit, QTableWidget, QTableWidgetItem,
                             QHeaderView, QFileDialog, QMessageBox, QDateEdit, QGroupBox)
from PyQt5.QtCore import Qt, QDate
from PyQt5.QtGui import QFont
from src.master_control.core.database import Database
import csv


class TestResultsWindow(QDialog):

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("测试结果查询")
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.resize(1000, 600)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        header = QLabel("测试结果查询")
        header.setFont(QFont("Arial", 14, QFont.Bold))
        header.setStyleSheet("padding: 10px; background-color: #2196F3; color: white;")
        layout.addWidget(header)

        search_group = QGroupBox("查询条件")
        search_group.setStyleSheet("QGroupBox { background-color: white; }")
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("SN:"))
        self.sn_input = QLineEdit()
        self.sn_input.setPlaceholderText("输入SN查询")
        search_layout.addWidget(self.sn_input)

        self.search_btn = QPushButton("查询")
        self.search_btn.clicked.connect(self.search_by_sn)
        search_layout.addWidget(self.search_btn)

        self.refresh_btn = QPushButton("刷新全部")
        self.refresh_btn.clicked.connect(self.load_all_records)
        search_layout.addWidget(self.refresh_btn)

        search_layout.addStretch()
        search_group.setLayout(search_layout)
        layout.addWidget(search_group)

        export_group = QGroupBox("导出设置")
        export_group.setStyleSheet("QGroupBox { background-color: white; }")
        export_layout = QHBoxLayout()
        export_layout.addWidget(QLabel("起始日期:"))
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setDate(QDate.currentDate())
        self.start_date_edit.setCalendarPopup(True)
        export_layout.addWidget(self.start_date_edit)

        export_layout.addWidget(QLabel("结束日期:"))
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setDate(QDate.currentDate())
        self.end_date_edit.setCalendarPopup(True)
        export_layout.addWidget(self.end_date_edit)

        self.export_btn = QPushButton("导出CSV")
        self.export_btn.clicked.connect(self.export_csv)
        export_layout.addWidget(self.export_btn)
        export_layout.addStretch()
        export_group.setLayout(export_layout)
        layout.addWidget(export_group)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["SN", "类型", "结果", "备注", "创建时间", "更新时间", "ID"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        self.setLayout(layout)

    def load_all_records(self):
        records = self.db.get_all_records()
        self.populate_table(records)

    def search_by_sn(self):
        sn = self.sn_input.text().strip()
        if sn:
            records = self.db.get_records_by_sn(sn)
        else:
            records = self.db.get_latest_records(30)
        self.populate_table(records)

    def populate_table(self, records):
        self.table.setRowCount(len(records))
        for i, record in enumerate(records):
            self.table.setItem(i, 0, QTableWidgetItem(record['sn']))
            self.table.setItem(i, 1, QTableWidgetItem(record['sub_type']))
            self.table.setItem(i, 2, QTableWidgetItem(record['results']))
            self.table.setItem(i, 3, QTableWidgetItem(record.get('remarks', '')))
            self.table.setItem(i, 4, QTableWidgetItem(record['create_time']))
            self.table.setItem(i, 5, QTableWidgetItem(record['update_time']))
            self.table.setItem(i, 6, QTableWidgetItem(str(record['id'])))

    def export_csv(self):
        start_date = self.start_date_edit.date().toString("yyyy-MM-dd")
        end_date = self.end_date_edit.date().toString("yyyy-MM-dd")
        records = self.db.get_records_by_date_range(start_date, end_date)

        if not records:
            QMessageBox.information(self, "提示", f"没有 {start_date} 至 {end_date} 的测试记录")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出CSV", f"test_results_{start_date}_to_{end_date}.csv", "CSV Files (*.csv)"
        )

        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8-sig', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(['SN', '类型', '结果', '备注', '创建时间', '更新时间'])
                    for r in records:
                        writer.writerow([r['sn'], r['sub_type'], r['results'],
                                       r.get('remarks', ''), r['create_time'], r['update_time']])
                QMessageBox.information(self, "成功", f"已导出 {len(records)} 条记录")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"导出失败: {e}")
