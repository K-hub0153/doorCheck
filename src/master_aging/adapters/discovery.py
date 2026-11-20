"""复用子网扫描协议；输出独立字典，不构造主控产测窗口。"""
from .ssh_transport import SSHTransport
from ..infrastructure.config import ssh_settings


class Discovery:
    def __init__(self):
        from src.master_control.core.discovery import DeviceDiscovery
        settings = dict(ssh_settings(), timeout=3)

        class BoundedDiscovery(DeviceDiscovery):
            def get_device_info_via_ssh(self, ip):
                transport = SSHTransport({'ip': ip}, settings)
                try:
                    transport.connect()
                    sn = transport.command(['cat', '/etc/serial-id'], timeout=3).strip()
                    if not sn:
                        return None
                    return {'ip': ip, 'sn': sn, 'port': 22,
                            'model': transport.command(['uname', '-m'], timeout=3).strip()}
                except Exception:
                    return None
                finally:
                    transport.close()

        self._source = BoundedDiscovery()

    @staticmethod
    def interfaces():
        from src.master_control.core.discovery import get_all_interfaces
        return get_all_interfaces()

    def scan(self, subnet=None, progress_callback=None):
        devices = self._source.scan_subnet(subnet, progress_callback=progress_callback)
        return [dict(device, port=22) for device in devices
                if device.get('sn') and not str(device['sn']).startswith('device-')
                and (not device.get('open_ports') or 22 in device['open_ports'])]
