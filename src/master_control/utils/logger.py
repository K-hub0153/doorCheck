import logging
import os
import sys
from datetime import datetime


def get_executable_dir():
    """获取可执行文件所在目录"""
    from .paths import app_dir
    return str(app_dir())


class Logger:
    _instance = None
    _logger = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._logger is None:
            self._setup_logger()

    def _setup_logger(self):
        base_dir = get_executable_dir()
        log_dir = os.path.join(base_dir, 'logs')
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, f'mcCheck_{datetime.now().strftime("%Y%m%d")}.log')

        self._logger = logging.getLogger('mcCheck')
        self._logger.setLevel(logging.DEBUG)
        # 避免重复初始化时叠加 handler（每次调用都会重复输出）
        if self._logger.handlers:
            return

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        # 中文 Windows 控制台默认 GBK，日志里出现 ✓ / ✗ / emoji 这类字符时，
        # 控制台 handler 会抛 UnicodeEncodeError，表现为刷一屏 "--- Logging error ---"。
        # 文件 handler 是 utf-8 不受影响，这里让控制台退化为替换字符即可。
        try:
            console_handler.stream.reconfigure(errors='replace')
        except Exception:
            # 打包成窗口程序时 sys.stderr 可能为 None，或 stream 不支持 reconfigure
            pass

        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        self._logger.addHandler(file_handler)
        self._logger.addHandler(console_handler)

    # 注意：必须透传 **kwargs（尤其 exc_info）。
    # 调用点大量使用 logger.error(msg, exc_info=True)，如果签名只收一个 msg，
    # 就会在 except 分支里再抛 TypeError —— 而 QThread.run() 的未捕获异常会让
    # Qt 直接静默 abort 整个进程（无 traceback），极难排查。历史崩溃就是这么来的。
    def _log(self, level: int, msg, *args, **kwargs):
        try:
            self._logger.log(level, msg, *args, **kwargs)
        except Exception:
            # 日志失败绝不能反过来杀掉调用方（哪怕是正在处理异常的 except 分支）
            try:
                sys.stderr.write(f"[Logger fallback] {msg}\n")
            except Exception:
                pass

    def debug(self, msg, *args, **kwargs):
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log(logging.WARNING, msg, *args, **kwargs)

    warn = warning

    def error(self, msg, *args, **kwargs):
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._log(logging.CRITICAL, msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        """等价于 error(msg, exc_info=True)"""
        kwargs.setdefault('exc_info', True)
        self._log(logging.ERROR, msg, *args, **kwargs)


logger = Logger()
