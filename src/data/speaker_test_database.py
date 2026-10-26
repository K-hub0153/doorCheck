import sqlite3
import os
import sys
from datetime import datetime
from typing import List, Dict, Optional
from ..utils.logger import logger


def get_data_path():
    """获取数据目录路径，兼容PyInstaller打包"""
    if getattr(sys, 'frozen', False):
        # 打包后：可执行文件所在目录
        base_path = os.path.dirname(sys.executable)
    else:
        # 开发环境：项目根目录
        base_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

    data_dir = os.path.join(base_path, 'data')
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


class TestRecordDB:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_dir = get_data_path()
            db_path = os.path.join(db_dir, 'test_records.db')

        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS test_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sn TEXT NOT NULL,
                create_time TEXT NOT NULL,
                update_time TEXT NOT NULL,
                sub_type TEXT NOT NULL,
                results TEXT NOT NULL,
                remarks TEXT,
                UNIQUE(sn, sub_type)
            )
        ''')
        conn.commit()
        conn.close()
        logger.info(f"测试记录数据库初始化完成: {self.db_path}")

    def save_record(self, sn: str, sub_type: str, results: str, remarks: str = ''):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        cursor.execute('SELECT id FROM test_records WHERE sn = ? AND sub_type = ?', (sn, sub_type))
        existing = cursor.fetchone()

        if existing:
            cursor.execute('''
                UPDATE test_records
                SET update_time = ?, results = ?, remarks = ?
                WHERE sn = ? AND sub_type = ?
            ''', (now, results, remarks, sn, sub_type))
            logger.info(f"更新测试记录: {sn} - {sub_type} - {results}")
        else:
            cursor.execute('''
                INSERT INTO test_records (sn, create_time, update_time, sub_type, results, remarks)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (sn, now, now, sub_type, results, remarks))
            logger.info(f"新增测试记录: {sn} - {sub_type} - {results}")

        conn.commit()
        conn.close()

    def query_by_sn(self, sn: str) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sn, create_time, update_time, sub_type, results, remarks
            FROM test_records WHERE sn LIKE ?
            ORDER BY update_time DESC
        ''', (f'%{sn}%',))

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                'sn': row[0],
                'create_time': row[1],
                'update_time': row[2],
                'sub_type': row[3],
                'results': row[4],
                'remarks': row[5] or ''
            }
            for row in rows
        ]

    def query_recent(self, limit: int = 50) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sn, create_time, update_time, sub_type, results, remarks
            FROM test_records
            ORDER BY update_time DESC
            LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                'sn': row[0],
                'create_time': row[1],
                'update_time': row[2],
                'sub_type': row[3],
                'results': row[4],
                'remarks': row[5] or ''
            }
            for row in rows
        ]

    def query_by_date(self, date: str) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sn, create_time, update_time, sub_type, results, remarks
            FROM test_records WHERE DATE(update_time) = ?
            ORDER BY update_time DESC
        ''', (date,))

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                'sn': row[0],
                'create_time': row[1],
                'update_time': row[2],
                'sub_type': row[3],
                'results': row[4],
                'remarks': row[5] or ''
            }
            for row in rows
        ]
    
    def query_by_date_range(self, start_date: str, end_date: str) -> List[Dict]:
        """按日期范围查询测试记录"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sn, create_time, update_time, sub_type, results, remarks
            FROM test_records 
            WHERE DATE(update_time) BETWEEN ? AND ?
            ORDER BY update_time DESC
        ''', (start_date, end_date))

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                'sn': row[0],
                'create_time': row[1],
                'update_time': row[2],
                'sub_type': row[3],
                'results': row[4],
                'remarks': row[5] or ''
            }
            for row in rows
        ]
