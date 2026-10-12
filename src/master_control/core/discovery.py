import socket
import time
from typing import List, Callable, Optional
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf, ServiceInfo
from src.master_control.core.device import MasterDevice
from src.master_control.utils.logger import logger
from src.master_control.utils.config import config
import threading
import ipaddress
import psutil
import concurrent.futures
from PyQt5.QtCore import QObject, pyqtSignal, QTimer

# 无线/虚拟/回环等需要排除的网卡关键词
_EXCLUDED_IFACE_KEYWORDS = ['wlan', 'wi-fi', 'wireless', 'bluetooth', '蓝牙',
                            'loopback', 'vmware', 'virtualbox', 'vethernet',
                            'docker', 'vbox', 'hyper-v', '本地连接*']

# 仅排除虚拟/回环网卡，保留 WiFi
_EXCLUDED_VIRTUAL_KEYWORDS = ['loopback', 'vmware', 'virtualbox', 'vethernet',
                               'docker', 'vbox', 'hyper-v', 'bluetooth', '蓝牙']


def _is_wired_interface(name: str) -> bool:
    """判断是否为有线网卡"""
    lower = name.lower()
    for kw in _EXCLUDED_IFACE_KEYWORDS:
        if kw in lower:
            return False
    return True


def _is_real_interface(name: str) -> bool:
    """判断是否为真实网卡（有线或无线，排除虚拟/回环）"""
    lower = name.lower()
    for kw in _EXCLUDED_VIRTUAL_KEYWORDS:
        if kw in lower:
            return False
    return True


def get_all_interfaces() -> List[dict]:
    """返回所有已连接的真实网卡（含有线和WiFi）及其子网信息"""
    interfaces = []
    try:
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if not _is_real_interface(name):
                continue
            if not (stats.get(name) and stats[name].isup):
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip, netmask = addr.address, addr.netmask
                    if ip and netmask and not ip.startswith('127.') and not ip.startswith('169.254.'):
                        try:
                            network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
                            interfaces.append({'name': name, 'ip': ip, 'subnet': str(network)})
                        except ValueError:
                            pass
    except Exception as e:
        logger.error(f"获取网卡列表失败: {e}")
    return interfaces


def get_local_subnets() -> List[str]:
    """自动识别本机有线网卡的局域网子网"""
    subnets = []
    try:
        for name, addrs in psutil.net_if_addrs().items():
            if not _is_wired_interface(name):
                logger.debug(f"跳过非有线网卡: {name}")
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    netmask = addr.netmask
                    if ip and netmask and not ip.startswith('127.') and not ip.startswith('169.254.'):
                        try:
                            network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
                            subnets.append(str(network))
                            logger.info(f"识别到有线网卡 [{name}]: {ip}/{netmask} -> {network}")
                        except ValueError:
                            pass
    except Exception as e:
        logger.error(f"获取本地子网失败: {e}")
    return subnets


