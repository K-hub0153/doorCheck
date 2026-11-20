import glob
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone


def read(path, default=''):
    try:
        return Path(path).read_text(errors='replace').strip().replace('\x00', '')
    except OSError:
        return default


def command(args, timeout=3):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError('%s: %s' % (args[0], result.stderr.strip()[:400]))
    return result.stdout.strip()


def temperatures():
    result = {}
    for path in glob.glob('/sys/class/thermal/thermal_zone*/temp'):
        try:
            result[path] = int(read(path)) / 1000.0
        except ValueError:
            continue
    return result


def memory():
    result = {}
    for line in read('/proc/meminfo').splitlines():
        parts = line.split()
        if len(parts) > 1:
            result[parts[0].rstrip(':')] = int(parts[1]) * 1024
    return result


def probe():
    sn = read('/etc/serial-id')
    stable_board = ' '.join([read('/proc/device-tree/compatible'), read('/proc/device-tree/model'),
                             read('/sys/firmware/devicetree/base/compatible')])
    board = stable_board + ' ' + read('/proc/cpuinfo')
    lower = board.lower()
    chip = 'wdse' if 'rk3576' in lower else ('hisi' if '310p' in lower else 'unknown')
    expected = 'wdse' if sn.lower().startswith('wdse') else ('hisi' if sn.lower().startswith('hisi') else 'unknown')
    disks = []
    for path in Path('/sys/block').glob('*'):
        name = path.name
        if name.startswith(('loop', 'ram', 'dm-', 'zram', 'md')):
            continue
        if read(path / 'removable') != '0' or 'usb' in str(path.resolve()).lower():
            continue
        serial = read(path / 'device/serial') or read(path / 'device/cid')
        if not serial and shutil.which('lsblk'):
            try:
                serial = command(['lsblk', '-dn', '-o', 'SERIAL', '/dev/' + name])
            except Exception:
                pass
        disks.append({'name': name, 'model': read(path / 'device/model'),
                      'serial': serial,
                      'size_bytes': int(read(path / 'size', '0')) * 512,
                      'sysfs': str(path.resolve()),
                      'emmc_lifetime': read(path / 'device/life_time'),
                      'emmc_pre_eol': read(path / 'device/pre_eol_info')})
    return {'sn': sn, 'chip': chip, 'sn_chip': expected, 'arch': platform.machine(),
            'firmware': read('/etc/os-release'), 'kernel': platform.release(),
            'boot_id': read('/proc/sys/kernel/random/boot_id'),
            'fingerprint': hashlib.sha256((sn + stable_board + read('/etc/machine-id') + platform.machine()).encode()).hexdigest(),
            'memory': memory(), 'disks': disks, 'temperatures': temperatures(),
            'hdmi_paths': glob.glob('/sys/class/drm/*HDMI*/status'),
            'tools': {k: shutil.which(k) for k in ('stress-ng', 'fio', 'i2cget', 'ip', 'ping')},
            'python': platform.python_version(), 'libc': platform.libc_ver(),
            'utc': datetime.now(timezone.utc).isoformat(),
            'cpu_count': os.cpu_count() or 1}


class SystemCollector:
    def __init__(self):
        self.previous_cpu = None
        self.kernel_tail = command(['dmesg']).splitlines()[-1:]

    def sample(self):
        values = [int(v) for v in read('/proc/stat').splitlines()[0].split()[1:9]]
        cpu = {'busy_pct': None, 'user_pct': None, 'system_pct': None, 'iowait_pct': None}
        if self.previous_cpu:
            delta = [a - b for a, b in zip(values, self.previous_cpu)]
            total = sum(delta)
            if total > 0:
                cpu = {'busy_pct': (total - delta[3] - delta[4]) * 100.0 / total,
                       'user_pct': (delta[0] + delta[1]) * 100.0 / total,
                       'system_pct': delta[2] * 100.0 / total, 'iowait_pct': delta[4] * 100.0 / total}
        self.previous_cpu = values
        freq = {p: read(p) for p in glob.glob('/sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq')}
        kernel = command(['dmesg']).splitlines()
        gap = bool(self.kernel_tail and self.kernel_tail[0] not in kernel)
        if self.kernel_tail and self.kernel_tail[0] in kernel:
            new_kernel = kernel[kernel.index(self.kernel_tail[0]) + 1:]
        else:
            new_kernel = kernel
        self.kernel_tail = kernel[-1:]
        errors = [line for line in new_kernel if re.search(
            r'out of memory|oom-kill|killed process|BUG:|kernel panic|I/O error|hardware error|EDAC.*(?:error|uncorrect)', line, re.I)]
        return {'sample_monotonic': time.monotonic(), 'sample_utc': datetime.now(timezone.utc).isoformat(),
                'cpu': cpu, 'temperature_c': temperatures(), 'memory': memory(),
                'frequency_khz': freq, 'load': read('/proc/loadavg'),
                'diskstats': read('/proc/diskstats'), 'vmstat': read('/proc/vmstat'),
                'kernel_new': new_kernel[-200:], 'kernel_errors': errors, 'kernel_log_gap': gap}


