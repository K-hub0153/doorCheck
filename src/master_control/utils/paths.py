"""主控配置独立，字体与日志目录复用工程统一路径。"""
import sys
from pathlib import Path


def project_root():
    return Path(__file__).resolve().parents[3]


def app_dir():
    if getattr(sys, 'frozen', False):
        from src.utils.paths import get_app_dir
        return Path(get_app_dir()).resolve()
    return project_root()


def runtime_dir():
    path = app_dir() / 'logs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir():
    return app_dir() / 'config' / 'master_control'


def resource_path(relative_path):
    """优先使用外置共享资源，其次使用打包内置资源。"""
    relative = Path(relative_path)
    if relative.is_absolute():
        return relative
    external = app_dir() / relative
    if external.exists():
        return external
    bundle = Path(getattr(sys, '_MEIPASS', project_root())) / relative
    return bundle if bundle.exists() else external
