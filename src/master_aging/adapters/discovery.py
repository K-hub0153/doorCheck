"""老化设备扫描：分轮探测 SSH，限制身份读取并发，自动补查暂未响应的地址。"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import ipaddress
import socket
import time

import paramiko

from .ssh_transport import SSHTransport
from ..infrastructure.config import ssh_settings


class ExcludedDevice(ValueError):
    """已读取身份，但不属于允许的主控，后续轮次无需再次登录。"""


class Discovery:
    SN_PREFIXES = ('wdse', 'hisi', 'orangepi')
    PORT_TIMEOUTS = (0.8, 1.5, 2.5)
    PORT_WORKERS = 64
    SSH_WORKERS = 6

    @classmethod
    def accepts_sn(cls, sn):
        return (isinstance(sn, str) and 0 < len(sn) <= 128
                and sn.lower().startswith(cls.SN_PREFIXES)
                and not any(ord(char) < 32 or ord(char) == 127 for char in sn))

    @staticmethod
    def interfaces():
        from src.master_control.core.discovery import get_all_interfaces
        return get_all_interfaces()

    @staticmethod
    def port_open(ip, timeout, port=22):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def identify(ip, settings):
        transport = SSHTransport({'ip': ip}, settings)
        try:
            transport.connect()
            # 与产测工具一致：序列号不可读时回退 hostname；一次读取避免重复握手。
            # 标记字段避免登录提示、空行和 CRLF 被误当作身份。
            output = transport.command(['sh', '-c',
                'sn=$(cat /etc/serial-id 2>/dev/null); source=serial_id; '
                'if [ -z "$sn" ]; then sn=$(hostname 2>/dev/null); source=hostname; fi; '
                'printf "\\nAGING_SN=%s\\nAGING_SOURCE=%s\\nAGING_MODEL=%s\\n" '
                '"$sn" "$source" "$(uname -m 2>/dev/null)"'], timeout=8)
            fields = {}
            for line in output.splitlines():
                key, separator, value = line.partition('=')
                if separator and key in ('AGING_SN', 'AGING_SOURCE', 'AGING_MODEL'):
                    fields[key] = value.strip()
            sn = fields.get('AGING_SN', '')
            if 'AGING_SN' not in fields:
                return None
            if not Discovery.accepts_sn(sn):
                raise ExcludedDevice('设备 SN / 主机名不属于 wdse、hisi、orangepi 主控')
            source = fields.get('AGING_SOURCE', 'hostname')
            return {'ip': ip, 'sn': sn, 'model': fields.get('AGING_MODEL') or 'MASTER',
                    'port': 22, 'identity_source': source,
                    'discovery_note': ('身份已读取' if source == 'serial_id'
                                       else '使用主机名，需读取设备信息确认 SN')}
        except (paramiko.AuthenticationException, ExcludedDevice):
            # 认证失败不做重复登录，避免反复触发设备防护。
            raise
        except (OSError, EOFError, RuntimeError, paramiko.SSHException, ValueError):
            return None
        finally:
            transport.close()

    def scan(self, subnet=None, progress_callback=None):
        subnets = [subnet] if subnet else list(dict.fromkeys(item['subnet'] for item in self.interfaces()))
        if not subnets:
            raise ValueError('未检测到可用局域网网卡，请检查电脑网络连接')
        hosts = set()
        for value in subnets:
            network = ipaddress.IPv4Network(value, strict=False)
            if network.num_addresses > 65536:
                raise ValueError('扫描范围过大，请选择不超过 /16 的设备子网')
            hosts.update(str(ip) for ip in network.hosts())
        settings = dict(ssh_settings(), timeout=8)
        found, rejected, reachable = {}, set(), set()
        total = len(hosts) * len(self.PORT_TIMEOUTS) * 2
        completed = 0

        def advance(count=1):
            nonlocal completed
            completed += count
            if progress_callback:
                progress_callback(completed, total)

        # 两个线程池并行衔接：端口一旦开放就读取身份，不等待整个子网探测结束。
        # 端口探测可提高并发，SSH 保持较低并发，避免设备握手被挤占。
        with ThreadPoolExecutor(max_workers=self.PORT_WORKERS, thread_name_prefix='aging-port') as port_pool, \
                ThreadPoolExecutor(max_workers=self.SSH_WORKERS, thread_name_prefix='aging-identity') as ssh_pool:
            for attempt, timeout in enumerate(self.PORT_TIMEOUTS):
                pending = sorted(hosts - found.keys() - rejected, key=ipaddress.IPv4Address)
                advance((len(hosts) - len(pending)) * 2)
                if attempt and pending:
                    time.sleep(0.4 * attempt)
                futures = {}
                for ip in pending:
                    if ip in reachable:
                        # 已开放 SSH 的地址仅重试身份读取，省去重复端口连接。
                        advance()
                        futures[ssh_pool.submit(self.identify, ip, settings)] = (ip, 'identity')
                    else:
                        futures[port_pool.submit(self.port_open, ip, timeout)] = (ip, 'port')
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        ip, stage = futures.pop(future)
                        if stage == 'port':
                            if future.result():
                                reachable.add(ip)
                                futures[ssh_pool.submit(self.identify, ip, settings)] = (ip, 'identity')
                            else:
                                advance()  # 未进入身份读取阶段。
                        else:
                            try:
                                device = future.result()
                            except (paramiko.AuthenticationException, ExcludedDevice):
                                rejected.add(ip)
                            else:
                                if device:
                                    if self.accepts_sn(device.get('sn')):
                                        device['open_ports'] = [22]
                                        found[ip] = device
                                    else:
                                        rejected.add(ip)
                        advance()
        return [found[ip] for ip in sorted(found, key=ipaddress.IPv4Address)]
