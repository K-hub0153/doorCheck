"""4G 老化监测：AT 模组识别、信号解析和阈值判定。"""
import fcntl
import os
import select
import termios
import time
from pathlib import Path

from ..domain.diagnostics import lte_scope, lte_thresholds, monitor_reason


def at(fd, text):
    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, (text + '\r').encode('ascii'))
    end, data = time.monotonic() + 2, b''
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], min(0.2, max(0, end - time.monotonic())))
        if ready:
            data += os.read(fd, 4096)
            if b'\r\nOK' in data or b'ERROR' in data:
                break
    return data.decode('utf-8', errors='replace')


def read_at(port, text):
    port = os.path.realpath(port)
    for process in Path('/proc').glob('[0-9]*'):
        if process.name == str(os.getpid()):
            continue
        for descriptor in (process / 'fd').glob('*'):
            try:
                if os.path.realpath(str(descriptor)) == port:
                    raise RuntimeError('诊断 AT 口正被 PID %s 使用，不抢占现有服务' % process.name)
            except OSError:
                continue
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    original = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0], attrs[1], attrs[3] = 0, 0, 0
        attrs[2] = termios.B115200 | termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return at(fd, text)
    finally:
        try:
            if original is not None:
                termios.tcsetattr(fd, termios.TCSANOW, original)
        finally:
            os.close(fd)


def sample(settings):
    # 纯解析模块在部署时随 zipapp 发布，PC 运行时使用同一份源文件。
    try:
        from lte_decode import (DEFAULT_AT_PORT, load_modules, identify_module, decode)
    except ImportError:
        from src.master_control.core.lte_signal import (
            DEFAULT_AT_PORT, load_modules, identify_module, decode)
    override = settings.get('at_port') or None
    modules = load_modules(at_port=override)
    raw = {'ati_port': override or DEFAULT_AT_PORT, 'ati': '', 'attempts': []}
    limits = lte_thresholds(settings)
    result = {'ok': False, 'raw': raw, 'signal': {}, 'thresholds': limits,
              'scope': lte_scope(settings)}
    try:
        raw['ati'] = read_at(override or DEFAULT_AT_PORT, 'ATI')
    except (OSError, RuntimeError, termios.error) as exc:
        raw['ati_error'] = str(exc)
    spec, model = identify_module(raw['ati'], modules)

    def read_signal(candidate):
        attempt = {'module': candidate.name, 'at_port': candidate.at_port,
                   'command': candidate.at_command}
        raw['attempts'].append(attempt)
        try:
            attempt['signal'] = read_at(candidate.at_port, candidate.at_command)
        except (OSError, RuntimeError, termios.error) as exc:
            attempt['error'] = str(exc)
        return decode(candidate, attempt.get('signal', ''))

    if spec is not None:
        decoded = read_signal(spec)
        note = 'ATI 型号识别'
    else:
        # ATI 未识别时，按各厂商端口和响应格式依次试读。
        for candidate in modules:
            decoded = read_signal(candidate)
            if decoded.ok:
                spec = candidate
                break
            raw['attempts'][-1]['reason'] = decoded.reason
        if spec is None:
            reasons = ['%s: %s' % (a['module'], a.get('error') or a['reason'])
                       for a in raw['attempts']]
            result['reason'] = '模组厂商识别失败（' + '; '.join(reasons) + '）'
            return result
        note = 'ATI 未识别，按响应格式判定'
    values = {m.key: m.value for m in decoded.metrics}
    missing = [key.upper() for key in limits if values.get(key) is None]
    checks = {key: values[key] >= limit for key, limit in limits.items()} if not missing else {}
    signal_ok = not missing and all(checks.values())
    reason = '缺少判定字段: ' + ', '.join(missing) if missing else ''
    result.update(ok=decoded.ok and signal_ok, module=spec.name, model=model,
                  at_port=spec.at_port, identify_note=note, checks=checks,
                  signal=values,
                  reason=raw['attempts'][-1].get('error') or decoded.reason or reason or '')
    if not result['ok']:
        result['reason'] = monitor_reason('lte', result)
    raw['signal'] = raw['attempts'][-1].get('signal', '')
    return result
