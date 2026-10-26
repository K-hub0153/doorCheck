import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from src.master_control.utils.config import config
from src.master_control.utils.logger import logger


class Database:
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.database_path
        self.conn = None
        self.init_database()
    
    def init_database(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()
        logger.info(f"数据库初始化完成: {self.db_path}")
    
    def create_tables(self):
        cursor = self.conn.cursor()

        # 检查旧表结构并迁移
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='test_records'")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(test_records)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'ip' in columns or 'test_time' in columns:
                logger.info("检测到旧表结构，正在迁移...")
                cursor.execute("DROP TABLE IF EXISTS test_records")
                cursor.execute("DROP INDEX IF EXISTS idx_sn")
                cursor.execute("DROP INDEX IF EXISTS idx_test_time")
                cursor.execute("DROP INDEX IF EXISTS idx_update_time")
                self.conn.commit()
                logger.info("旧表已删除")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS test_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sn TEXT NOT NULL,
                sub_type TEXT NOT NULL,
                results TEXT NOT NULL,
                remarks TEXT,
                create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sn, sub_type)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_sn ON test_records(sn)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_update_time ON test_records(update_time)
        ''')

        self.conn.commit()
    
    def save_test_result(self, sn: str, sub_type: str, results: str, remarks: str = ""):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO test_records (sn, sub_type, results, remarks, update_time)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(sn, sub_type) DO UPDATE SET
                results = excluded.results,
                remarks = excluded.remarks,
                update_time = CURRENT_TIMESTAMP
        ''', (sn, sub_type, results, remarks))
        self.conn.commit()
        logger.info(f"测试记录已保存: SN={sn}, 类型={sub_type}, 结果={results}")

    def get_all_records(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM test_records ORDER BY update_time DESC')
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_records_by_sn(self, sn: str) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM test_records WHERE sn = ? ORDER BY sub_type', (sn,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_records_by_date(self, date_str: str) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM test_records
            WHERE DATE(update_time) = ?
            ORDER BY sn, sub_type
        ''', (date_str,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_records_by_date_range(self, start_date: str, end_date: str) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM test_records
            WHERE DATE(update_time) BETWEEN ? AND ?
            ORDER BY update_time DESC, sn, sub_type
        ''', (start_date, end_date))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_latest_records(self, limit: int = 30) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM test_records ORDER BY update_time DESC LIMIT ?', (limit,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    def close(self):
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")
