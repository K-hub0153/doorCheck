import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

import psutil
from PyQt5.QtCore import QThread, pyqtSignal
from zeroconf import IPVersion, ServiceBrowser, Zeroconf

from .device_info import (DEVICE_TYPE_OUTDOOR_SMART_HORN, DEVICE_TYPE_SMART_HORN,
                          DeviceInfo)
from .mdns_discovery import DeviceDiscoveryListener
from .speaker_sn_dhcp import WindowsEthernetProvisioner, acquire_network_lock
from ..utils.logger import logger
from ..utils.paths import get_app_dir


SPEAKER_TYPES = {
    DEVICE_TYPE_SMART_HORN: '室内音箱',
    DEVICE_TYPE_OUTDOOR_SMART_HORN: '室外音箱',
}


def compare_serial_numbers(device_sn, body_sn, box_sn):
    values = tuple(value.strip() if isinstance(value, str) else ''
                   for value in (device_sn, body_sn, box_sn))
    return bool(values[0]) and values[0] == values[1] == values[2]


def network_interfaces():
    addresses = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    interfaces = {}
    for name, entries in addresses.items():
        status = stats.get(name)
        ipv4 = []
        for entry in entries:
            if entry.family != socket.AF_INET or not entry.netmask:
                continue
            address = ipaddress.IPv4Address(entry.address)
            if address.is_loopback or address.is_unspecified:
                continue
            ipv4.append(str(ipaddress.IPv4Interface(f'{entry.address}/{entry.netmask}')))
        if status and 'loopback' not in status.flags and (
                any(entry.family == psutil.AF_LINK for entry in entries) or ipv4):
            interfaces[name] = (status.isup, tuple(sorted(ipv4)))
    return interfaces


