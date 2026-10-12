import os
import sys


def get_app_dir() -> str:
    """获取应用根目录：打包后返回 exe 所在目录，开发时返回项目根目录"""
    if getattr(sys, 'frozen', False):
        executable_dir = os.path.dirname(sys.executable)
        # macOS .app 内的程序与 dist 下的命令行程序共用外置配置。
        contents_dir = os.path.dirname(executable_dir)
        bundle_dir = os.path.dirname(contents_dir)
        if (os.path.basename(executable_dir) == 'MacOS'
                and os.path.basename(contents_dir) == 'Contents'
                and bundle_dir.endswith('.app')):
            return os.path.dirname(bundle_dir)
        return executable_dir
    return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
