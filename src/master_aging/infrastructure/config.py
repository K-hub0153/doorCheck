from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import yaml
from ..domain.policies import validate_plan


def app_dir():
    if getattr(sys, 'frozen', False):
        from src.utils.paths import get_app_dir
        return Path(get_app_dir()).resolve()
    return Path(__file__).resolve().parents[3]


def config_path():
    return app_dir() / 'config/master_aging/config.yaml'


def normalize_config(plan):
    """迁移旧配置；历史运行快照保持原样，用于核验和补采。"""
    result = deepcopy(plan)
    result.pop('max_parallel_devices', None)
    for key in ('usb', 'hdmi'):
        result.get('items', {}).pop(key, None)
    environment = result.get('environment') or {}
    result['environment'] = {'cabinet_id': environment.get('cabinet_id', environment.get('chamber_id'))}
    return result


def load_config():
    with config_path().open(encoding='utf-8') as stream:
        result = yaml.safe_load(stream)
    return normalize_config(result)


def save_config(plan):
    plan = validate_plan(normalize_config(plan))
    path = config_path()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            yaml.safe_dump(plan, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def runtime_dir():
    path = app_dir() / 'logs/master_aging'
    path.mkdir(parents=True, exist_ok=True)
    return path


def ssh_settings():
    # 只读取既有凭据，不导入/修改主控单例，不把凭据写入运行快照。
    with (app_dir() / 'config/master_control/config.yaml').open(encoding='utf-8') as stream:
        settings = yaml.safe_load(stream).get('ssh', {})
    return {key: settings[key] for key in ('username', 'password', 'timeout') if key in settings}
