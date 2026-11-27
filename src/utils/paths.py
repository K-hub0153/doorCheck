import os
import sys
import shutil
from pathlib import Path


def get_app_dir() -> str:
    """获取应用根目录：打包后返回 exe 所在目录，开发时返回项目根目录"""
    if getattr(sys, 'frozen', False):
        executable_dir = os.path.dirname(sys.executable)
        # macOS 安装到 Applications 后应用目录不可写；首次启动复制默认配置到用户目录。
        contents_dir = os.path.dirname(executable_dir)
        bundle_dir = os.path.dirname(contents_dir)
        if (os.path.basename(executable_dir) == 'MacOS'
                and os.path.basename(contents_dir) == 'Contents'
                and bundle_dir.endswith('.app')):
            data_dir = Path.home() / 'Library' / 'Application Support' / 'Weidian' / 'DeviceFactoryTool'
            for name in ('config', 'certs'):
                source = Path(sys._MEIPASS) / name
                target = data_dir / name
                if source.is_dir():
                    for resource in source.rglob('*'):
                        if resource.is_file():
                            destination = target / resource.relative_to(source)
                            if not destination.exists():
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(resource, destination)
            data_dir.mkdir(parents=True, exist_ok=True)
            return str(data_dir)
        return executable_dir
    return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
