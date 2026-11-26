"""随程序内嵌的设备运行环境；仅部署匹配的系统/架构，不依赖设备软件源。"""
import hashlib
import json
from pathlib import Path
import sys


TOOLS = ('python3', 'stress-ng', 'fio', 'findmnt', 'i2cget')


def resource_dir():
    root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[3]
    return root / 'assets' / 'master_aging'


def bundles():
    manifest = json.loads((resource_dir() / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1:
        raise ValueError('不支持的离线依赖清单版本')
    return manifest['bundles']


def select_bundle(info):
    arch = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(info['arch'], info['arch'])
    for bundle in bundles():
        if (bundle['os_id'], bundle['version_id'], bundle['arch']) == (info.get('os_id'), info.get('version_id'), arch):
            actual = tuple(int(part) for part in info.get('glibc', '0').split('.'))
            minimum = tuple(int(part) for part in bundle['glibc_min'].split('.'))
            if actual < minimum:
                raise RuntimeError('设备 glibc %s 低于离线环境要求 %s' % (info.get('glibc'), bundle['glibc_min']))
            return bundle
    raise RuntimeError('程序尚未内置 %s %s / %s 的离线依赖，请补充对应系统运行包后重新打包；设备未联网安装' % (
        info.get('os_id', '未知系统'), info.get('version_id', ''), arch))


def archive_path(bundle):
    path = resource_dir() / bundle['archive']
    if path.parent != resource_dir() or not path.is_file():
        raise RuntimeError('内嵌离线依赖包缺失：' + bundle['archive'])
    if path.stat().st_size != bundle['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != bundle['sha256']:
        raise RuntimeError('内嵌离线依赖包完整性校验失败：' + bundle['archive'])
    return path


def validate_resources():
    """打包前检查，避免构建出缺少设备依赖的 exe。"""
    for bundle in bundles():
        if not set(TOOLS).issubset(bundle['tools']):
            raise RuntimeError('离线依赖包没有覆盖全部老化工具：' + bundle['id'])
        archive_path(bundle)
