"""
全局串口管理器 - 解决多设备并发红外测试时的串口竞争问题

使用线程锁确保串口在同一时间只被一个测试使用
"""
import threading
from .serial_reader import SerialReader
from .logger import logger


class SerialManager:
    """全局串口管理器（单例模式）"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._serial_reader = None
        self._usage_lock = threading.Lock()  # 串口使用锁
        self._ref_count = 0  # 引用计数
        logger.info("串口管理器已初始化")

    def acquire_serial_reader(self, timeout=30):
        """
        获取串口读取器（阻塞等待直到可用）

        Args:
            timeout: 等待超时时间（秒）

        Returns:
            SerialReader 实例，如果超时则返回 None
        """
        acquired = self._usage_lock.acquire(timeout=timeout)
        if not acquired:
            logger.error(f"获取串口超时（等待了{timeout}秒），其他窗口可能正在使用")
            return None

        try:
            self._ref_count += 1

            # 首次使用或串口已关闭，创建新实例
            if self._serial_reader is None:
                logger.info("创建新的串口读取器实例")
                self._serial_reader = SerialReader()

            return self._serial_reader
        except Exception as e:
            logger.error(f"创建串口读取器失败: {e}")
            self._usage_lock.release()
            return None

    def release_serial_reader(self):
        """
        释放串口读取器（其他等待的测试可以继续）
        """
        try:
            self._ref_count -= 1

            # 如果没有其他引用，关闭串口（可选，也可以保持打开）
            # if self._ref_count == 0 and self._serial_reader:
            #     self._serial_reader.close()
            #     self._serial_reader = None
            #     logger.info("串口读取器已关闭")

        finally:
            self._usage_lock.release()
            logger.debug(f"串口已释放，当前引用计数: {self._ref_count}")

    def close_all(self):
        """关闭所有串口连接"""
        with self._usage_lock:
            if self._serial_reader:
                self._serial_reader.close()
                self._serial_reader = None
                self._ref_count = 0
                logger.info("所有串口连接已关闭")
