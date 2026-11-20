from copy import deepcopy
from pathlib import Path
import sys
import yaml


def app_dir():
    if getattr(sys, 'frozen', False):
        from src.utils.paths import get_app_dir
        return Path(get_app_dir()).resolve()
    return Path(__file__).resolve().parents[3]


def load_config():
    path = app_dir() / 'config/master_aging/config.yaml'
    with path.open(encoding='utf-8') as stream:
        result = yaml.safe_load(stream)
    return deepcopy(result)


def runtime_dir():
    path = app_dir() / 'logs/master_aging'
    path.mkdir(parents=True, exist_ok=True)
    return path


def ssh_settings():
    # 只读取既有凭据，不导入/修改主控单例，不把凭据写入运行快照。
    with (app_dir() / 'config/master_control/config.yaml').open(encoding='utf-8') as stream:
        settings = yaml.safe_load(stream).get('ssh', {})
    return {key: settings[key] for key in ('username', 'password', 'timeout') if key in settings}
