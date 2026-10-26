"""进程级错误和未捕获异常记录；文件句柄保持到进程结束。"""
import faulthandler
import os
import sys
import threading
from datetime import datetime

from .logger import get_executable_dir, logger

_crash_file = None


def install_crash_handlers():
    global _crash_file
    if _crash_file is not None:
        return
    try:
        log_dir = os.path.join(get_executable_dir(), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, 'crash.log')
        _crash_file = open(path, 'a', encoding='utf-8', buffering=1)
        _crash_file.write(f'\n=== {datetime.now().isoformat()} pid={os.getpid()} ===\n')
        faulthandler.enable(file=_crash_file, all_threads=True)
        logger.info(f'崩溃日志已启用: {path}')
    except Exception:
        logger.error('启用崩溃日志失败', exc_info=True)

    def report(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical('未捕获异常', exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = report
    threading.excepthook = lambda args: report(args.exc_type, args.exc_value, args.exc_traceback)
