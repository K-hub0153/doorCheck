"""Linux 设备端执行器：单调计时、本地保护、独占锁、追加日志与可恢复读取。"""
import argparse
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
import threading
import time

from ..domain.models import conclusion, digest, utc_now
from ..domain.policies import validate_plan
from . import collectors, lte, runners

ROOT = Path('/var/tmp/master-aging')


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_path(run_id):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('非法 run_id')
    return ROOT / run_id


def preflight(plan, expected):
    validate_plan(plan)
    info = collectors.probe()
    info['tool_versions'] = {}
    if not info['sn'] or info['sn'] != expected['sn']:
        raise ValueError('设备 SN 与所选身份不一致')
    if info['chip'] == 'unknown' or info['chip'] != info['sn_chip']:
        raise ValueError('真实硬件与 SN 机型无法核验一致；请确认设备树/固件')
    if expected.get('fingerprint') and info['fingerprint'] != expected['fingerprint']:
        raise ValueError('设备指纹发生变化')
    temperature_limit = plan.get('protection', {}).get('cpu_stop_temperature_c')
    if temperature_limit is not None:
        if not info['temperatures']:
            raise ValueError('未发现可读 thermal zone，无法启用本地温度保护')
        if max(info['temperatures'].values()) >= temperature_limit:
            raise ValueError('当前温度已到保护停止点')
    items = plan['items']
    for key in ('cpu', 'ddr'):
        if items[key]['enabled'] and not info['tools']['stress-ng']:
            raise ValueError('设备缺少 stress-ng，自动部署后仍不可用，请查看环境部署日志')
    if items['cpu']['enabled'] or items['ddr']['enabled']:
        info['tool_versions']['stress-ng'] = collectors.command(['stress-ng', '--version'])
    if items['ddr']['enabled']:
        cfg = items['ddr']
        allocation = min(info['memory']['MemTotal'] * cfg['max_memory_pct'] // 100,
                         info['memory']['MemAvailable'] - cfg['reserve_mib'] * 2**20)
        if allocation < 16 * 2**20:
            raise ValueError('DDR 可用内存不足，无法保留系统保护空间')
        info['ddr_bytes'] = int(allocation)
    if items['storage']['enabled']:
        if not info['tools']['fio'] or not collectors.command(['fio', '--version']).startswith('fio-'):
            raise ValueError('设备缺少真正的 fio 工具')
        info['tool_versions']['fio'] = collectors.command(['fio', '--version'])
        info['journal_storage'] = collectors.storage_target(str(ROOT), info['disks'])
        info['storage_targets'] = [collectors.storage_target(d, info['disks']) for d in items['storage']['targets']]
        identities = [v['identity'] for v in info['storage_targets']]
        if len(set(identities)) != len(identities):
            raise ValueError('同一物理盘不能重复分配多个独立预算')
        required = items['storage']['chunk_mib'] * 2**20 + plan['min_free_space_mib'] * 2**20
        if any(v['free_bytes'] < required for v in info['storage_targets']):
            raise ValueError('存储目标剩余空间不足')
        cfg = items['storage']
        total_plan = (cfg['rounds'] * cfg['chunk_mib'] + cfg['log_reserve_mib'] + cfg['metadata_reserve_mib']) * 2**20
        for target in info['storage_targets']:
            ledger = ROOT / 'write-ledger' / (target['identity'] + '.json')
            if ledger.exists():
                data = json.loads(ledger.read_text())
                if data.get('unresolved_bytes'):
                    raise ValueError('目标盘存在未决写入预留，需要人工核对后才能进行新的存储测试')
                limit = cfg.get('cumulative_write_budget_gib')
                if limit is not None and data['reserved_bytes'] + total_plan > limit * 2**30:
                    raise ValueError('该盘累计剩余批准预算不足以完成计划')
    if items['battery']['enabled']:
        if not info['tools']['i2cget']:
            raise ValueError('设备缺少 i2cget')
        info['battery_bus'] = 2 if info['chip'] == 'hisi' else 0
        info['battery_initial'] = collectors.battery_sample(info['battery_bus'])
        info['battery_identity'] = collectors.battery_identity(info['battery_bus'])
        if not info['battery_initial']['valid']:
            raise ValueError('电池必读寄存器读取失败')
    if items['usb']['enabled'] and not collectors.usb_sample(items['usb']['min_speed_mbps'])['ok']:
        raise ValueError('未发现达到最低速率的 USB 夹具')
    if items['hdmi']['enabled'] and info['chip'] != 'hisi':
        if not collectors.hdmi_sample(info['hdmi_paths'])['ok']:
            raise ValueError('未发现已连接的 HDMI')
    if items['lte']['enabled']:
        result = lte.sample(items['lte'])
        if not result['ok']:
            raise ValueError('4G 预检失败: ' + json.dumps(result, ensure_ascii=False))
        info['lte_initial'] = result
    if shutil.disk_usage(ROOT).free < plan['min_free_space_mib'] * 2**20:
        raise ValueError('设备日志目录剩余空间不足')
    return info


