"""老化设备扫描：分轮探测 SSH，限制身份读取并发，自动补查暂未响应的地址。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import socket
import time

import paramiko

from .ssh_transport import SSHTransport
from ..infrastructure.config import ssh_settings


class Discovery:
    PORT_TIMEOUTS = (0.8, 1.5, 2.5)
    PORT_WORKERS = 32
    SSH_WORKERS = 6

    @staticmethod
    def interfaces():
        from src.master_control.core.discovery import get_all_interfaces
        return get_all_interfaces()

    @staticmethod
    def port_open(ip, timeout):
        try:
            with socket.create_connection((ip, 22), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def identify(ip, settings):
        transport = SSHTransport({'ip': ip}, settings)
        try:
            transport.connect()
            # 一个通道读取身份，避免逐命令握手及无关的 10001 端口扫描。
            output = transport.command(['sh', '-c',
                'sn=$(cat /etc/serial-id) || exit; printf "%s\\n" "$sn"; uname -m'], timeout=8)
            lines = output.splitlines()
            if len(lines) != 2:
                return None
            sn, model = (line.strip() for line in lines)
            if not sn.lower().startswith(('wdse-', 'hisi-')) or len(sn) > 128:
                return None
            return {'ip': ip, 'sn': sn, 'model': model, 'port': 22, 'open_ports': [22]}
        except paramiko.AuthenticationException:
            # 认证失败不做重复登录，避免反复触发设备防护。
            raise
        except (OSError, RuntimeError, paramiko.SSHException, ValueError):
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
        found, rejected = {}, set()
        total = len(hosts) * len(self.PORT_TIMEOUTS) * 2
        completed = 0

        def advance(count=1):
            nonlocal completed
            completed += count
            if progress_callback:
                progress_callback(completed, total)

        for attempt, timeout in enumerate(self.PORT_TIMEOUTS):
            pending = sorted(hosts - found.keys() - rejected, key=ipaddress.IPv4Address)
            advance((len(hosts) - len(pending)) * 2)
            if attempt and pending:
                time.sleep(0.4 * attempt)
            reachable = []
            with ThreadPoolExecutor(max_workers=self.PORT_WORKERS, thread_name_prefix='aging-port') as pool:
                futures = {pool.submit(self.port_open, ip, timeout): ip for ip in pending}
                for future in as_completed(futures):
                    if future.result():
                        reachable.append(futures[future])
                    else:
                        advance()  # 未进入身份读取阶段。
                    advance()
            with ThreadPoolExecutor(max_workers=self.SSH_WORKERS, thread_name_prefix='aging-identity') as pool:
                futures = {pool.submit(self.identify, ip, settings): ip for ip in reachable}
                for future in as_completed(futures):
                    ip = futures[future]
                    try:
                        device = future.result()
                    except paramiko.AuthenticationException:
                        rejected.add(ip)
                    else:
                        if device:
                            found[ip] = device
                    advance()
        return [found[ip] for ip in sorted(found, key=ipaddress.IPv4Address)]
