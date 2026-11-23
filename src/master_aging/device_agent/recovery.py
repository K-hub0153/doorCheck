"""在设备独占锁内恢复本工具的中断运行，历史证据和累计预算始终保留。"""
import json
from pathlib import Path
import re

from ..domain.models import utc_now
from . import collectors, runners


RUN_ID = re.compile(r'[0-9a-f]{32}')
TEST_FILE = re.compile(r'\.master-aging-([0-9a-f]{32})\.bin')


def close_reservation(root, identity, run_id, evidence, write_json):
    """将未完成校验的预留归档为未验证写入，不退回 reserved_bytes。"""
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('未决预留所属运行身份无效')
    path = root / 'write-ledger' / (identity + '.json')
    total = json.loads(path.read_text())
    size = total.get('unresolved_bytes', 0)
    if not size:
        previous = total.get('last_resolution', {})
        return previous if previous.get('run_id') == run_id else None
    if total.get('last_run_id') != run_id:
        raise ValueError('未决预留所属运行不匹配')
    if type(size) is not int or size < 0 or size > total['reserved_bytes']:
        raise ValueError('写入账本计数无效')
    resolution = {'run_id': run_id, 'unverified_bytes': size, 'utc': utc_now(),
                  'reason': '进程与临时文件已清理，中断轮次按全额预留计入累计预算',
                  'evidence': evidence}
    # 先持久化原账本与恢复证据；中间断电后可幂等重试。
    directory = root / run_id
    directory.mkdir(mode=0o700, exist_ok=True)
    write_json(directory / ('storage-resolution-' + identity + '.json'),
               {'ledger_before': total, 'resolution': resolution})
    total['unresolved_bytes'] = 0
    total['unverified_bytes'] = total.get('unverified_bytes', 0) + size
    total['last_resolution'] = resolution
    write_json(path, total)
    return resolution


def prepare(root, current_run_id, plan, info, emit, write_json):
    """调用方须持有 device.lock。先回收旧负载，再读结果、清文件和归档预留。"""
    old_runs = {path.name for path in root.iterdir()
                if RUN_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
                and path.name != current_run_id}
    ledgers = {}
    for path in (root / 'write-ledger').glob('*.json'):
        data = json.loads(path.read_text())
        if data.get('unresolved_bytes'):
            run_id = data.get('last_run_id', '')
            if not RUN_ID.fullmatch(run_id) or run_id == current_run_id:
                raise ValueError('无法核验未决账本所属运行: ' + path.name)
            if (root / run_id).is_symlink():
                raise ValueError('旧运行目录不能是符号链接')
            old_runs.add(run_id)
            ledgers[path.stem] = data

    directories = set(plan['items']['storage']['targets']) if plan['items']['storage']['enabled'] else set()
    requests = {}
    for run_id in sorted(old_runs):
        request_path = root / run_id / 'request.json'
        if request_path.exists():
            request = json.loads(request_path.read_text())
            if request.get('run_id') != run_id:
                raise ValueError('旧运行身份不匹配: ' + run_id)
            requests[run_id] = request
            storage = request.get('plan', {}).get('items', {}).get('storage', {})
            if storage.get('enabled'):
                directories.update(storage.get('targets', []))

    targets = {}
    for directory in sorted(directories):
        if not Path(directory).exists():
            continue
        try:
            target = collectors.storage_target(directory, info['disks'])
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            # 未挂载或已改变用途的旧目录不作为可删除目标；对应账本仍不解除。
            if any(TEST_FILE.fullmatch(path.name) for path in Path(directory).glob('.master-aging-*.bin')):
                raise RuntimeError('旧测试目录无法核验，不能自动清理 %s: %s' % (directory, exc)) from exc
            continue
        targets[target['directory']] = target

    files = {}
    for directory, target in targets.items():
        for path in Path(directory).glob('.master-aging-*.bin'):
            match = TEST_FILE.fullmatch(path.name)
            if match and match[1] != current_run_id:
                old_runs.add(match[1])
                files.setdefault(match[1], []).append((path, target['identity']))

    if not old_runs:
        return []
    if any((root / run_id).is_symlink() for run_id in old_runs):
        raise ValueError('旧运行目录不能是符号链接')
    if emit:
        emit('event', {'stage': 'recovery', 'reason': '启动前检查并自动清理历史运行残留'})
    processes = runners.recover_run_processes([root / run_id for run_id in sorted(old_runs)])
    recovered = []
    identities = {value['identity'] for value in targets.values()}
    for run_id in sorted(old_runs):
        pending = {identity: ledger for identity, ledger in ledgers.items()
                   if ledger['last_run_id'] == run_id and identity in identities}
        if not processes.get(run_id) and not files.get(run_id) and not pending:
            continue
        directory = root / run_id
        if directory.is_symlink():
            raise ValueError('旧运行目录不能是符号链接')
        directory.mkdir(mode=0o700, exist_ok=True)
        evidence = {'run_id': run_id, 'utc': utc_now(), 'process_cleanup': processes.get(run_id, []),
                    'fio_results': {}, 'file_cleanup': [], 'reservations': {}}
        storage = requests.get(run_id, {}).get('plan', {}).get('items', {}).get('storage', {})
        size = storage.get('chunk_mib', 0) * 2**20
        for path in sorted(directory.glob('storage_*.json')):
            evidence['fio_results'][path.name] = runners.fio_evidence(path, size)
        for path, identity in files.get(run_id, []):
            # 仅删除受控物理盘目录中、准确匹配该 run_id 的文件，不递归清目录。
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError('自动清理旧测试文件失败 %s: %s' % (path, exc)) from exc
            evidence['file_cleanup'].append({'path': str(path), 'identity': identity, 'removed': True})
        write_json(directory / 'environment-recovery.json', evidence)
        for identity in pending:
            resolution = close_reservation(root, identity, run_id, evidence.copy(), write_json)
            if resolution:
                evidence['reservations'][identity] = resolution['unverified_bytes']
        write_json(directory / 'environment-recovery.json', evidence)
        if evidence['process_cleanup'] or evidence['file_cleanup'] or evidence['reservations']:
            recovered.append(evidence)
            if emit:
                emit('event', {'stage': 'recovery', 'reason': '历史负载、临时文件与未决预留已自动处理',
                               'recovery': evidence})
    return recovered