def battery_sample(bus):
    registers = {'mode': 0x03, 'temperature': 0x08, 'voltage': 0x09, 'current': 0x0A,
                 'soc': 0x0D, 'absolute_soc': 0x0E, 'remaining': 0x0F,
                 'fcc': 0x10, 'status': 0x16, 'cycles': 0x17, 'design': 0x18}
    raw, errors = {}, {}
    for key, address in registers.items():
        try:
            value = command(['i2cget', '-y', str(bus), '0x0b', hex(address), 'w'], timeout=1)
            if not re.fullmatch(r'0x[0-9a-fA-F]{4}', value):
                raise ValueError('invalid word')
            raw[key] = int(value, 16)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            errors[key] = str(exc)
    required = ('temperature', 'voltage', 'current', 'soc')
    current = raw.get('current')
    if current is not None and current >= 32768:
        current -= 65536
    return {'valid': all(k in raw for k in required), 'raw': raw, 'errors': errors,
            'temperature_c': raw['temperature'] / 10.0 - 273.15 if 'temperature' in raw else None,
            'voltage_mv': raw.get('voltage'), 'current_ma': current, 'soc_pct': raw.get('soc'),
            'capacity_unit': ('10mWh' if raw['mode'] & 0x8000 else 'mAh') if 'mode' in raw else 'unknown'}


def battery_identity(bus):
    result = {}
    if not shutil.which('i2ctransfer'):
        return {'manufacturer': None, 'model': None, 'note': '缺少可选的 i2ctransfer'}
    for key, register in (('manufacturer', '0x20'), ('model', '0x21')):
        try:
            output = command(['i2ctransfer', '-y', str(bus), 'w1@0x0b', register, 'r33'], 2)
            data = [int(v, 16) for v in output.split()]
            if not data or not 1 <= data[0] <= 32 or len(data) < data[0] + 1:
                raise ValueError('SBS block 长度无效')
            result[key] = bytes(data[1:1 + data[0]]).decode('ascii', errors='replace').rstrip('\x00')
            result[key + '_raw'] = output
        except Exception as exc:
            result[key] = None
            result[key + '_error'] = str(exc)
    return result


def usb_sample(min_speed):
    devices = []
    for path in Path('/sys/bus/usb/devices').glob('*'):
        if path.name.startswith('usb') or ':' in path.name:
            continue
        try:
            speed = float(read(path / 'speed'))
            if speed >= min_speed:
                devices.append({'path': path.name, 'speed_mbps': speed,
                                'vendor': read(path / 'idVendor'), 'product': read(path / 'idProduct'),
                                'serial': read(path / 'serial'), 'devnum': read(path / 'devnum')})
        except ValueError:
            pass
    return {'ok': bool(devices), 'devices': devices}


def hdmi_sample(paths):
    values = {p: {'status': read(p), 'modes': read(Path(p).parent / 'modes'),
                  'edid_sha256': None} for p in paths}
    for path, value in values.items():
        try:
            value['edid_sha256'] = hashlib.sha256((Path(path).parent / 'edid').read_bytes()).hexdigest()
        except OSError:
            pass
    return {'ok': bool(values) and any(v['status'] == 'connected' for v in values.values()), 'connectors': values}


def storage_target(directory, disks):
    path = Path(directory).resolve(strict=True)
    if not path.is_dir():
        raise ValueError('存储目标目录不存在')
    # 只接受内置物理盘的直接挂载；overlay/tmpfs/LVM/USB 一律阻止，避免错误落盘。
    mount = json.loads(command(['findmnt', '-J', '-T', str(path), '-o', 'SOURCE,FSTYPE,TARGET']))['filesystems'][0]
    source = mount['source'].split('[')[0]
    if not source.startswith('/dev/') or mount['fstype'] in ('tmpfs', 'overlay', 'squashfs'):
        raise ValueError('目标不是已验证的物理盘文件系统: ' + str(mount))
    name = Path(os.path.realpath(source)).name
    candidates = [d for d in disks if name == d['name'] or
                  re.fullmatch(re.escape(d['name']) + (r'p\d+' if d['name'][-1].isdigit() else r'\d+'), name)]
    if len(candidates) != 1:
        raise ValueError('目标未映射到唯一内置盘: ' + source)
    disk = candidates[0]
    if not disk['serial']:
        raise ValueError('无法读取目标盘序列号/CID，不能可靠追踪写入预算')
    identity = {key: disk[key] for key in ('serial', 'model', 'size_bytes')}
    return dict(disk, directory=str(path), mount=mount, free_bytes=shutil.disk_usage(path).free,
                identity=hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest())
