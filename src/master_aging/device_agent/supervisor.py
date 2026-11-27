"""Linux 设备端执行器：单调计时、本地保护、独占锁、追加日志与可恢复读取。"""
import argparse
import errno
from functools import wraps
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from ..domain.models import conclusion, digest, utc_now
from ..domain.execution_log import format_record
from ..domain.diagnostics import lte_scope, monitor_reason
from ..domain.policies import validate_plan
from . import collectors, lte, recovery, runners

ROOT = Path('/var/tmp/master-aging')


def state_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.state_lock:
            return method(self, *args, **kwargs)
    return locked


def atomic_json(path, value):
    temporary = None
    try:
        # 不同工具可能同时发送幂等停止/确认请求，临时文件须独立，避免互相覆盖。
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_path(run_id):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('非法 run_id')
    return ROOT / run_id


def ddr_allocation(memory, config):
    available = memory['MemAvailable']
    # 除固定保留空间外，为并行项目和设备业务保留至少 30% 当前可用内存。
    reserve = max(config['reserve_mib'] * 2**20, (available * 30 + 99) // 100)
    allocation = int(min(memory['MemTotal'] * config['max_memory_pct'] // 100,
                         available - reserve))
    if allocation < 16 * 2**20:
        raise ValueError('DDR 可分配内存 %.1f MiB，至少需要 16 MiB；系统可用 %.1f MiB，需保留 %.1f MiB' %
                         (allocation / 2**20, available / 2**20, reserve / 2**20))
    return {'bytes': allocation, 'available_bytes': available, 'reserve_bytes': reserve}


def preflight(plan, expected, emit=None, current_run_id=None):
    validate_plan(plan)
    info = collectors.probe()
    info['tool_versions'] = {}
    if not info['sn'] or info['sn'] != expected['sn']:
        raise ValueError('设备 SN 与所选身份不一致')
    if info['chip'] == 'unknown' or info['chip'] != info['sn_chip']:
        raise ValueError('真实硬件与 SN 机型无法核验一致；请确认设备树/固件')
    if expected.get('fingerprint') and info['fingerprint'] != expected['fingerprint']:
        raise ValueError('设备指纹发生变化')
    if current_run_id:
        info['environment_recovery'] = recovery.prepare(ROOT, current_run_id, plan, info, emit, atomic_json)
        if any(value['process_cleanup'] for value in info['environment_recovery']):
            info['memory'] = collectors.memory()
    temperature_limit = plan.get('protection', {}).get('cpu_stop_temperature_c')
    if temperature_limit is not None:
        if not info['temperatures']:
            raise ValueError('未发现可读 thermal zone，无法启用本地温度保护')
        if max(info['temperatures'].values()) >= temperature_limit:
            raise ValueError('当前 CPU 最高温度 %.1f ℃，已达停止门限 %.1f ℃' %
                             (max(info['temperatures'].values()), temperature_limit))
    items = plan['items']
    for key in ('cpu', 'ddr'):
        if items[key]['enabled'] and not info['tools']['stress-ng']:
            raise ValueError('设备缺少 stress-ng，自动部署后仍不可用，请查看环境部署日志')
    if items['cpu']['enabled'] or items['ddr']['enabled']:
        info['tool_versions']['stress-ng'] = collectors.command(['stress-ng', '--version'])
    if items['ddr']['enabled']:
        info['ddr_allocation'] = ddr_allocation(info['memory'], items['ddr'])
        info['ddr_bytes'] = info['ddr_allocation']['bytes']
    if items['storage']['enabled']:
        if not info['tools']['fio'] or not collectors.command(['fio', '--version']).startswith('fio-'):
            raise ValueError('设备缺少真正的 fio 工具')
        info['tool_versions']['fio'] = collectors.command(['fio', '--version'])
        try:
            info['journal_storage'] = collectors.storage_target(str(ROOT), info['disks'], allow_overlay=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError('老化日志目录 %s 无法核验实际落盘位置: %s' % (ROOT, exc)) from exc
        info['storage_targets'] = [collectors.storage_target(d, info['disks']) for d in items['storage']['targets']]
        identities = [v['identity'] for v in info['storage_targets']]
        if len(set(identities)) != len(identities):
            raise ValueError('同一物理盘不能重复分配多个独立预算')
        required = items['storage']['chunk_mib'] * 2**20 + plan['min_free_space_mib'] * 2**20
        for target in info['storage_targets']:
            if target['free_bytes'] < required:
                raise ValueError('存储目录 %s 剩余 %.1f MiB，至少需要 %.1f MiB（单轮写入加保护预留）' %
                                 (target['directory'], target['free_bytes'] / 2**20, required / 2**20))
        cfg = items['storage']
        total_plan = (cfg['rounds'] * cfg['chunk_mib'] + cfg['log_reserve_mib'] + cfg['metadata_reserve_mib']) * 2**20
        for target in info['storage_targets']:
            ledger = ROOT / 'write-ledger' / (target['identity'] + '.json')
            if ledger.exists():
                data = json.loads(ledger.read_text())
                if data.get('unresolved_bytes'):
                    raise ValueError('目标盘历史预留自动恢复未完成，无法确认旧测试目录已清理: ' + str(ledger))
                limit = cfg.get('cumulative_write_budget_gib')
                if limit is not None and data['reserved_bytes'] + total_plan > limit * 2**30:
                    raise ValueError('%s 累计写入预算不足: 已预留 %.1f MiB，本次需要 %.1f MiB，累计上限 %.1f MiB' %
                                     (target['directory'], data['reserved_bytes'] / 2**20,
                                      total_plan / 2**20, limit * 1024))
    if items['battery']['enabled']:
        if not info['tools']['i2cget']:
            raise ValueError('设备缺少 i2cget')
        info['battery_bus'] = 2 if info['chip'] == 'hisi' else 0
        info['battery_initial'] = collectors.battery_sample(info['battery_bus'])
        info['battery_identity'] = collectors.battery_identity(info['battery_bus'])
        if not info['battery_initial']['valid']:
            detail = '；'.join('%s: %s' % pair for pair in info['battery_initial'].get('errors', {}).items())
            raise ValueError('电池必读寄存器读取失败: ' + (detail or '温度/电压/电流/SOC 数据缺失'))
    if items['lte']['enabled']:
        result = lte.sample(items['lte'])
        if emit:
            emit('event', {'item': 'lte', 'stage': 'preflight',
                           'result': 'PASS' if result['ok'] else 'BLOCKED',
                           'reason': result.get('reason') or '4G 预检通过', 'lte': result})
        if not result['ok']:
            raise ValueError('4G 预检失败: ' + (result.get('reason') or '无法获取有效信号'))
        info['lte_initial'] = result
    free = shutil.disk_usage(ROOT).free / 2**20
    if free < plan['min_free_space_mib']:
        raise ValueError('设备日志目录 %s 剩余 %.1f MiB，要求至少 %s MiB' %
                         (ROOT, free, plan['min_free_space_mib']))
    return info


class Supervisor:
    def __init__(self, directory, request):
        self.directory = Path(directory)
        self.request = request
        self.plan = request['plan']
        self.info = {}
        self.processes = {}
        self.storage = {}
        self.storage_lock = threading.RLock()
        self.stop_lock = threading.RLock()
        self.item_locks = {key: threading.RLock() for key in self.plan['items']}
        self.stop_requested = threading.Event()
        self.state_lock = threading.RLock()
        self.guard_stop = threading.Event()
        self.safety_reason = None
        self.last_sample = time.monotonic()
        self.started = None
        self.seq = 0
        self.status = {'run_id': request['run_id'], 'state': 'PREFLIGHT', 'result': None,
                       'boot_id': collectors.read('/proc/sys/kernel/random/boot_id'),
                       'pid': os.getpid(),
                       'created_at': utc_now(), 'items': {}, 'write_ledger': {},
                       'config_hash': digest(self.plan), 'seq': 0, 'latest': {}}
        battery = self.plan['items']['battery']
        self.battery_phases = [phase for phase in ('charge', 'discharge')
                               if battery[phase + '_duration_min'] > 0]
        self.battery = {'index': 0, 'phase': self.battery_phases[0] if self.battery_phases else 'charge',
                        'state': 'WAIT_OPERATOR', 'confirmed': False,
                        'stop_count': 0, 'failures': 0, 'segments': [],
                        'charge_mah': 0.0, 'discharge_mah': 0.0, 'charge_wh': 0.0, 'discharge_wh': 0.0}
        self.previous_battery = None
        self.battery_timing_anchor = None
        self.timed_out = set()

    @state_locked
    def save(self):
        self.status['updated_at'] = utc_now()
        self.status['seq'] = self.seq
        self.status['battery'] = self.battery
        # 先序列化快照，避免持久化期间采集线程更新其他项目的字段。
        snapshot = json.loads(json.dumps(self.status, ensure_ascii=False, allow_nan=False))
        atomic_json(self.directory / 'status.json', snapshot)

    @state_locked
    def emit(self, kind, data):
        self.seq += 1
        record = {'seq': self.seq, 'kind': kind, 'utc': utc_now(), 'monotonic': time.monotonic(),
                  'boot_id': self.status['boot_id'], 'run_id': self.request['run_id'], 'data': data}
        with (self.directory / 'records.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        with (self.directory / 'execution.log').open('a', encoding='utf-8') as stream:
            stream.write(format_record(record))
        return record

    def guard(self):
        while not self.guard_stop.wait(1):
            if self.stop_requested.is_set():
                return
            try:
                temperature_limit = self.plan.get('protection', {}).get('cpu_stop_temperature_c')
                if temperature_limit is not None:
                    temps = collectors.temperatures()
                    if not temps:
                        raise RuntimeError('CPU 温度传感器失效')
                    if max(temps.values()) >= temperature_limit:
                        raise RuntimeError('CPU 超温: 最高 %.1f ℃，停止门限 %.1f ℃' %
                                           (max(temps.values()), temperature_limit))
                gap = time.monotonic() - self.last_sample
                if gap > self.plan['max_sample_gap_s']:
                    raise RuntimeError('采样/调度超时: %.1f 秒未完成采样，最大允许 %.1f 秒' %
                                       (gap, self.plan['max_sample_gap_s']))
                for name in ('cpu', 'ddr'):
                    item = self.status['items'].get(name)
                    process = self.processes.get(name)
                    if item and process and item['result'] is None and name not in self.timed_out:
                        # stress-ng 自身按计划到时退出，留出汇总和回收 worker 的时间。
                        if time.monotonic() - item['started_monotonic'] >= item['planned_s'] + 5:
                            self.timed_out.add(name)
                            process.request_stop()
                for path in [ROOT] + [Path(t['directory']) for t in self.info.get('storage_targets', [])]:
                    free = shutil.disk_usage(path).free / 2**20
                    if free < self.plan['min_free_space_mib']:
                        raise RuntimeError('目录 %s 剩余空间 %.1f MiB，低于保护线 %s MiB' %
                                           (path, free, self.plan['min_free_space_mib']))
                size = sum(p.stat().st_size for p in self.directory.rglob('*') if p.is_file())
                if size > self.plan['max_log_mib'] * 2**20:
                    raise RuntimeError('设备日志 %.1f MiB，超过上限 %s MiB' %
                                       (size / 2**20, self.plan['max_log_mib']))
                continue
            except Exception as exc:
                self.safety_reason = str(exc)
                # 不依赖 PC 或主采样循环：只停止本 supervisor 创建的进程组。
                for process in list(self.processes.values()):
                    try:
                        os.killpg(process.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                return

    def stop_process(self, name):
        process = self.processes[name]
        evidence = {'pgid': process.process.pid, 'confirmed': False}
        self.status.setdefault('process_cleanup', {})[name] = evidence
        try:
            code = process.stop()
        except Exception as exc:
            evidence['error'] = str(exc)
            raise
        evidence.update(confirmed=True, exit_code=code, checked_at=utc_now())
        # 停止未确认时保留引用，最终清理仍可重试，不能遗漏该进程组。
        self.processes.pop(name)
        return code

    def storage_stop_evidence(self):
        size = self.plan['items']['storage']['chunk_mib'] * 2**20
        errors = []
        for task in self.storage.values():
            if task['pending']:
                task['last_fio'] = runners.fio_evidence(task['result_path'], size)
                if task['last_fio'].get('fio_error') in (errno.EIO, errno.EILSEQ):
                    error = task['last_fio']['fio_error']
                    errors.append('%s 第 %s 轮 fio 错误: %s（errno=%s）' % (
                        task['target']['directory'], task['completed'] + 1, os.strerror(error), error))
        self.cleanup_storage_files()
        self.close_storage_reservations()
        self.status['items']['storage']['storage_progress'] = self.storage_progress()
        return errors

    def close_storage_reservations(self):
        for name, task in self.storage.items():
            ledger = self.status['write_ledger'][name]
            if not ledger.get('unresolved_bytes') or name in self.processes:
                continue
            if task.get('file') and not task.get('file_cleanup', {}).get('removed'):
                continue
            try:
                resolution = recovery.close_reservation(ROOT, ledger['identity'], self.request['run_id'],
                    {'fio': task.get('last_fio', {}), 'cleanup': self.status.get('process_cleanup', {}).get(name),
                     'file_cleanup': task.get('file_cleanup'), 'completed_rounds': task['completed']}, atomic_json)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                task['reservation_cleanup_error'] = str(exc)
                continue
            if resolution:
                ledger['unverified_bytes'] = ledger.get('unverified_bytes', 0) + ledger['unresolved_bytes']
                ledger['unresolved_bytes'] = 0
                ledger['resolution'] = resolution
                task.pop('reservation_cleanup_error', None)

    def cleanup_storage_files(self):
        """仅在进程组已确认停止、证据已读取后删除本次运行的测试文件。"""
        for name, task in self.storage.items():
            if not task.get('file'):
                continue
            cleanup = self.status.get('process_cleanup', {}).get(name, {})
            path = task.get('file')
            evidence = task.setdefault('file_cleanup', {})
            if not cleanup.get('confirmed'):
                evidence.update(removed=False, reason='进程回收尚未确认，保留测试文件')
                continue
            expected = Path(task['target']['directory']) / ('.master-aging-' + self.request['run_id'] + '.bin')
            if path != expected:
                evidence.update(removed=False, reason='文件路径与本次运行不匹配，保留文件')
                continue
            try:
                path.unlink(missing_ok=True)
                evidence.clear()
                evidence.update(removed=True, path=str(path), checked_at=utc_now())
            except OSError as exc:
                evidence.update(removed=False, path=str(path), error=str(exc))

    def finish_item(self, key, result, reason):
        if key == 'storage':
            with self.storage_lock:
                return self._finish_item(key, result, reason)
        if key == 'battery':
            with self.state_lock:
                return self._finish_item(key, result, reason)
        with self.item_locks[key]:
            return self._finish_item(key, result, reason)

    def _finish_item(self, key, result, reason):
        item = self.status['items'][key]
        if item.get('result') is not None:
            return
        names = [key] if key in self.processes else []
        if key == 'storage':
            names += [name for name in list(self.processes) if name.startswith('storage_')]
        for name in names:
            try:
                code = self.stop_process(name)
                if name == key:
                    item['exit_code'] = code
                if result == 'PASS' and code != 0:
                    result, reason = 'INCOMPLETE', '压测退出码不是 0: %s' % code
                    detail = runners.process_diagnostics(self.directory, name)
                    if detail:
                        reason += '；' + detail
            except Exception as exc:
                if result == 'PASS':
                    result = 'INCOMPLETE'
                reason += '；压测进程停止未确认: ' + str(exc)
        if key == 'storage':
            size = self.plan['items']['storage']['chunk_mib'] * 2**20
            errors = self.storage_stop_evidence()
            if errors:
                result = 'FAIL'
                reason += '；' + '；'.join(errors)
            details = []
            for value in item['storage_progress'].values():
                detail = '%s 已完成 %s/%s 轮' % (value['directory'], value['completed_rounds'], value['planned_rounds'])
                if value['pending']:
                    evidence = value['last_fio']
                    if 'written_bytes' in evidence:
                        detail += '，中断轮写入 %.1f MiB、读取校验 %.1f MiB（本轮目标 %.1f MiB）' % (
                            evidence['written_bytes'] / 2**20, evidence['verified_bytes'] / 2**20, size / 2**20)
                    else:
                        detail += '，中断轮未取得可解析的 fio 结果'
                    if evidence.get('fio_error'):
                        error = evidence['fio_error']
                        detail += '，fio 错误 %s（errno=%s）' % (os.strerror(error), error)
                    if evidence.get('evidence_error'):
                        detail += '，结果读取异常: ' + evidence['evidence_error']
                    cleanup = value.get('cleanup', {})
                    detail += '，进程回收%s，退出码 %s' % ('已确认' if cleanup.get('confirmed') else '未确认', cleanup.get('exit_code'))
                    file_cleanup = value.get('file_cleanup', {})
                    detail += '，临时文件%s' % ('已清理' if file_cleanup.get('removed') else '未清理')
                details.append(detail)
                if value.get('reservation_cleanup_error'):
                    details.append('未决预留自动归档失败: ' + value['reservation_cleanup_error'])
            reason += '；' + '；'.join(details)
        item.update(result=result, stop_reason=reason, finished_at=utc_now())
        if result in ('FAIL', 'INCOMPLETE') and item.get('last_sample'):
            item['failure_sample'] = item['last_sample']
        event = {'item': key, 'result': result, 'reason': reason,
                 'exit_code': item.get('exit_code'), 'effective_s': item.get('effective_s'),
                 'planned_s': item.get('planned_s')}
        if item.get('last_sample'):
            event[key] = item['last_sample']
        self.emit('event', event)

    def start_items(self):
        self.started = time.monotonic()
        for key, config in self.plan['items'].items():
            if not config.get('enabled'):
                continue
            item = {'result': None, 'effective_s': 0, 'planned_s': config['duration_min'] * 60,
                    'started_at': utc_now(), 'started_monotonic': self.started}
            self.status['items'][key] = item
            if (self.directory / 'stop.json').exists():
                self.finish_item(key, 'ABORTED', '操作者停止，未启动负载')
                continue
            if key in ('cpu', 'ddr'):
                args = ['stress-ng', '--verify', '--metrics-brief']
                if key == 'cpu':
                    args += ['--cpu', str(self.info['cpu_count']), '--cpu-method', 'all']
                else:
                    # 预检后可能已启动其他负载，按实际启动时的可用内存重新限额。
                    memory = collectors.memory()
                    budget = ddr_allocation(memory, config)
                    self.info['ddr_bytes'] = min(self.info['ddr_bytes'], budget['bytes'])
                    budget['bytes'] = self.info['ddr_bytes']
                    self.info['ddr_allocation'] = item['allocation'] = budget
                    args += ['--vm', '1', '--vm-bytes', str(self.info['ddr_bytes']), '--vm-method', 'all',
                             '--vm-keep', '--vm-locked', '--vm-populate', '--vm-madvise', 'normal']
                    item['scope'] = 'DDR 正确性压力；带宽基线需另行使用已验证的 STREAM 工具测量'
                    # 在启动负载前记录基线，不能把系统原有 swap 占用当作本轮换出。
                    item['swap_baseline'] = self.swap_snapshot(memory, collectors.read('/proc/vmstat'))
                args += ['--timeout', str(config['duration_min'] * 60) + 's']
                self.processes[key] = runners.Process(args, self.directory, key)
                item['started_monotonic'] = time.monotonic()
                item['pid'] = self.processes[key].process.pid
                item['argv'] = args
            elif key == 'storage':
                item['scope'] = '按设定容量逐轮写入，直接读取并校验数据；完成后持续监测至计划结束'
                for idx, target in enumerate(self.info['storage_targets']):
                    name = 'storage_' + str(idx)
                    self.storage[name] = dict(target=target, completed=0, pending=False, due=self.started)
                    self.status['write_ledger'][name] = {'identity': target['identity'], 'target': target,
                                                       'reserved_bytes': 0, 'settled_bytes': 0, 'unresolved_bytes': 0}
                    overhead = (config['log_reserve_mib'] + config['metadata_reserve_mib']) * 2**20
                    if not self.reserve(name, overhead, overhead=True):
                        raise ValueError('无法预留存储日志和元数据预算')
            elif key == 'battery':
                item['planned_s'] = (config['charge_duration_min'] + config['discharge_duration_min']) * 60 * config['cycles']
                operation = '接入适配器' if self.battery['phase'] == 'charge' else '拔除适配器，确认放电负载'
                self.emit('event', {'item': 'battery', 'phase': self.battery['phase'],
                                    'reason': '请为本批全部设备' + operation + '，整批确认一次', 'token': 0})
            elif key == 'lte':
                item['scope'] = lte_scope(self.plan['items']['lte'])
            if item.get('result') is None:
                self.emit('event', {'item': key, 'state': 'WAIT_OPERATOR' if key == 'battery' else 'RUNNING',
                                    'reason': '项目已启动，等待首次电源确认' if key == 'battery' else '项目已启动',
                                    'planned_s': item['planned_s'], 'argv': item.get('argv'),
                                    'allocation': item.get('allocation')})
        self.status['state'] = 'WAIT_OPERATOR' if 'battery' in self.status['items'] else 'RUNNING'
        self.emit('event', {'state': self.status['state'], 'hardware': self.info})

    def reserve(self, name, size, overhead=False):
        ledger = self.status['write_ledger'][name]
        config = self.plan['items']['storage']
        if ledger['reserved_bytes'] + size > config['write_budget_gib'] * 2**30:
            return False
        global_path = ROOT / 'write-ledger' / (ledger['identity'] + '.json')
        global_path.parent.mkdir(parents=True, exist_ok=True)
        total = json.loads(global_path.read_text()) if global_path.exists() else {'reserved_bytes': 0, 'unresolved_bytes': 0}
        ledger['cumulative_reserved_bytes'] = total['reserved_bytes']
        cumulative_limit = config.get('cumulative_write_budget_gib')
        if cumulative_limit is not None and total['reserved_bytes'] + size > cumulative_limit * 2**30:
            return False
        total['reserved_bytes'] += size
        total['unresolved_bytes'] += 0 if overhead else size
        total['last_run_id'] = self.request['run_id']
        atomic_json(global_path, total)
        ledger['cumulative_reserved_bytes'] = total['reserved_bytes']
        ledger['reserved_bytes'] += size
        ledger['unresolved_bytes'] += 0 if overhead else size
        ledger['overhead_reserved_bytes'] = ledger.get('overhead_reserved_bytes', 0) + (size if overhead else 0)
        self.save()
        return True

    def storage_progress(self):
        return {name: {'directory': task['target']['directory'], 'completed_rounds': task['completed'],
                       'planned_rounds': self.plan['items']['storage']['rounds'], 'pending': task['pending'],
                       'last_fio': task.get('last_fio', {}),
                       'result_file': task['result_path'].name if task.get('result_path') else None,
                       'file_cleanup': task.get('file_cleanup', {}),
                       'reservation_cleanup_error': task.get('reservation_cleanup_error'),
                       'cleanup': self.status.get('process_cleanup', {}).get(name, {})}
                for name, task in self.storage.items()}

    def storage_tick(self, now):
        with self.storage_lock:
            item = self.status['items'].get('storage')
            if not item or item['result'] is not None:
                return
            previous = item.get('storage_progress')
            try:
                self._storage_tick(now)
            finally:
                item['storage_progress'] = self.storage_progress()
                if item['storage_progress'] != previous or item['result'] is not None:
                    self.save()

    def storage_loop(self):
        while not self.guard_stop.is_set():
            try:
                self.storage_tick(time.monotonic())
            except Exception as exc:
                self.finish_item('storage', 'INCOMPLETE', '存储调度异常: ' + str(exc))
                self.save()
                return
            if self.status['items']['storage']['result'] is not None or self.guard_stop.wait(0.2):
                return

    def _storage_tick(self, now):
        item = self.status['items'].get('storage')
        if not item or item['result'] is not None:
            return
        config = self.plan['items']['storage']
        size = config['chunk_mib'] * 2**20
        for name, task in self.storage.items():
            ledger = self.status['write_ledger'][name]
            if task['pending']:
                process = self.processes[name]
                if process.poll() is not None:
                    code = self.stop_process(name)
                    task['last_fio'] = runners.fio_evidence(task['result_path'], size)
                    if code:
                        detail = runners.process_diagnostics(self.directory, name)
                        self.finish_item('storage', 'FAIL', '%s 第 %s 轮 fio 退出码 %s%s' % (
                            task['target']['directory'], task['completed'] + 1, code,
                            '；' + detail if detail else ''))
                        return
                    try:
                        result = runners.fio_result(task['result_path'], size)
                    except Exception as exc:
                        self.finish_item('storage', 'FAIL', '%s 第 %s 轮 fio 校验结果异常: %s' % (
                            task['target']['directory'], task['completed'] + 1, exc))
                        return
                    ledger['settled_bytes'] += result['written_bytes']
                    ledger['unresolved_bytes'] -= size
                    global_path = ROOT / 'write-ledger' / (ledger['identity'] + '.json')
                    total = json.loads(global_path.read_text())
                    total['unresolved_bytes'] -= size
                    atomic_json(global_path, total)
                    task['completed'] += 1
                    task['pending'] = False
                    self.emit('storage', dict(target=name, round=task['completed'], **result))
                    # 只删除本 run_id 创建的文件。
                    if task['file'].exists():
                        task['file'].unlink()
            if task['pending'] or task['completed'] >= config['rounds']:
                continue
            # 到时先结算已退出的 fio，再停止未完成轮次；禁止到期后启动新轮。
            now = time.monotonic()
            if now - item['started_monotonic'] >= item['planned_s']:
                continue
            if self.guard_stop.is_set() or self.safety_reason or (self.directory / 'stop.json').exists():
                continue
            due = item['started_monotonic'] + task['completed'] * item['planned_s'] / config['rounds']
            if now < due:
                continue
            if not self.reserve(name, size):
                self.finish_item('storage', 'INCOMPLETE', '%s 第 %s 轮写入预算不足: 本轮需 %s MiB，'
                                     '本次已预留 %.1f MiB，上限 %.1f MiB；累计已预留 %s MiB，累计上限 %s GiB' % (
                                     task['target']['directory'], task['completed'] + 1, config['chunk_mib'],
                                     ledger['reserved_bytes'] / 2**20, config['write_budget_gib'] * 1024,
                                     ('%.1f' % (ledger['cumulative_reserved_bytes'] / 2**20)
                                      if 'cumulative_reserved_bytes' in ledger else '未读取'),
                                     config.get('cumulative_write_budget_gib', '未设置')))
                return
            # 每轮预留必须先落盘。重连仅补采，禁止启动同 run_id 或重发写入批次。
            self.emit('write_reservation', dict(target=name, bytes=size, round=task['completed'] + 1))
            # 账本与事件落盘也可能耗时，启动前再次检查，不能越过截止时间补跑。
            if time.monotonic() - item['started_monotonic'] >= item['planned_s']:
                item['effective_s'] = item['planned_s']
                self.finish_item('storage', 'INCOMPLETE', '写入预留落盘后已到时，未启动新轮；保留未决预留量')
                return
            if self.guard_stop.is_set() or self.safety_reason or (self.directory / 'stop.json').exists():
                return
            path = Path(task['target']['directory']) / ('.master-aging-' + self.request['run_id'] + '.bin')
            if path.exists():
                raise RuntimeError('发现未决存储文件，禁止覆盖')
            task['file'] = path
            task['pending'] = True
            task['last_fio'] = {}
            task.pop('file_cleanup', None)
            task['result_path'] = self.directory / ('%s.round-%s.json' % (name, task['completed'] + 1))
            self.status.get('process_cleanup', {}).pop(name, None)
            self.processes[name] = runners.Process(
                runners.fio_args(path, size, task['result_path']), self.directory, name)
        elapsed = time.monotonic() - item['started_monotonic']
        item['effective_s'] = min(elapsed, item['planned_s'])
        if elapsed >= item['planned_s']:
            unfinished = any(t['pending'] or t['completed'] < config['rounds'] for t in self.storage.values())
            self.finish_item('storage', 'INCOMPLETE' if unfinished else 'PASS',
                             '到时仍有未完成的写入校验轮次' if unfinished else '有效时长达标，所有写入与读取校验轮次完成')

    @state_locked
    def start_battery_phase(self, sample, now):
        self.battery.update(state='RUNNING', started=now, phase_effective_s=0, stop_count=0)
        self.previous_battery = (now, sample['current_ma'], sample['voltage_mv']) if sample else None
        self.battery_timing_anchor = now if sample is None else None
        operation = '接入适配器' if self.battery['phase'] == 'charge' else '拔除适配器并接好放电负载'
        self.emit('event', {'item': 'battery', 'phase': self.battery['phase'], 'state': 'RUNNING',
                            'reason': '按配置时长开始计时，请' + operation + '；无需等待电流达标或再次确认'})

    @state_locked
    def accept_power_confirmation(self):
        item = self.status['items'].get('battery')
        if (not item or item['result'] is not None or self.battery['state'] != 'WAIT_OPERATOR'
                or self.safety_reason or self.guard_stop.is_set()
                or (self.directory / 'stop.json').exists()):
            return
        confirmation = self.directory / 'confirm.json'
        if not confirmation.exists():
            return
        value = json.loads(confirmation.read_text())
        confirmation.unlink()
        if value.get('token') != self.battery['index'] or value.get('phase') != self.battery['phase']:
            return
        self.battery['confirmed'] = True
        self.emit('operator', dict(value, reason='本次测试电源已确认，直接按配置时长计时'))
        self.start_battery_phase(None, time.monotonic())
        self.status['state'] = 'RUNNING'
        self.save()

    def accept_operator_stop(self):
        with self.stop_lock:
            if self.stop_requested.is_set():
                return True
            if not (self.directory / 'stop.json').exists():
                return False
            self.stop_requested.set()
            with self.state_lock:
                self.status['state'] = 'STOPPING'
                self.save()
            # 先通知全部本次负载停止，再逐组等待回收，避免串行等待仍在压测。
            for process in list(self.processes.values()):
                try:
                    process.request_stop()
                except Exception as exc:
                    self.emit('event', {'reason': '负载停止通知异常，继续执行进程组回收', 'error': str(exc)})
            for key in self.status['items']:
                self.finish_item(key, 'ABORTED', '操作者停止')
            self.save()
            return True

    def control_loop(self):
        while not self.guard_stop.is_set():
            try:
                if self.accept_operator_stop():
                    return
                if self.plan['items']['battery']['enabled']:
                    self.accept_power_confirmation()
            except Exception as exc:
                self.safety_reason = '控制命令处理失败: ' + str(exc)
                return
            if self.guard_stop.wait(0.1):
                return

    @state_locked
    def battery_tick(self, sample, now):
        item = self.status['items']['battery']
        b, cfg, protection = self.battery, self.plan['items']['battery'], self.plan.get('protection', {})
        item['last_sample'] = sample
        if not sample['valid']:
            b['failures'] += 1
            self.previous_battery = None
            self.battery_timing_anchor = None
            b['stop_count'] = 0
            if b['failures'] >= cfg['max_read_failures']:
                self.safety_reason = '电池连续读取失败 %s 次（上限 %s 次）: %s' % (
                    b['failures'], cfg['max_read_failures'],
                    '；'.join('%s: %s' % pair for pair in sample.get('errors', {}).items()) or '必读寄存器数据缺失')
            return
        b['failures'] = 0
        current, voltage, temperature = sample['current_ma'], sample['voltage_mv'], sample['temperature_c']
        limits = protection.get('battery_%s_temperature_range_c' % b['phase'])
        minimum = protection.get('battery_min_voltage_mv')
        maximum = protection.get('battery_max_voltage_mv')
        max_current = protection.get('battery_max_abs_current_ma')
        min_soc = protection.get('discharge_min_soc_pct')
        violations = []
        if limits is not None and not limits[0] <= temperature <= limits[1]:
            violations.append('温度 %.1f ℃，允许 %.1f～%.1f ℃' % (temperature, *limits))
        if minimum is not None and voltage < minimum:
            violations.append('电压 %s mV，低于下限 %s mV' % (voltage, minimum))
        if maximum is not None and voltage > maximum:
            violations.append('电压 %s mV，高于上限 %s mV' % (voltage, maximum))
        if max_current is not None and abs(current) > max_current:
            violations.append('电流 %s mA，绝对值超过上限 %s mA' % (current, max_current))
        if min_soc is not None and b['phase'] == 'discharge' and sample['soc_pct'] <= min_soc:
            violations.append('SOC %s%%，达到放电停止线 %s%%' % (sample['soc_pct'], min_soc))
        phase_label = '充电' if b['phase'] == 'charge' else '放电'
        if violations:
            item['failure_sample'] = sample
            self.safety_reason = '电池%s保护: %s；请接回适配器' % (phase_label, '；'.join(violations))
            return
        if item['result'] is not None:
            return
        if b['state'] == 'WAIT_OPERATOR':
            self.previous_battery = None
            return
        sign = 1 if b['phase'] == 'charge' else -1
        directed = current * sign
        if directed < -cfg['stop_current_ma']:
            self.finish_item('battery', 'INCOMPLETE', '电池%s电流方向异常: 实测 %s mA，允许反向幅度 ≤ %s mA；'
                             '正值表示充电，负值表示放电，请核验电源/负载条件' %
                             (phase_label, current, cfg['stop_current_ma']))
            return
        duration = cfg[b['phase'] + '_duration_min'] * 60
        # 仅配置单一方向时将所有循环合并，持续充/放电，不要求中途重复切电确认。
        if len(self.battery_phases) == 1:
            duration *= cfg['cycles']
        elapsed = b.get('phase_effective_s', 0)
        if self.battery_timing_anchor is not None:
            gap = now - self.battery_timing_anchor
            if 0 <= gap <= self.plan['max_sample_gap_s']:
                elapsed += gap
            # 首次确认后不使用确认前的电流补造积分；失败或超长缺口不计有效时长。
            self.battery_timing_anchor = None
        if self.previous_battery:
            previous_time, previous_current, previous_voltage = self.previous_battery
            gap = now - previous_time
            if gap <= self.plan['max_sample_gap_s']:
                elapsed += gap
                b[b['phase'] + '_mah'] += max(0, (current + previous_current) * sign / 2) * gap / 3600
                b[b['phase'] + '_wh'] += max(0, (current * voltage + previous_current * previous_voltage) * sign / 2) * gap / 3600000000
        self.previous_battery = (now, current, voltage)
        b['phase_effective_s'] = elapsed
        item['effective_s'] = sum(s['effective_s'] for s in b['segments']) + min(elapsed, duration)
        b['stop_count'] = b['stop_count'] + 1 if abs(current) <= cfg['stop_current_ma'] else 0
        natural = b['stop_count'] >= cfg['stop_confirm_samples'] and (
            (b['phase'] == 'charge' and sample['soc_pct'] >= 99) or
            (b['phase'] == 'discharge' and (min_soc is None or sample['soc_pct'] <= min_soc + 2)))
        stop_node = natural and cfg['completion_mode'] == 'stop_node'
        # 固定时长模式下，满电/低电流仍继续监测；只有停止节点模式允许提前完成。
        if elapsed < duration and not stop_node:
            return
        if elapsed >= duration and cfg['completion_mode'] == 'stop_node' and not natural:
            self.finish_item('battery', 'INCOMPLETE', '电池%s已监测 %.1f/%s 秒，未确认停止节点: '
                             '电流 %s mA、SOC %s%%；要求绝对电流 ≤ %s mA 连续 %s 次（当前 %s 次），%s' % (
                                 phase_label, elapsed, duration, current, sample['soc_pct'], cfg['stop_current_ma'],
                                 cfg['stop_confirm_samples'], b['stop_count'],
                                 '充电 SOC ≥ 99%' if b['phase'] == 'charge' else (
                                     '放电 SOC ≤ %g%%' % (min_soc + 2) if min_soc is not None else '未设置放电 SOC 条件')))
            return
        b['segments'].append({'phase': b['phase'], 'index': b['index'], 'effective_s': min(elapsed, duration),
                              'end_soc': sample['soc_pct'], 'stop_reason': '停止节点' if stop_node else '到时'})
        b['index'] += 1
        phase_count = cfg['cycles'] * len(self.battery_phases) if len(self.battery_phases) > 1 else 1
        if b['index'] >= phase_count:
            self.finish_item('battery', 'PASS', '所有已配置的电池阶段完成')
            return
        b['phase'] = self.battery_phases[b['index'] % len(self.battery_phases)]
        self.start_battery_phase(sample, now)

    @staticmethod
    def swap_snapshot(memory, vmstat):
        counters = dict(line.split() for line in vmstat.splitlines() if len(line.split()) == 2)
        return {'used_bytes': max(0, memory.get('SwapTotal', 0) - memory.get('SwapFree', 0)),
                'pswpout': int(counters['pswpout']) if 'pswpout' in counters else None}

    def sample(self):
        now = time.monotonic()
        data = self.collector.sample()
        if self.stop_requested.is_set():
            return
        if data.get('kernel_errors'):
            self.safety_reason = '检测到新的内核错误: ' + '；'.join(data['kernel_errors'])
        if data.get('kernel_log_gap'):
            raise RuntimeError('内核环形日志缺段，证据不足')
        if self.plan['items']['battery']['enabled']:
            data['battery'] = collectors.battery_sample(self.info['battery_bus'])
            if self.stop_requested.is_set():
                return
            self.battery_tick(data['battery'], time.monotonic())
        if self.safety_reason:
            self.emit('metric', data)
            self.status['latest'] = data
            return
        for key, item in self.status['items'].items():
            if self.stop_requested.is_set():
                return
            if item['result'] is not None:
                continue
            cfg = self.plan['items'][key]
            elapsed = now - item['started_monotonic']
            if key not in ('battery', 'storage'):
                item['effective_s'] = min(elapsed, item['planned_s'])
            if key in ('cpu', 'ddr'):
                if key == 'cpu':
                    item['last_sample'] = data.get('cpu', {})
                process = self.processes.get(key)
                if process is None:
                    continue
                code = process.poll()
                swapping = False
                if key == 'ddr':
                    latest_swap = self.swap_snapshot(data['memory'], data.get('vmstat', ''))
                    item['swap_latest'] = latest_swap
                    try:
                        group = process.memory_snapshot(self.info['ddr_bytes'])
                    except (OSError, ValueError) as exc:
                        self.finish_item(key, 'INCOMPLETE', 'DDR 进程内存采样失败: ' + str(exc))
                        continue
                    # wrapper 和 stress-ng 主进程不执行 VM 校验，其闲置页可正常被换出。
                    # VM worker 是组内孙进程；名称同时覆盖退出时重挂到 wrapper 的 worker。
                    workers = [p for p in group['processes'] if p['pid'] != item['pid'] and
                               (p['ppid'] != item['pid'] or p['name'].startswith('stress-ng-vm'))]
                    group['worker_swap_bytes'] = sum(p['swap_bytes'] for p in workers)
                    item['memory_latest'] = data['ddr_process_memory'] = group
                    item['last_sample'] = group
                    swapping = group['vm_swap_bytes'] > 0
                    # 进程总 VmSwap 仅作诊断；只根据已定位的匿名测试区判断换出。
                    item['memory_policy'] = 'locked_vm_region'
                    startup_grace = max(15, self.plan['sample_interval_s'] * 2)
                    page_size = os.sysconf('SC_PAGE_SIZE')
                    expected = self.info['ddr_bytes'] // page_size * page_size
                    if (group['vm_regions'] and group['vm_locked_bytes'] >= expected
                            and group['vm_rss_bytes'] >= expected and not swapping):
                        item['memory_verified_samples'] = item.get('memory_verified_samples', 0) + 1
                        item['memory_last_verified'] = group
                    if not swapping and code is None and elapsed >= startup_grace:
                        if (not group['vm_regions'] or group['vm_locked_bytes'] < expected
                                or group['vm_rss_bytes'] < expected):
                            self.finish_item(key, 'INCOMPLETE',
                                'DDR 测试内存锁定未确认：已锁定 %.1f MiB，要求 %.1f MiB；'
                                '请检查锁页权限或可锁定内存限额' % (group['vm_locked_bytes'] / 2**20, expected / 2**20))
                            continue
                if swapping:
                    self.finish_item(key, 'INCOMPLETE', 'DDR 测试内存区换出 %.1f KiB（要求 0 KiB），'
                                     '内存负载 %.1f MiB；需降低内存负载后重试' % (
                                         group['vm_swap_bytes'] / 1024, self.info['ddr_bytes'] / 2**20))
                elif elapsed >= item['planned_s']:
                    # 正常到时先等 stress-ng 汇总；非零退出和清理失败仍不能通过。
                    if code is not None or key in self.timed_out or elapsed >= item['planned_s'] + 5:
                        if key == 'ddr' and not item.get('memory_verified_samples'):
                            self.finish_item(key, 'INCOMPLETE', 'DDR 未取得测试内存驻留与锁定证据，无法判为通过')
                        else:
                            self.finish_item(key, 'PASS', '计划时长已到，已核验压测退出和进程清理')
                    continue
                elif code is not None:
                    detail = runners.process_diagnostics(self.directory, key)
                    self.finish_item(key, 'FAIL' if code else 'INCOMPLETE', '%s 压测进程提前退出: 退出码 %s，'
                                     '已运行 %.1f/%s 秒%s' % (key.upper(), code, elapsed, item['planned_s'],
                                                                          '；' + detail if detail else ''))
                elif key == 'cpu' and elapsed > cfg['grace_s'] and data['cpu']['busy_pct'] is not None and data['cpu']['busy_pct'] < cfg['target_busy_pct']:
                    self.finish_item(key, 'FAIL', 'CPU 有效计算忙碌率 %.1f%%，要求 ≥ %.1f%%；已过启动宽限 %s 秒' % (
                        data['cpu']['busy_pct'], cfg['target_busy_pct'], cfg['grace_s']))
            elif key == 'battery':
                pass  # 电池采样与保护在上方执行，包括项目完成后的剩余综合负载阶段。
            elif key == 'lte':
                try:
                    try:
                        data[key] = lte.sample(cfg)
                    finally:
                        # AT 读取可能耗时数秒，按实际返回时间记录监测时长。
                        elapsed = time.monotonic() - item['started_monotonic']
                        item['effective_s'] = min(elapsed, item['planned_s'])
                    item['last_sample'] = data[key]
                    if not data[key]['ok']:
                        reason = monitor_reason(key, data[key])
                        self.finish_item(key, 'FAIL', reason)
                except Exception as exc:
                    data[key] = {'ok': False, 'error': str(exc)}
                    item['last_sample'] = data[key]
                    self.finish_item(key, 'INCOMPLETE', '采样失败: ' + str(exc))
            if self.safety_reason:
                self.emit('metric', data)
                self.status['latest'] = data
                return
            if key not in ('battery', 'storage') and item['result'] is None and elapsed >= item['planned_s']:
                self.finish_item(key, 'PASS', '有效时长达标，校验/监测无错误')
        self.last_sample = time.monotonic()
        self.status['latest'] = data
        self.status['wall_s'] = self.last_sample - self.started
        with self.state_lock:
            if self.stop_requested.is_set():
                return
            self.status['state'] = (self.battery['state']
                                    if self.status['items'].get('battery', {}).get('result') is None
                                    and self.plan['items']['battery']['enabled']
                                    and self.battery['state'] == 'WAIT_OPERATOR' else 'RUNNING')
            self.emit('metric', data)

    def run(self):
        try:
            self.emit('event', {'state': 'PREFLIGHT', 'reason': '开始设备预检'})
            self.info = preflight(self.plan, self.request['device'], emit=self.emit,
                                  current_run_id=self.request['run_id'])
            self.status['hardware'] = self.info
            self.collector = collectors.SystemCollector()
            self.start_items()
            self.last_sample = time.monotonic()
            self.guard_thread = threading.Thread(target=self.guard, daemon=True)
            self.guard_thread.start()
            next_sample = 0
            self.save()
            self.control_thread = threading.Thread(target=self.control_loop, daemon=True)
            self.control_thread.start()
            if self.plan['items']['storage']['enabled']:
                self.storage_thread = threading.Thread(target=self.storage_loop, daemon=True)
                self.storage_thread.start()
            while any(i['result'] is None for i in self.status['items'].values()):
                if self.accept_operator_stop():
                    break
                if self.safety_reason:
                    self.status['state'] = 'SAFETY_STOP'
                    for key in self.status['items']:
                        self.finish_item(key, 'FAIL', self.safety_reason)
                    self.status['safety_stop'] = self.safety_reason
                    break
                now = time.monotonic()
                if now >= next_sample:
                    self.sample()
                    next_sample = now + self.plan['sample_interval_s']
                    self.save()
                time.sleep(0.2)
        except Exception as exc:
            self.status['error'] = str(exc)
            self.emit('event', {'error': str(exc)})
            if self.started is None:
                self.status.update(state='BLOCKED', result='BLOCKED')
            else:
                for key in self.status['items']:
                    self.finish_item(key, 'INCOMPLETE', '执行异常: ' + str(exc))
        finally:
            self.guard_stop.set()
            if hasattr(self, 'control_thread'):
                self.control_thread.join()
            if hasattr(self, 'storage_thread'):
                self.storage_thread.join()
            if hasattr(self, 'guard_thread'):
                self.guard_thread.join(timeout=3)
            cleanup_errors = []
            storage_retried = any(name.startswith('storage_') for name in self.processes)
            for name in list(self.processes):
                try:
                    self.stop_process(name)
                except Exception as exc:
                    cleanup_errors.append(name + ': ' + str(exc))
            storage_errors = self.storage_stop_evidence() if self.storage else []
            if storage_retried:
                # 首次回收失败时结果可能尚未落盘，最终重试后重新分析并刷新证据。
                item = self.status['items']['storage']
                if storage_errors:
                    item['result'] = 'FAIL'
                    item['stop_reason'] += '；最终回收分析：' + '；'.join(storage_errors)
                confirmed = all(value.get('cleanup', {}).get('confirmed') for value in
                                item['storage_progress'].values() if value['pending'])
                item['stop_reason'] += '；最终重试后进程回收' + ('已确认' if confirmed else '未确认')
                files_removed = all(task.get('file_cleanup', {}).get('removed') for task in self.storage.values()
                                    if task.get('file'))
                item['stop_reason'] += '，临时文件清理' + ('已确认' if files_removed else '未确认')
            self.status['cleanup_errors'] = cleanup_errors
            self.status['cleanup_confirmed'] = not cleanup_errors
            file_errors = {name: task['file_cleanup'] for name, task in self.storage.items()
                           if task.get('file_cleanup') and not task['file_cleanup'].get('removed')}
            self.status['file_cleanup_errors'] = file_errors
            self.status['file_cleanup_confirmed'] = not file_errors
            reservation_errors = {name: task.get('reservation_cleanup_error') or '进程或临时文件清理未确认'
                                  for name, task in self.storage.items()
                                  if self.status['write_ledger'][name].get('unresolved_bytes')}
            self.status['reservation_cleanup_errors'] = reservation_errors
            self.status['reservation_cleanup_confirmed'] = not reservation_errors
            if self.status['state'] != 'BLOCKED':
                self.status['state'] = 'FINISHED'
                if self.safety_reason:
                    self.status['safety_stop'] = self.safety_reason
                    for item in self.status['items'].values():
                        if item.get('result') in (None, 'PASS'):
                            item.update(result='FAIL', stop_reason=self.safety_reason)
                self.status['result'] = conclusion(self.status['items'])
                if cleanup_errors and self.status['result'] == 'PASS':
                    self.status['result'] = 'INCOMPLETE'
            self.status['finished_at'] = utc_now()
            self.emit('event', {'state': self.status['state'], 'result': self.status['result'], 'cleanup_errors': cleanup_errors})
            self.save()


def read_run_status(directory, run_id):
    """控制命令读取实时存活证据；历史 status.json 本身不代表进程仍在运行。"""
    boot = collectors.read('/proc/sys/kernel/random/boot_id')
    try:
        status = json.loads((directory / 'status.json').read_text())
        if not isinstance(status, dict) or 'state' not in status:
            raise ValueError('运行状态结构无效')
    except (FileNotFoundError, ValueError) as exc:
        # 启动命令刚创建目录时给 supervisor 留出初始化窗口；窗口内不能认定异常退出。
        initializing = directory.exists() and time.time() - directory.stat().st_mtime < 30
        status = {'run_id': run_id, 'state': 'READY' if initializing else
                  ('MISSING' if isinstance(exc, FileNotFoundError) else 'CORRUPT'),
                  'result': None, 'status_error': str(exc), 'agent_alive': None}
        if not initializing:
            # 独占锁空闲可证明当前没有本工具的 supervisor；不凭文件丢失猜测存活。
            with (directory.parent / 'device.lock').open('a+') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    status['agent_alive'] = False
                    fcntl.flock(lock, fcntl.LOCK_UN)
    else:
        if status.get('run_id') != run_id:
            raise ValueError('运行状态身份不一致')
        if status['state'] not in ('FINISHED', 'BLOCKED'):
            try:
                process = Path('/proc/%s' % status.get('pid'))
                arguments = (process / 'cmdline').read_bytes().split(b'\x00')
                state = (process / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                status['agent_alive'] = (boot == status.get('boot_id') and state not in ('Z', 'X')
                                         and b'run' in arguments and run_id.encode() in arguments)
            except FileNotFoundError:
                status['agent_alive'] = False
    status['current_boot_id'] = boot
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('probe', 'launch', 'run', 'status', 'stop', 'confirm'))
    parser.add_argument('--run-id')
    parser.add_argument('--request')
    parser.add_argument('--token', type=int)
    parser.add_argument('--phase', choices=('charge', 'discharge'))
    parser.add_argument('--operator', default='')
    args = parser.parse_args()
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == 'probe':
        print(json.dumps(collectors.probe(), ensure_ascii=False))
        return
    directory = run_path(args.run_id)
    if args.action == 'launch':
        # exclusive mkdir 是幂等边界。相同 run_id 的第二次启动只返回现状。
        if directory.exists():
            print(json.dumps({'accepted': True, 'existing': True}))
            return
        directory.mkdir(mode=0o700)
        request = json.loads(Path(args.request).read_text())
        if request['run_id'] != args.run_id:
            raise ValueError('运行身份不一致')
        atomic_json(directory / 'request.json', request)
        with (directory / 'supervisor.log').open('ab', buffering=0) as stream:
            process = subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0]), 'run', '--run-id', args.run_id],
                                       stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                       start_new_session=True, close_fds=True)
        print(json.dumps({'accepted': True, 'pid': process.pid}))
    elif args.action == 'run':
        supervisor = Supervisor(directory, json.loads((directory / 'request.json').read_text()))
        with (ROOT / 'device.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                supervisor.status.update(state='BLOCKED', result='BLOCKED', error='设备已有本工具的运行持有锁')
                supervisor.save()
                return
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps({'run_id': args.run_id, 'pid': os.getpid(), 'owner': supervisor.plan.get('operator')}))
            lock.flush()
            def stop_signal(signum, frame):
                atomic_json(directory / 'stop.json', {'signal': signum})
            signal.signal(signal.SIGTERM, stop_signal)
            signal.signal(signal.SIGINT, stop_signal)
            supervisor.save()
            supervisor.run()
    elif args.action == 'status':
        print(json.dumps(read_run_status(directory, args.run_id), ensure_ascii=False))
    elif args.action == 'stop':
        atomic_json(directory / 'stop.json', {'operator': args.operator, 'utc': utc_now()})
        print('{"accepted": true}')
    elif args.action == 'confirm':
        atomic_json(directory / 'confirm.json', {'token': args.token, 'phase': args.phase,
                                               'operator': args.operator, 'utc': utc_now()})
        print('{"accepted": true}')


if __name__ == '__main__':
    main()