class Supervisor:
    def __init__(self, directory, request):
        self.directory = Path(directory)
        self.request = request
        self.plan = request['plan']
        self.info = {}
        self.processes = {}
        self.storage = {}
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
                        'start_count': 0, 'stop_count': 0, 'failures': 0, 'segments': [],
                        'start_required': battery['start_confirm_samples'],
                        'start_current_ma': battery['start_current_ma'],
                        'charge_mah': 0.0, 'discharge_mah': 0.0, 'charge_wh': 0.0, 'discharge_wh': 0.0}
        self.previous_battery = None
        self.timed_out = set()

    def save(self):
        self.status['updated_at'] = utc_now()
        self.status['seq'] = self.seq
        self.status['battery'] = self.battery
        atomic_json(self.directory / 'status.json', self.status)

    def emit(self, kind, data):
        self.seq += 1
        record = {'seq': self.seq, 'kind': kind, 'utc': utc_now(), 'monotonic': time.monotonic(),
                  'boot_id': self.status['boot_id'], 'run_id': self.request['run_id'], 'data': data}
        with (self.directory / 'records.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        return record

    def guard(self):
        while not self.guard_stop.wait(1):
            try:
                temperature_limit = self.plan.get('protection', {}).get('cpu_stop_temperature_c')
                if temperature_limit is not None:
                    temps = collectors.temperatures()
                    if not temps:
                        raise RuntimeError('CPU 温度传感器失效')
                    if max(temps.values()) >= temperature_limit:
                        raise RuntimeError('CPU 超温')
                if time.monotonic() - self.last_sample > self.plan['max_sample_gap_s']:
                    raise RuntimeError('采样/调度超时，保护停止')
                for name in ('cpu', 'ddr'):
                    item = self.status['items'].get(name)
                    process = self.processes.get(name)
                    if item and process and item['result'] is None and name not in self.timed_out:
                        # stress-ng 自身按计划到时退出，留出汇总和回收 worker 的时间。
                        if time.monotonic() - item['started_monotonic'] >= item['planned_s'] + 5:
                            self.timed_out.add(name)
                            process.request_stop()
                for path in [ROOT] + [Path(t['directory']) for t in self.info.get('storage_targets', [])]:
                    if shutil.disk_usage(path).free < self.plan['min_free_space_mib'] * 2**20:
                        raise RuntimeError('剩余空间低于保护线')
                size = sum(p.stat().st_size for p in self.directory.rglob('*') if p.is_file())
                if size > self.plan['max_log_mib'] * 2**20:
                    raise RuntimeError('设备日志达到上限')
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

    def finish_item(self, key, result, reason):
        item = self.status['items'][key]
        if item.get('result') is not None:
            return
        names = [key] if key in self.processes else []
        if key == 'storage':
            names += [name for name in self.processes if name.startswith('storage_')]
        for name in names:
            try:
                code = self.stop_process(name)
                if name == key:
                    item['exit_code'] = code
                if result == 'PASS' and code != 0:
                    result, reason = 'INCOMPLETE', '压测退出码不是 0: %s' % code
            except Exception as exc:
                if result == 'PASS':
                    result = 'INCOMPLETE'
                reason += '；压测进程停止未确认: ' + str(exc)
        item.update(result=result, stop_reason=reason, finished_at=utc_now())
        self.emit('event', {'item': key, 'result': result, 'reason': reason})

    def start_items(self):
        self.started = time.monotonic()
        for key, config in self.plan['items'].items():
            if not config.get('enabled'):
                continue
            item = {'result': None, 'effective_s': 0, 'planned_s': config['duration_min'] * 60,
                    'started_at': utc_now(), 'started_monotonic': self.started}
            self.status['items'][key] = item
            if key == 'hdmi' and self.info['chip'] == 'hisi':
                self.finish_item(key, 'NOT_APPLICABLE', '四代主控无 HDMI')
            elif key in ('cpu', 'ddr'):
                args = ['stress-ng', '--verify', '--metrics-brief']
                if key == 'cpu':
                    args += ['--cpu', str(self.info['cpu_count']), '--cpu-method', 'all']
                else:
                    args += ['--vm', '1', '--vm-bytes', str(self.info['ddr_bytes']), '--vm-method', 'all', '--vm-keep']
                    item['scope'] = 'DDR 正确性压力；带宽基线需另行使用已验证的 STREAM 工具测量'
                    # 在启动负载前记录基线，不能把系统原有 swap 占用当作本轮换出。
                    item['swap_baseline'] = self.swap_snapshot(collectors.memory(), collectors.read('/proc/vmstat'))
                args += ['--timeout', str(config['duration_min'] * 60) + 's']
                self.processes[key] = runners.Process(args, self.directory, key)
                item['started_monotonic'] = time.monotonic()
                item['pid'] = self.processes[key].process.pid
                item['argv'] = args
            elif key == 'storage':
                item['scope'] = '固定字节写入与 direct 校验读取；轮次间及结束后只读观察至计划到时'
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
                                    'reason': '请逐设备' + operation + '并确认', 'token': 0})
            elif key == 'lte':
                item['scope'] = '沿用产测 AT 模组识别及 RSRP/RSRQ/SINR 阈值，周期检测信号'
            elif key == 'hdmi':
                item['scope'] = 'HDMI 连接、EDID 与模式；人工显示确认：' + ('已记录' if config.get('display_confirmed') else '未记录')
        self.status['state'] = 'RUNNING'
        self.emit('event', {'state': 'RUNNING', 'hardware': self.info})

    def reserve(self, name, size, overhead=False):
        ledger = self.status['write_ledger'][name]
        config = self.plan['items']['storage']
        if ledger['reserved_bytes'] + size > config['write_budget_gib'] * 2**30:
            return False
        global_path = ROOT / 'write-ledger' / (ledger['identity'] + '.json')
        global_path.parent.mkdir(parents=True, exist_ok=True)
        total = json.loads(global_path.read_text()) if global_path.exists() else {'reserved_bytes': 0, 'unresolved_bytes': 0}
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

    def storage_tick(self, now):
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
                    if code:
                        self.finish_item('storage', 'FAIL', 'fio 非零退出；保留未决预留量')
                        return
                    try:
                        result = runners.fio_result(self.directory / (name + '.json'), size)
                    except Exception as exc:
                        self.finish_item('storage', 'FAIL', 'fio 校验结果异常: ' + str(exc))
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
            due = self.started + task['completed'] * item['planned_s'] / config['rounds']
            if now < due:
                continue
            if not self.reserve(name, size):
                self.finish_item('storage', 'INCOMPLETE', '写入预算不足')
                return
            # 每轮预留必须先落盘。重连仅补采，禁止启动同 run_id 或重发写入批次。
            self.emit('write_reservation', dict(target=name, bytes=size, round=task['completed'] + 1))
            path = Path(task['target']['directory']) / ('.master-aging-' + self.request['run_id'] + '.bin')
            if path.exists():
                raise RuntimeError('发现未决存储文件，禁止覆盖')
            task['file'] = path
            task['pending'] = True
            self.processes[name] = runners.Process(
                runners.fio_args(path, size, self.directory / (name + '.json')), self.directory, name)

    def battery_tick(self, sample, now):
        item = self.status['items']['battery']
        b, cfg, protection = self.battery, self.plan['items']['battery'], self.plan.get('protection', {})
        if not sample['valid']:
            b['failures'] += 1
            self.previous_battery = None
            b['start_count'] = b['stop_count'] = 0
            if b['failures'] >= cfg['max_read_failures']:
                self.safety_reason = '电池连续读取失败'
            return
        b['failures'] = 0
        current, voltage, temperature = sample['current_ma'], sample['voltage_mv'], sample['temperature_c']
        limits = protection.get('battery_%s_temperature_range_c' % b['phase'])
        minimum = protection.get('battery_min_voltage_mv')
        maximum = protection.get('battery_max_voltage_mv')
        max_current = protection.get('battery_max_abs_current_ma')
        min_soc = protection.get('discharge_min_soc_pct')
        if ((limits is not None and not limits[0] <= temperature <= limits[1]) or
                (minimum is not None and voltage < minimum) or
                (maximum is not None and voltage > maximum) or
                (max_current is not None and abs(current) > max_current) or
                (min_soc is not None and b['phase'] == 'discharge' and sample['soc_pct'] <= min_soc)):
            self.safety_reason = '电池温度/电压/电流/SOC 达到保护线；请接回适配器'
            return
        if item['result'] is not None:
            return
        if b['state'] == 'WAIT_OPERATOR':
            self.previous_battery = None
            confirmation = self.directory / 'confirm.json'
            if confirmation.exists():
                value = json.loads(confirmation.read_text())
                confirmation.unlink()
                if value.get('token') == b['index'] and value.get('phase') == b['phase']:
                    b['state'], b['wait_started'] = 'WAIT_CURRENT', now
                    b['confirmed'] = True
                    self.emit('operator', value)
            return
        sign = 1 if b['phase'] == 'charge' else -1
        directed = current * sign
        if directed < -cfg['stop_current_ma']:
            self.finish_item('battery', 'INCOMPLETE', '电流方向异常，需核验电源/负载条件')
            return
        if b['state'] == 'WAIT_CURRENT':
            b['start_count'] = b['start_count'] + 1 if directed >= cfg['start_current_ma'] else 0
            if now - b['wait_started'] > cfg['start_timeout_min'] * 60:
                self.finish_item('battery', 'INCOMPLETE', '适配器/负载条件不足，启动等待超时')
                return
            if b['start_count'] >= cfg['start_confirm_samples']:
                b['state'], b['started'] = 'RUNNING', now
                b['phase_effective_s'] = 0
                self.previous_battery = (now, current, voltage)
                self.emit('event', {'item': 'battery', 'phase': b['phase'], 'state': 'RUNNING'})
            return
        duration = cfg[b['phase'] + '_duration_min'] * 60
        # 仅配置单一方向时将所有循环合并，持续充/放电，不要求中途重复切电确认。
        if len(self.battery_phases) == 1:
            duration *= cfg['cycles']
        elapsed = b.get('phase_effective_s', 0)
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
        if elapsed < duration and not natural:
            return
        if natural and elapsed < duration and cfg['completion_mode'] == 'fixed_duration':
            self.finish_item('battery', 'INCOMPLETE', '充放电提前停止，未完成固定时长')
            return
        if elapsed >= duration and cfg['completion_mode'] == 'stop_node' and not natural:
            self.finish_item('battery', 'INCOMPLETE', '到达最大阶段时间，未确认正常停止节点')
            return
        b['segments'].append({'phase': b['phase'], 'index': b['index'], 'effective_s': min(elapsed, duration),
                              'end_soc': sample['soc_pct'], 'stop_reason': '停止节点' if natural else '到时'})
        b['index'] += 1
        phase_count = cfg['cycles'] * len(self.battery_phases) if len(self.battery_phases) > 1 else 1
        if b['index'] >= phase_count:
            self.finish_item('battery', 'PASS', '所有已配置的电池阶段完成')
            return
        b.update(phase=self.battery_phases[b['index'] % len(self.battery_phases)],
                 state='WAIT_OPERATOR', confirmed=False, start_count=0, stop_count=0)
        self.previous_battery = None
        self.emit('event', {'item': 'battery', 'phase': b['phase'], 'state': 'WAIT_OPERATOR', 'token': b['index']})

    @staticmethod
    def swap_snapshot(memory, vmstat):
        counters = dict(line.split() for line in vmstat.splitlines() if len(line.split()) == 2)
        return {'used_bytes': max(0, memory.get('SwapTotal', 0) - memory.get('SwapFree', 0)),
                'pswpout': int(counters['pswpout']) if 'pswpout' in counters else None}

    def sample(self):
        now = time.monotonic()
        data = self.collector.sample()
        if data.get('kernel_errors'):
            self.safety_reason = '检测到新的内核/OOM/I/O 错误，详见采样证据'
        if data.get('kernel_log_gap'):
            raise RuntimeError('内核环形日志缺段，证据不足')
        if self.plan['items']['battery']['enabled']:
            data['battery'] = collectors.battery_sample(self.info['battery_bus'])
            self.battery_tick(data['battery'], time.monotonic())
        if self.safety_reason:
            self.emit('metric', data)
            self.status['latest'] = data
            return
        for key, item in self.status['items'].items():
            if item['result'] is not None:
                continue
            cfg = self.plan['items'][key]
            elapsed = now - item['started_monotonic']
            if key != 'battery':
                item['effective_s'] = min(elapsed, item['planned_s'])
            if key in ('cpu', 'ddr'):
                code = self.processes[key].poll()
                swapping = False
                if key == 'ddr':
                    baseline = item['swap_baseline']
                    latest_swap = self.swap_snapshot(data['memory'], data.get('vmstat', ''))
                    item['swap_latest'] = latest_swap
                    swapping = (latest_swap['used_bytes'] > baseline['used_bytes'] or
                                (latest_swap['pswpout'] is not None and baseline['pswpout'] is not None
                                 and latest_swap['pswpout'] > baseline['pswpout']))
                if swapping:
                    self.finish_item(key, 'INCOMPLETE', '本轮 DDR 压测期间出现新增 swap 换出，无法保证工作集驻留内存')
                elif elapsed >= item['planned_s']:
                    # 正常到时先等 stress-ng 汇总；非零退出和清理失败仍不能通过。
                    if code is not None or key in self.timed_out or elapsed >= item['planned_s'] + 5:
                        self.finish_item(key, 'PASS', '计划时长已到，已核验压测退出和进程清理')
                    continue
                elif code is not None:
                    self.finish_item(key, 'FAIL' if code else 'INCOMPLETE', '压测进程提前退出: %s' % code)
                elif key == 'cpu' and elapsed > cfg['grace_s'] and data['cpu']['busy_pct'] is not None and data['cpu']['busy_pct'] < cfg['target_busy_pct']:
                    self.finish_item(key, 'FAIL', 'CPU 有效计算忙碌率未达到目标')
            elif key == 'battery':
                pass  # 电池采样与保护在上方执行，包括项目完成后的剩余综合负载阶段。
            elif key in ('usb', 'hdmi', 'lte'):
                try:
                    if key == 'usb':
                        data[key] = collectors.usb_sample(cfg['min_speed_mbps'])
                        # 夹具身份及 devnum 发生变化保留为重枚举故障。
                        identities = data[key]['devices']
                        if 'usb_baseline' not in item:
                            item['usb_baseline'] = identities
                        elif item['usb_baseline'] != identities:
                            data[key]['ok'] = False
                    elif key == 'hdmi':
                        data[key] = collectors.hdmi_sample(self.info['hdmi_paths'])
                    else:
                        data[key] = lte.sample(cfg)
                    if not data[key]['ok']:
                        self.finish_item(key, 'FAIL', '连续监测发现异常，详见原始样本；归因待复核')
                except Exception as exc:
                    data[key] = {'ok': False, 'error': str(exc)}
                    self.finish_item(key, 'INCOMPLETE', '采样失败: ' + str(exc))
            if self.safety_reason:
                self.emit('metric', data)
                self.status['latest'] = data
                return
            if key != 'battery' and item['result'] is None and elapsed >= item['planned_s']:
                if key == 'storage' and any(t['pending'] or t['completed'] < cfg['rounds'] for t in self.storage.values()):
                    self.finish_item(key, 'INCOMPLETE', '到时仍有未完成的写入校验轮次')
                else:
                    self.finish_item(key, 'PASS', '有效时长达标，校验/监测无错误')
        self.last_sample = time.monotonic()
        self.status['latest'] = data
        self.status['wall_s'] = self.last_sample - self.started
        self.status['state'] = (self.battery['state']
                                if self.status['items'].get('battery', {}).get('result') is None
                                and self.plan['items']['battery']['enabled']
                                and self.battery['state'] in ('WAIT_OPERATOR', 'WAIT_CURRENT') else 'RUNNING')
        self.emit('metric', data)

    def run(self):
        try:
            self.info = preflight(self.plan, self.request['device'])
            self.status['hardware'] = self.info
            self.collector = collectors.SystemCollector()
            self.start_items()
            self.last_sample = time.monotonic()
            self.guard_thread = threading.Thread(target=self.guard, daemon=True)
            self.guard_thread.start()
            next_sample = 0
            self.save()
            while any(i['result'] is None for i in self.status['items'].values()):
                if (self.directory / 'stop.json').exists():
                    self.status['state'] = 'STOPPING'
                    for key in self.status['items']:
                        self.finish_item(key, 'ABORTED', '操作者停止')
                    break
                if self.safety_reason:
                    self.status['state'] = 'SAFETY_STOP'
                    for key in self.status['items']:
                        self.finish_item(key, 'FAIL', self.safety_reason)
                    self.status['safety_stop'] = self.safety_reason
                    break
                now = time.monotonic()
                self.storage_tick(now)
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
            if hasattr(self, 'guard_thread'):
                self.guard_thread.join(timeout=3)
            cleanup_errors = []
            for name in list(self.processes):
                try:
                    self.stop_process(name)
                except Exception as exc:
                    cleanup_errors.append(name + ': ' + str(exc))
            self.status['cleanup_errors'] = cleanup_errors
            self.status['cleanup_confirmed'] = not cleanup_errors
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
        status_path = directory / 'status.json'
        if not status_path.exists():
            initializing = directory.exists() and time.time() - directory.stat().st_mtime < 30
            print(json.dumps({'run_id': args.run_id, 'state': 'READY' if initializing else 'MISSING',
                              'agent_alive': initializing, 'result': None}, ensure_ascii=False))
            return
        status = json.loads(status_path.read_text())
        if status['state'] not in ('FINISHED', 'BLOCKED'):
            boot = collectors.read('/proc/sys/kernel/random/boot_id')
            try:
                arguments = Path('/proc/%s/cmdline' % status.get('pid')).read_bytes().split(b'\x00')
                status['agent_alive'] = (boot == status['boot_id'] and b'run' in arguments
                                         and args.run_id.encode() in arguments)
            except OSError:
                status['agent_alive'] = False
        print(json.dumps(status, ensure_ascii=False))
    elif args.action == 'stop':
        atomic_json(directory / 'stop.json', {'operator': args.operator, 'utc': utc_now()})
        print('{"accepted": true}')
    elif args.action == 'confirm':
        atomic_json(directory / 'confirm.json', {'token': args.token, 'phase': args.phase,
                                               'operator': args.operator, 'utc': utc_now()})
        print('{"accepted": true}')


if __name__ == '__main__':
    main()