class DeviceDiscovery(QObject):
    
    device_discovered = pyqtSignal(object)
    
    def __init__(self):
        super().__init__()
        self.zeroconf = None
        self.browser = None
        self.devices = {}
        self.device_callbacks = []
        self.running = False
        self._discovery_lock = threading.Lock()
        
    def add_device_callback(self, callback: Callable[[MasterDevice], None]):
        self.device_callbacks.append(callback)
    
    def start_mdns_discovery(self):
        logger.info("启动 mDNS 设备发现...")
        with self._discovery_lock:
            if self.running:
                return
            self.running = True
            
        try:
            self.zeroconf = Zeroconf()
            listener = MasterDeviceListener(self)
            service_type = config.mdns_service_type
            self.browser = ServiceBrowser(self.zeroconf, service_type, listener)
            logger.info(f"mDNS 监听服务类型: {service_type}")
        except Exception as e:
            logger.error(f"启动 mDNS 发现失败: {e}")
            self.running = False
    
    def restart_mdns_discovery(self):
        """重启 mDNS 发现，刷新 ServiceBrowser 缓存"""
        self.stop_mdns_discovery()
        self.start_mdns_discovery()

    def stop_mdns_discovery(self):
        logger.info("停止 mDNS 设备发现")
        with self._discovery_lock:
            if not self.running:
                return
            self.running = False
            
        try:
            if self.browser:
                self.browser.cancel()
                self.browser = None
            if self.zeroconf:
                self.zeroconf.close()
                self.zeroconf = None
        except Exception as e:
            logger.error(f"停止 mDNS 发现失败: {e}")
    
    def get_device_info_via_ssh(self, ip: str) -> Optional[dict]:
        try:
            import paramiko
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                ip,
                username=config.ssh_username,
                password=config.ssh_password,
                timeout=3,
                allow_agent=False,
                look_for_keys=False
            )

            stdin, stdout, stderr = ssh.exec_command("cat /etc/serial-id")
            sn = stdout.read().decode().strip()

            if not sn:
                stdin, stdout, stderr = ssh.exec_command("hostname")
                sn = stdout.read().decode().strip()

            stdin, stdout, stderr = ssh.exec_command("uname -m")
            model = stdout.read().decode().strip()

            # 优化硬盘检测：使用 --pairs 输出格式，避免列宽/空格问题
            stdin, stdout, stderr = ssh.exec_command(
                "lsblk -d -n -P -o NAME,MODEL,SIZE | grep -E 'NAME=\"(sd|nvme|mmcblk)' | head -1"
            )
            disk_info = stdout.read().decode().strip()
            disk_model = ""
            disk_size = ""
            if disk_info:
                # 解析 KEY="VALUE" 格式
                import re
                model_match = re.search(r'MODEL="([^"]*)"', disk_info)
                size_match = re.search(r'SIZE="([^"]+)"', disk_info)
                name_match = re.search(r'NAME="([^"]+)"', disk_info)
                
                disk_model = model_match.group(1).strip() if model_match and model_match.group(1).strip() else ''
                disk_size = size_match.group(1) if size_match else ''
                
                # 如果 MODEL 为空，使用设备名
                if not disk_model and name_match:
                    disk_model = name_match.group(1)

            ssh.close()

            if sn:
                return {
                    'ip': ip,
                    'sn': sn if sn else f"device-{ip.replace('.', '-')}",
                    'model': model if model else 'MASTER',
                    'port': 22,
                    'disk_model': disk_model,
                    'disk_size': disk_size
                }
        except Exception as e:
            logger.debug(f"SSH连接 {ip} 失败: {e}")

        return None

    def check_port_open(self, ip: str, port: int, timeout: float = 0.5) -> bool:
        """检查指定端口是否开放"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, port))
            sock.close()
            # Windows: 10035=WSAEWOULDBLOCK(正在连接), 0=成功
            # Linux: 115=EINPROGRESS(正在连接), 0=成功
            # 只有明确的连接成功(0)才返回True，超时/拒绝都返回False
            return result == 0
        except socket.timeout:
            return False
        except Exception:
            return False
    
    def scan_subnet(self, subnet: str = None, progress_callback: Optional[Callable[[int, int], None]] = None) -> List[dict]:
        # 自动识别本地子网
        if subnet:
            subnets = [subnet]
        else:
            subnets = get_local_subnets()
            if not subnets:
                logger.warning("未检测到本地子网，使用默认子网")
                subnets = [config.scan_subnet]

        logger.info(f"扫描子网列表: {subnets}")
        discovered_devices = []
        lock = threading.Lock()

        # 计算所有子网的总IP数
        all_hosts = []
        for s in subnets:
            network = ipaddress.IPv4Network(s, strict=False)
            for ip in network.hosts():
                all_hosts.append(str(ip))

        total_ips = len(all_hosts)
        scanned = 0

        scan_ports = [22,10001]

        def scan_host(ip_str):
            nonlocal scanned

            try:
                open_ports = []
                for port in scan_ports:
                    if self.check_port_open(ip_str, port, 0.3):
                        open_ports.append(port)

                if open_ports:
                    logger.info(f"发现主机: {ip_str}, 开放端口: {open_ports}")

                    device_info = None
                    if 22 in open_ports:
                        device_info = self.get_device_info_via_ssh(ip_str)

                    if device_info:
                        device_info['open_ports'] = open_ports
                        with lock:
                            discovered_devices.append(device_info)
                        logger.info(f"获取到设备信息: {device_info['sn']} ({ip_str})")
                    else:
                        with lock:
                            discovered_devices.append({
                                'ip': ip_str,
                                'sn': f"device-{ip_str.replace('.', '-')}",
                                'model': 'UNKNOWN',
                                'port': open_ports[0],
                                'open_ports': open_ports
                            })
                        logger.info(f"添加未知设备: {ip_str}")

            except Exception as e:
                logger.debug(f"扫描 {ip_str} 异常: {e}")
            finally:
                with lock:
                    scanned += 1
                    if progress_callback:
                        progress_callback(scanned, total_ips)

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            futures = []
            for ip_str in all_hosts:
                futures.append(executor.submit(scan_host, ip_str))

            concurrent.futures.wait(futures)

        logger.info(f"子网扫描完成，发现 {len(discovered_devices)} 台设备")
        return discovered_devices
    
    def add_device_safely(self, device: MasterDevice):
        """线程安全的设备添加方法"""
        with self._discovery_lock:
            is_new = device.sn not in self.devices

            if is_new:
                self.devices[device.sn] = device
                logger.info(f"添加设备: {device.sn} ({device.ip})")
            else:
                self.devices[device.sn].last_seen = device.last_seen
                self.devices[device.sn].ip = device.ip
                logger.info(f"更新设备: {device.sn} ({device.ip})")

            logger.info(f"准备发射设备发现信号: {device.sn}")
            self.device_discovered.emit(device)
            logger.info(f"设备发现信号已发射: {device.sn}")

            for callback in self.device_callbacks:
                try:
                    callback(device)
                except Exception as e:
                    logger.error(f"设备回调异常: {e}")
    
    def add_device(self, device: MasterDevice):
        """保持兼容性的设备添加方法"""
        self.add_device_safely(device)
    
    def remove_device(self, sn: str):
        with self._discovery_lock:
            if sn in self.devices:
                logger.info(f"移除设备: {sn}")
                del self.devices[sn]

    def clear_devices(self):
        """清空已发现的设备缓存（重新刷新发现/子网扫描前调用）"""
        with self._discovery_lock:
            count = len(self.devices)
            self.devices.clear()
        logger.info(f"已清空设备缓存，原有 {count} 台设备")
    
    def get_devices(self) -> List[MasterDevice]:
        with self._discovery_lock:
            return list(self.devices.values())


class MasterDeviceListener(ServiceListener):
    
    def __init__(self, discovery: DeviceDiscovery):
        self.discovery = discovery
    
    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        logger.info(f"mDNS 服务添加: {name}")
        # 主动获取服务信息
        info = zc.get_service_info(type_, name)
        if info:
            self.service_resolved(zc, type_, name, info)
        else:
            logger.warning(f"无法获取服务信息: {name}")
    
    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        logger.info(f"mDNS 服务移除: {name}")
        sn = name.split('.')[0]
        self.discovery.remove_device(sn)
    
    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        logger.info(f"mDNS 服务更新: {name}")
        # 主动获取更新的服务信息
        info = zc.get_service_info(type_, name)
        if info:
            self.service_resolved(zc, type_, name, info)
    
    def service_state_changed(self, zc: Zeroconf, type_: str, name: str, state_change) -> None:
        pass
    
    def service_resolved(self, zc: Zeroconf, type_: str, name: str, info: ServiceInfo) -> None:
        logger.info(f"mDNS 服务解析成功: {name}")
        try:
            if info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port if info.port else 22

                # 尝试通过 SSH 获取完整设备信息（包括硬盘）
                device_info = self.discovery.get_device_info_via_ssh(ip)

                if device_info:
                    # SSH 获取成功，使用完整信息
                    device = MasterDevice(
                        ip=device_info['ip'],
                        sn=device_info['sn'],
                        model=device_info['model'],
                        port=device_info['port'],
                        disk_model=device_info.get('disk_model', ''),
                        disk_size=device_info.get('disk_size', '')
                    )
                else:
                    # SSH 失败，使用 mDNS 信息
                    properties = {}
                    if info.properties:
                        for key, value in info.properties.items():
                            try:
                                properties[key.decode('utf-8')] = value.decode('utf-8')
                            except Exception:
                                pass

                    device_info_str = properties.get('device', '{}')
                    sn = None
                    model = 'MASTER'

                    try:
                        import json
                        device_data = json.loads(device_info_str)
                        sn = device_data.get('sn')
                        model = device_data.get('model', 'MASTER')
                    except:
                        pass

                    if not sn:
                        sn = properties.get('sn', name.split('.')[0])

                    device = MasterDevice(
                        ip=ip,
                        sn=sn,
                        model=model,
                        port=port,
                        disk_model='',
                        disk_size=''
                    )

                logger.info(f"解析设备信息: IP={ip}, SN={device.sn}, 硬盘={device.disk_model} {device.disk_size}")
                self.discovery.add_device_safely(device)
                logger.info(f"设备已添加到列表: {device.sn} ({ip})")
        except Exception as e:
            logger.error(f"解析设备信息失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
