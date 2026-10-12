import faulthandler
import os
import sys
import traceback

from src.master_control.utils.logger import logger


def _install_crash_handlers():
    """安装全局崩溃捕获

    PyQt 的 QThread 被销毁、段错误这类问题属于进程级 abort，
    Python 的 try/except 抓不到，必须靠 faulthandler 把原生栈写出来。
    没有这两个钩子时程序只会静默消失，日志停在崩溃前一行。
    """
    from src.master_control.utils.logger import get_executable_dir

    crash_log_dir = os.path.join(get_executable_dir(), 'logs')
    os.makedirs(crash_log_dir, exist_ok=True)
    crash_log_path = os.path.join(crash_log_dir, 'crash.log')

    try:
        crash_file = open(crash_log_path, 'a', encoding='utf-8', buffering=1)
        faulthandler.enable(file=crash_file, all_threads=True)
        logger.info(f"faulthandler 已启用: {crash_log_path}")
    except Exception as e:
        logger.warning(f"faulthandler 启用失败: {e}")

    def excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        detail = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error(f"未捕获异常:\n{detail}")
        with open(crash_log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'=' * 60}\n未捕获异常\n{detail}\n")

    sys.excepthook = excepthook

