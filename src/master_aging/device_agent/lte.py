"""独占诊断串口，原始 AT 证据及绑定蜂窝接口的有限 ICMP 流量。"""
import fcntl
import os
import re
import select
import termios
import time
from pathlib import Path
from .collectors import command, read


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


def sample(settings):
    # 纯解析模块在部署时随 zipapp 发布，PC 运行时使用同一份源文件。
    try:
        from lte_decode import load_modules, identify_module, decode, judge
    except ImportError:
        from src.master_control.core.lte_signal import load_modules, identify_module, decode, judge
    port = os.path.realpath(settings['at_port'])
    for process in Path('/proc').glob('[0-9]*'):
        if process.name == str(os.getpid()):
            continue
        for descriptor in (process / 'fd').glob('*'):
            try:
                if os.path.realpath(str(descriptor)) == port:
                    raise RuntimeError('诊断 AT 口正被 PID %s 使用，不抢占现有服务' % process.name)
            except OSError:
                continue
    fd = os.open(settings['at_port'], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
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
        raw = {'ati': at(fd, 'ATI'), 'sim': at(fd, 'AT+CPIN?'), 'registration': at(fd, 'AT+CEREG?')}
        spec, _ = identify_module(raw['ati'], load_modules())
        if not spec:
            return {'ok': False, 'reason': '未知模组', 'raw': raw}
        raw['signal'] = at(fd, spec.at_command)
        decoded = decode(spec, raw['signal'])
        signal_ok, _, reason = judge(decoded.metrics)
        interface = settings['data_interface']
        routes, probes = {}, {}
        for target in settings['probe_targets']:
            routes[target] = command(['ip', '-4', 'route', 'get', target, 'oif', interface])
            if not re.search(r'\bdev\s+' + re.escape(interface) + r'\b', routes[target]):
                raise RuntimeError('蜂窝路由未验证')
            try:
                probes[target] = {'ok': True, 'raw': command(['ping', '-n', '-I', interface, '-c', '1', '-W', '1', '-s', '16', target], 2)}
            except Exception as exc:
                probes[target] = {'ok': False, 'raw': str(exc)}
        registered = bool(re.search(r'\+CEREG:\s*\d+,\s*[15](?:\s|,|$)', raw['registration']))
        sim = 'READY' in raw['sim']
        return {'ok': decoded.ok and signal_ok and registered and sim and all(v['ok'] for v in probes.values()),
                'raw': raw, 'signal': {m.key: m.value for m in decoded.metrics}, 'reason': reason,
                'registered': registered, 'sim_ready': sim, 'routes': routes, 'probes': probes,
                'rx_bytes': read('/sys/class/net/%s/statistics/rx_bytes' % interface),
                'tx_bytes': read('/sys/class/net/%s/statistics/tx_bytes' % interface),
                'scope': 'AT/SIM/注册/信号/绑定蜂窝的ICMP；不包含DNS、TCP或HTTPS吞吐判定'}
    finally:
        if original is not None:
            termios.tcsetattr(fd, termios.TCSANOW, original)
        os.close(fd)