@lru_cache(maxsize=2)
def physical_ethernet_names(refresh_bucket):
    system = platform.system()
    try:
        if system == 'Windows':
            command = ("$ErrorActionPreference='Stop'; "
                       "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                       "@(Get-NetAdapter -Physical | Where-Object {"
                       "$_.Status -eq 'Up' -and -not $_.Virtual -and "
                       "($_.NdisPhysicalMedium -eq 14 -or "
                       "($_.NdisPhysicalMedium -eq 0 -and $_.InterfaceType -eq 6))} "
                       "| Select-Object Name,InterfaceIndex) | ConvertTo-Json -Compress")
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                capture_output=True, text=True, encoding='utf-8-sig', timeout=3, check=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            adapters = json.loads(result.stdout.strip() or '[]')
            if isinstance(adapters, dict):
                adapters = [adapters]
            return {adapter['Name']: int(adapter['InterfaceIndex']) for adapter in adapters
                    if int(adapter.get('InterfaceIndex', 0)) > 0}
        if system == 'Darwin':
            result = subprocess.run(['/usr/sbin/networksetup', '-listallhardwareports'],
                                    capture_output=True, text=True, timeout=3, check=True)
            names = {}
            for block in result.stdout.split('\n\n'):
                fields = dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line)
                hardware = fields.get('Hardware Port', '').lower()
                if ('ethernet' in hardware or re.search(r'\blan\b', hardware)) and fields.get('Device'):
                    names[fields['Device']] = None
            return names
        if system == 'Linux':
            return {name: None for name in os.listdir('/sys/class/net')
                    if os.path.exists(f'/sys/class/net/{name}/device')
                    and not os.path.exists(f'/sys/class/net/{name}/wireless')
                    and _linux_interface_type(name) == '1'}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        logger.warning(f'读取物理以太网列表失败：{error}')
        return {}
    return {}


def _linux_interface_type(name):
    try:
        with open(f'/sys/class/net/{name}/type', encoding='ascii') as interface_type:
            return interface_type.read().strip()
    except OSError:
        return ''


def select_ethernet_interface(interfaces):
    ethernet_names = physical_ethernet_names(int(time.monotonic() // 10))
    candidates = [(name, details) for name, details in interfaces.items()
                  if name in ethernet_names and details[0]]
    if len(candidates) != 1:
        return None
    candidates.sort(key=lambda item: (not bool(item[1][1]), item[0].lower()))
    return candidates[0][0]


@dataclass(frozen=True)
class SpeakerIdentity:
    sn: str
    device_type: str
    ip: str


class SpeakerConnectionWorker(QThread):
    state_changed = pyqtSignal(object)

    def __init__(self, service_type, allow_direct_dhcp=False, parent=None):
        super().__init__(parent)
        self.service_type = service_type
        self.allow_direct_dhcp = allow_direct_dhcp
        self.stop_event = threading.Event()
        self.cleanup_error = None
        self.active_provisioner = None

    def stop(self):
        self.stop_event.set()

    def run(self):
        zeroconf = None
        browser = None
        devices = {}
        lock = threading.Lock()
        epoch = 0
        active_interface = None
        active_addresses = ()
        next_refresh = 0
        provisioner = None
        network_lock = None

        if platform.system() == 'Windows':
            try:
                network_lock = acquire_network_lock(get_app_dir())
                WindowsEthernetProvisioner.recover(get_app_dir())
            except Exception as error:
                self.cleanup_error = str(error)
                self.state_changed.emit(((), f'上次 RJ45 网络配置恢复失败：{error}',
                                         epoch, None, ()))
                if network_lock:
                    network_lock.close()
                return

        def device_found(device: DeviceInfo):
            with lock:
                devices[device.ip] = SpeakerIdentity(device.sn, device.type, device.ip)

        def device_removed(sn):
            with lock:
                for ip in [ip for ip, device in devices.items() if device.sn == sn]:
                    devices.pop(ip, None)

        try:
            while not self.stop_event.is_set():
                interfaces = network_interfaces()
                selected = select_ethernet_interface(interfaces)
                is_up, addresses = interfaces.get(selected, (False, ()))
                addresses = addresses if is_up else ()

                if provisioner and selected != provisioner.interface_name:
                    provisioner.restore()
                    provisioner = None
                    self.active_provisioner = None

                if provisioner and provisioner.server and provisioner.server.error:
                    raise RuntimeError(f'DHCP 服务已停止：{provisioner.server.error}')

                if (selected and platform.system() == 'Windows' and not self.stop_event.is_set()
                        and not any(not ipaddress.IPv4Interface(value).ip.is_link_local
                                    for value in addresses)):
                    if provisioner is None and self.allow_direct_dhcp:
                        ethernet = physical_ethernet_names(int(time.monotonic() // 10))
                        provisioner = WindowsEthernetProvisioner(
                            selected, ethernet[selected], get_app_dir())
                        self.active_provisioner = provisioner
                        all_addresses = tuple(
                            address for interface_name, (_, values) in interfaces.items()
                            if interface_name != selected for address in values)
                        try:
                            self.state_changed.emit(((),
                                f'正在为专用 RJ45 配置临时 DHCP 服务；Windows 可能弹出管理员确认。',
                                epoch, selected, addresses))
                            provisioner.start(addresses, all_addresses)
                            interfaces = network_interfaces()
                            is_up, addresses = interfaces.get(selected, (False, ()))
                            addresses = addresses if is_up else ()
                        except Exception as error:
                            self.state_changed.emit(((), f'以太网 DHCP 配置失败：{error}',
                                                     epoch, selected, addresses))
                            break
                    if not addresses and not self.allow_direct_dhcp:
                        message = 'RJ45 尚无可用 IPv4；请允许直连 DHCP 配置，或预先配置网络。'
                        self.state_changed.emit(((), message, epoch, selected, addresses))
                        self.stop_event.wait(1)
                        continue
                if (selected, addresses) != (active_interface, active_addresses):
                    if browser is not None:
                        browser.cancel()
                        browser = None
                    if zeroconf is not None:
                        zeroconf.close()
                        zeroconf = None
                    with lock:
                        devices.clear()
                    active_interface, active_addresses = selected, addresses
                    epoch += 1
                    if addresses:
                        local_ips = [str(ipaddress.IPv4Interface(address).ip)
                                     for address in addresses]
                        zeroconf = Zeroconf(interfaces=local_ips, ip_version=IPVersion.V4Only)
                        listener = DeviceDiscoveryListener(
                            on_device_found=device_found,
                            on_device_removed=device_removed,
                            device_types=[DEVICE_TYPE_SMART_HORN,
                                          DEVICE_TYPE_OUTDOOR_SMART_HORN],
                        )
                        browser = ServiceBrowser(zeroconf, self.service_type, listener)
                        next_refresh = time.monotonic() + 5

                with lock:
                    current_devices = tuple(devices.values())
                if not selected:
                    message = ('请只保留一块已连接的物理以太网，并将 RJ45 直连音箱；'
                               '未连接或同时有多块物理以太网时不启动 DHCP。')
                elif not addresses:
                    message = (f'{selected} 已连接但没有 IPv4 地址；音箱和电脑之间需要 DHCP '
                               '服务器或预先配置的同网段地址。')
                elif not current_devices:
                    message = (f'仅在以太网 {selected} 监听音箱广播，等待设备识别。'
                               '请确认 RJ45 链路及 DHCP 地址分配正常。')
                elif len(current_devices) > 1:
                    message = '以太网上发现多台音箱，为防止串台已停止比对。'
                else:
                    message = '音箱已识别，可以扫描机身 SN 和彩盒贴纸 SN。'

                self.state_changed.emit((current_devices, message, epoch, selected, addresses))
                if (zeroconf is not None and browser is not None
                        and time.monotonic() >= next_refresh):
                    listener.refresh_all_devices(zeroconf, self.service_type)
                    next_refresh = time.monotonic() + 5
                self.stop_event.wait(1)
        except Exception as error:
            logger.exception(f'音箱 SN 比对发现异常：{error}')
            self.state_changed.emit(((), f'mDNS 发现失败：{error}', epoch,
                                     active_interface, active_addresses))
        finally:
            if browser is not None:
                browser.cancel()
            if zeroconf is not None:
                zeroconf.close()
            if provisioner is not None:
                try:
                    provisioner.restore()
                except Exception as error:
                    self.cleanup_error = str(error)
                    logger.exception(f'恢复 RJ45 网络配置失败：{error}')
            if network_lock:
                network_lock.close()
