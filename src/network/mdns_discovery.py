"""
统一的mDNS设备发现模块

支持设备类型过滤，可用于门控和音箱工具
"""

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf, ServiceInfo
from typing import Callable, Dict, List, Optional
import threading
import socket
import json
from .device_info import (
    DeviceInfo,
    DEVICE_TYPE_MAP,
    DEVICE_TYPE_SMART_DOOR,
    DEVICE_TYPE_SMART_HORN,
    DEVICE_TYPE_OUTDOOR_SMART_HORN
)
from ..utils.logger import logger


class DeviceDiscoveryListener(ServiceListener):
    """
    统一的mDNS设备发现监听器

    支持设备类型过滤，门控工具只接受SMART_DOOR，音箱工具只接受SMART_HORN/OUTDOOR_SMART_HORN
    """

    def __init__(
        self,
        on_device_found: Callable[[DeviceInfo], None],
        on_device_removed: Optional[Callable[[str], None]] = None,
        device_types: Optional[List[str]] = None,
        local_sn: Optional[str] = None
    ):
        """
        初始化设备发现监听器

        Args:
            on_device_found: 发现设备时的回调函数
            on_device_removed: 设备离线时的回调函数
            device_types: 要发现的设备类型列表，例如 [DEVICE_TYPE_SMART_DOOR]
                         如果为None，则接受所有设备类型
            local_sn: 本机序列号（用于排除自己）
        """
        self.on_device_found = on_device_found
        self.on_device_removed = on_device_removed
        self.device_types = device_types
        self.local_sn = local_sn
        self.discovered_devices: Dict[str, DeviceInfo] = {}
        self._lock = threading.Lock()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            self._process_service(info)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            self._process_service(info)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        with self._lock:
            if name in self.discovered_devices:
                device = self.discovered_devices.pop(name)
                logger.info(f"设备离线: {device}")
                if self.on_device_removed:
                    self.on_device_removed(device.sn)

    def refresh_all_devices(self, zc: Zeroconf, service_type: str) -> None:
        """重新扫描所有mDNS服务（不只是刷新已知设备）"""
        try:
            # 获取所有当前的mDNS服务
            from zeroconf import ServiceStateChange

            # 使用ServiceBrowser的缓存或直接查询
            # 注意：zeroconf没有直接的get_all_services API
            # 我们需要通过重新查询来发现新设备

            # 方案：重新查询所有已知设备，同时依赖ServiceBrowser的add_service自动发现新设备
            with self._lock:
                device_names = list(self.discovered_devices.keys())

            if device_names:
                logger.debug(f"刷新 {len(device_names)} 个已知设备...")
                for name in device_names:
                    info = zc.get_service_info(service_type, name, timeout=1000)
                    if info:
                        self._process_service(info)

            # ServiceBrowser会自动监听新设备的add_service事件
            # 所以新设备应该会被自动发现

        except Exception as e:
            logger.error(f"刷新设备失败: {e}")

    def _process_service(self, info: ServiceInfo) -> None:
        """
        处理mDNS��务信息

        解析设备信息并应用设备类型过滤
        """
        try:
            addresses = info.parsed_addresses()
            if not addresses:
                return

            ip = addresses[0]
            port = info.port
            properties = {k.decode('utf-8'): v.decode('utf-8') for k, v in info.properties.items()}

            # 优先从device字段获取完整信息
            device_json = properties.get('device', '')
            device_data = {}

            if device_json:
                try:
                    device_data = json.loads(device_json)
                except json.JSONDecodeError as e:
                    logger.warning(f"解析device JSON失败: {e}")

            # 提取设备序列号
            sn = device_data.get('sn', '') or properties.get('sn', '')

            # 如果仍然没有SN，尝试从服务名提取
            if not sn and info.name:
                name_parts = info.name.split('.')
                if name_parts and name_parts[0].startswith('lock-'):
                    sn = name_parts[0]

            if not sn:
                logger.debug(f"服务 {info.name} 缺少设备序列号")
                return

            # 排除本机设备
            if self.local_sn and sn == self.local_sn:
                logger.debug(f"排除本机设备: {sn}")
                return

            # 提取设备类型
            device_type = device_data.get('type', '')
            type_code = DEVICE_TYPE_MAP.get(device_type, 0)

            # 设备类型过滤
            if self.device_types and device_type not in self.device_types:
                logger.debug(f"过滤设备: type={device_type}, sn={sn} (不在允许列表中)")
                return

            # 如果没有type字段，尝试兼容旧格式
            if not device_type:
                # 旧格式没有type字段，默认认为是门控设备（兼容性）
                logger.debug(f"设备 {sn} 缺少type字段，使用兼容模式")
                device_type = DEVICE_TYPE_SMART_DOOR
                type_code = DEVICE_TYPE_MAP[device_type]

            # 提取其他信息
            model = device_data.get('model', '') or properties.get('model', '')
            hw_ver = device_data.get('hw_ver', '')
            fw_ver = device_data.get('fw_ver', '')

            # 提取地址信息
            addr = device_data.get('addr', {})
            mqtt_port = addr.get('port', 1883)
            mac = addr.get('mac', '')

            # 构建DeviceInfo
            device = DeviceInfo(
                sn=sn,
                type=device_type,
                type_code=type_code,
                ip=ip,
                port=port,
                model=model if model else 'Unknown',
                mac=mac,
                hw_ver=hw_ver,
                fw_ver=fw_ver,
                mqtt_port=mqtt_port,
                properties=properties
            )

            # 添加到已发现设备
            with self._lock:
                is_new = info.name not in self.discovered_devices
                self.discovered_devices[info.name] = device

            if is_new:
                logger.info(
                    f"发现新设备: {device.get_type_display()} - "
                    f"{device.get_display_name()} ({device.ip})"
                )
            else:
                logger.debug(f"更新设备: {device.get_display_name()} ({device.ip})")

            if self.on_device_found:
                self.on_device_found(device)

        except Exception as e:
            logger.error(f"处理设备信息失败: {e}", exc_info=True)


