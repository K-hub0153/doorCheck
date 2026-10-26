"""
IP扫描器 - 用于主动扫描局域网内的设备

当mDNS发现不工作时，可以使用IP扫描作为补充
"""

import socket
import threading
import requests
from typing import Callable, Optional
from ..utils.logger import logger
from .device_info import DeviceInfo, DEVICE_TYPE_SMART_HORN


class IPScanner:
    """IP扫描器"""

    def __init__(self, on_device_found: Callable[[DeviceInfo], None]):
        self.on_device_found = on_device_found
        self.scanning = False
        self.timeout = 2  # HTTP请求超时时间
        self.http_port = 8080

    def get_local_subnet(self) -> Optional[str]:
        """获取本机所在的网段"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()

            parts = local_ip.split('.')
            if len(parts) == 4:
                subnet = '.'.join(parts[:3])
                logger.info(f"检测到本机IP: {local_ip}, 网段: {subnet}.0/24")
                return subnet
        except Exception as e:
            logger.error(f"获取本机网段失败: {e}")
        return None

    def scan_local_subnet(self, start: int = 1, end: int = 254):
        """扫描本机所在网段"""
        subnet = self.get_local_subnet()
        if not subnet:
            logger.error("无法获取本机网段，跳过IP扫描")
            return

        self.scan_subnet(subnet, start, end)

    def scan_subnet(self, subnet: str = "192.168.1", start: int = 1, end: int = 254):
        """扫描指定网段"""
        if self.scanning:
            logger.warning("IP扫描已在进行中")
            return

        self.scanning = True
        logger.info(f"开始扫描IP段: {subnet}.{start}-{end}")

        threads = []
        for i in range(start, end + 1):
            ip = f"{subnet}.{i}"
            thread = threading.Thread(target=self._check_device, args=(ip,), daemon=True)
            thread.start()
            threads.append(thread)

            # 每50个IP一批，避免创建太多线程
            if len(threads) >= 50:
                for t in threads:
                    t.join()
                threads = []

        # 等待剩余线程完成
        for t in threads:
            t.join()

        self.scanning = False
        logger.info("IP扫描完成")

    def _check_device(self, ip: str):
        """检查指定IP是否为设备"""
        try:
            url = f"http://{ip}:{self.http_port}/hi"
            response = requests.get(url, timeout=self.timeout)

            if response.status_code == 200 and "Hello World" in response.text:
                logger.info(f"发现设备: {ip}:{self.http_port}")
                self._query_device_info(ip)
        except:
            pass  # 忽略连接失败的IP

    def _query_device_info(self, ip: str):
        """查询设备详细信息"""
        try:
            sn = None
            fw_ver = None

            # 步骤1: 查询设备SN
            sn_url = f"http://{ip}:{self.http_port}/api/system/sn"
            try:
                sn_response = requests.get(sn_url, timeout=self.timeout)
                if sn_response.status_code == 200:
                    sn_json = sn_response.json()
                    if sn_json.get('code') == 0:
                        sn_data = sn_json.get('data', {})
                        sn = sn_data.get('sn') or sn_data.get('SN') or sn_data.get('serial_number')
                        if sn:
                            logger.info(f"[{ip}] 查询到设备SN: {sn}")
            except Exception as e:
                logger.debug(f"[{ip}] /api/system/sn 不可用: {e}")

            # 步骤2: 如果没有SN接口，尝试从MAC地址获取
            if not sn:
                mac_url = f"http://{ip}:{self.http_port}/api/system/get_mac"
                try:
                    mac_response = requests.get(mac_url, timeout=self.timeout)
                    if mac_response.status_code == 200:
                        mac_json = mac_response.json()
                        if mac_json.get('code') == 0:
                            mac_data = mac_json.get('data', {})
                            wifi_mac = (mac_data.get('wifi_mac') or
                                       mac_data.get('wlan_mac') or
                                       mac_data.get('mac') or
                                       mac_data.get('MAC'))
                            if wifi_mac and not wifi_mac.startswith('00:'):
                                sn = wifi_mac.replace(':', '').replace('-', '').lower()
                                logger.info(f"[{ip}] 使用WiFi MAC作为SN: {sn}")
                except Exception as e:
                    logger.debug(f"[{ip}] /api/system/get_mac 不可用: {e}")

            # 步骤3: 查询版本信息
            version_url = f"http://{ip}:{self.http_port}/api/system/version"
            try:
                version_response = requests.get(version_url, timeout=self.timeout)
                if version_response.status_code == 200:
                    version_json = version_response.json()
                    if version_json.get('code') == 0:
                        version_data = version_json.get('data', {})
                        fw_ver = version_data.get('app_version') or version_data.get('version')
            except Exception as e:
                logger.debug(f"[{ip}] /api/system/version 不可用: {e}")

            # 如果没有获取到SN，跳过此设备（不使用临时标识）
            if not sn:
                logger.debug(f"[{ip}] 无法获取SN，跳过此设备")
                return

            # 构建设备信息
            device = DeviceInfo(
                sn=sn,
                type=DEVICE_TYPE_SMART_HORN,  # IP扫描默认假设为音箱
                type_code=1,
                model="Speaker",
                ip=ip,
                port=self.http_port,
                fw_ver=fw_ver,
                properties={'source': 'ip_scan'}
            )

            # 步骤4: 尝试触发设备发送mDNS广播
            try:
                announce_url = f"http://{ip}:{self.http_port}/api/sle/announce"
                announce_response = requests.get(announce_url, timeout=self.timeout)
                if announce_response.status_code == 200:
                    announce_json = announce_response.json()
                    if announce_json.get('code') == 0:
                        logger.info(f"[{ip}] 已触发设备mDNS广播")
            except Exception as e:
                logger.debug(f"[{ip}] /api/sle/announce 不可用: {e}")

            logger.info(f"[{ip}] 设备信息: SN={sn}, FW={fw_ver}")
            if self.on_device_found:
                self.on_device_found(device)

        except Exception as e:
            logger.error(f"[{ip}] 查询设备信息异常: {e}")
