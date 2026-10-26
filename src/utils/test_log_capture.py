import os
import sys
import logging
import threading
from datetime import datetime
from io import StringIO
from ..utils.logger import get_executable_dir


class TestLogCapture:
    """测试日志捕获器，用于捕获单次测试的完整日志"""

    def __init__(self, device_sn: str, test_type: str = "一键检测"):
        self.device_sn = device_sn
        self.test_type = test_type
        self.log_buffer = StringIO()
        self.start_time = datetime.now()
        self.end_time = None
        self.test_result = "UNKNOWN"
        self._lock = threading.Lock()

    def write(self, message: str):
        """写入日志行"""
        with self._lock:
            self.log_buffer.write(message)

    def append(self, message: str):
        """追加日志（自动加换行，线程安全）"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.log_buffer.write(f"[{timestamp}] {message}\n")

    def set_result(self, result: str):
        """设置测试结果 (PASS/FAIL)"""
        self.test_result = result
        self.end_time = datetime.now()

    def get_content(self) -> str:
        """获取完整日志内容"""
        with self._lock:
            return self.log_buffer.getvalue()

    def save_to_file(self) -> str:
        """保存日志到文件，返回文件路径"""
        base_dir = get_executable_dir()
        log_dir = os.path.join(base_dir, 'logs', 'tests')
        os.makedirs(log_dir, exist_ok=True)

        timestamp = self.start_time.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{self.device_sn}_{self.test_type}_{timestamp}.log"
        filepath = os.path.join(log_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            # 写入头部信息
            f.write("=" * 60 + "\n")
            f.write(f"设备SN: {self.device_sn}\n")
            f.write(f"测试项: {self.test_type}\n")
            f.write(f"测试开始时间: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            if self.end_time:
                f.write(f"测试结束时间: {self.end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                duration = (self.end_time - self.start_time).total_seconds()
                f.write(f"测试耗时: {duration:.1f}秒\n")
            f.write(f"测试结果: {self.test_result}\n")
            f.write("=" * 60 + "\n\n")

            # 写入日志内容
            f.write(self.get_content())

        return filepath


class TestLogManager:
    """测试日志管理器，管理所有测试日志记录"""

    @staticmethod
    def get_log_dir():
        """获取测试日志目录"""
        base_dir = get_executable_dir()
        return os.path.join(base_dir, 'logs', 'tests')

    @staticmethod
    def list_test_logs():
        """列出所有测试日志记录"""
        log_dir = TestLogManager.get_log_dir()
        if not os.path.exists(log_dir):
            return []

        logs = []
        for filename in os.listdir(log_dir):
            if filename.endswith('.log'):
                filepath = os.path.join(log_dir, filename)
                try:
                    # 从文件中读取元数据
                    with open(filepath, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        sn = ""
                        test_type = ""
                        start_time = ""
                        result = "UNKNOWN"

                        for line in lines[:10]:  # 只读前10行找元数据
                            if line.startswith("设备SN:"):
                                sn = line.split(":", 1)[1].strip()
                            elif line.startswith("测试项:"):
                                test_type = line.split(":", 1)[1].strip()
                            elif line.startswith("测试开始时间:"):
                                start_time = line.split(":", 1)[1].strip()
                            elif line.startswith("测试结果:"):
                                result = line.split(":", 1)[1].strip()

                        if sn and start_time:
                            logs.append({
                                'filename': filename,
                                'filepath': filepath,
                                'sn': sn,
                                'test_type': test_type or '一键检测',
                                'start_time': start_time,
                                'result': result
                            })
                except Exception as e:
                    print(f"读取日志文件失败 {filename}: {e}")

        # 按时间倒序排列
        logs.sort(key=lambda x: x['start_time'], reverse=True)
        return logs

    @staticmethod
    def read_log_content(filepath: str) -> str:
        """读取日志文件内容"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f"读取日志失败: {e}"


class PanelLogHandler(logging.Handler):
    """临时 logging handler，把本窗口产生的日志行写入对应的 TestLogCapture。

    每次自动测试开始时注册到全局 logger，结束时移除，避免长期持有。
    通过 panel_prefix 过滤（如 "窗口1"），多窗口并行测试互不干扰。
    """

    def __init__(self, capture: TestLogCapture, panel_prefix: str):
        super().__init__()
        self.capture = capture
        self.panel_prefix = panel_prefix  # 例如 "窗口1"

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            # 只捕获包含本窗口前缀的日志，避免混入其他窗口
            if self.panel_prefix in msg:
                # 去掉时间戳前缀，只保留有效内容（已在 append 中加时间戳）
                content = record.getMessage()
                self.capture.write(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"[{record.levelname}] {content}\n"
                )
        except Exception:
            pass