class MasterMdnsService:
    """主控mDNS服务（用于注册本机为主控设备）"""

    def __init__(self, zeroconf: Zeroconf, port: int = 8080):
        self.zeroconf = zeroconf
        self.port = port
        self.service_info = None

    def register(self):
        try:
            local_ip = self._get_local_ip()
            hostname = socket.gethostname()

            device_info = {"sn": "master-001", "model": "MASTER"}

            self.service_info = ServiceInfo(
                "_master._tcp.local.",
                f"master-{hostname}._master._tcp.local.",
                addresses=[socket.inet_aton(local_ip)],
                port=self.port,
                properties={
                    b'device': json.dumps(device_info, separators=(',', ':')).encode('utf-8')
                }
            )
            self.zeroconf.register_service(self.service_info)
            logger.info(f"主控mDNS服务已注册: {local_ip}:{self.port}, device: {json.dumps(device_info, separators=(',', ':'))}")
        except Exception as e:
            logger.error(f"注册主控mDNS服务失败: {e}")

    def _get_local_ip(self):
        """获取局域网IP地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return socket.gethostbyname(socket.gethostname())

    def unregister(self):
        if self.service_info:
            try:
                self.zeroconf.unregister_service(self.service_info)
                logger.info("主控mDNS服务已注销")
            except Exception as e:
                logger.error(f"注销主控mDNS服务失败: {e}")


class DebugServiceListener(ServiceListener):
    """调试用的mDNS服务监听器"""

    def __init__(self):
        self.services = {}

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        try:
            info = zc.get_service_info(type_, name, timeout=3000)
            if info:
                addresses = info.parsed_addresses()
                if info.properties:
                    try:
                        props = {}
                        for k, v in info.properties.items():
                            key = k.decode('utf-8') if k else str(k)
                            value = v.decode('utf-8') if v else str(v)
                            props[key] = value
                    except Exception as e:
                        logger.warning(f"[mDNS调试] 属性解析失败: {e}")
        except Exception as e:
            logger.warning(f"[mDNS调试] 处理服务失败 {name}: {e}")

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        logger.info(f"[mDNS调试] 移除服务: {name}")

