"""
统一的设备信息模型

支持智能门控、智能室内音箱、智能室外音箱三种设备类型
"""

from dataclasses import dataclass, field
from typing import Optional, Dict

# 设备类型常量
DEVICE_TYPE_SMART_HORN = "SMART_HORN"
DEVICE_TYPE_OUTDOOR_SMART_HORN = "OUTDOOR_SMART_HORN"
DEVICE_TYPE_SMART_DOOR = "SMART_DOOR"

# 设备类型映射（用于兼容性）
DEVICE_TYPE_MAP = {
    DEVICE_TYPE_SMART_HORN: 1,           # 智能室内音箱
    DEVICE_TYPE_OUTDOOR_SMART_HORN: 2,   # 智能室外音箱
    DEVICE_TYPE_SMART_DOOR: 3,           # 智能门控
}

# 设备类型显示名称
DEVICE_TYPE_DISPLAY = {
    DEVICE_TYPE_SMART_HORN: "智能室内音箱",
    DEVICE_TYPE_OUTDOOR_SMART_HORN: "智能室外音箱",
    DEVICE_TYPE_SMART_DOOR: "智能门控",
}

# 设备类型对应的 Product ID（MQTT通信使用）
PRODUCT_ID_MAP = {
    DEVICE_TYPE_SMART_HORN: "1700",           # 智能室内音箱
    DEVICE_TYPE_OUTDOOR_SMART_HORN: "1699",   # 智能室外音箱
    DEVICE_TYPE_SMART_DOOR: "1696",           # 智能门控
}


@dataclass
class DeviceInfo:
    """统一的设备信息模型"""

    # 基本信息
    sn: str                              # 设备序列号
    type: str                            # 设备类型: SMART_HORN/OUTDOOR_SMART_HORN/SMART_DOOR
    type_code: int                       # 设备类型代码: 1/2/3
    ip: str                              # IP地址
    port: int = 8080                     # HTTP端口

    # 可选信息
    model: Optional[str] = None          # 设备型号
    mac: Optional[str] = None            # MAC地址
    hw_ver: Optional[str] = None         # 硬件版本
    fw_ver: Optional[str] = None         # 固件版本
    mqtt_port: int = 1883                # MQTT端口
    product_id: Optional[str] = None     # 设备实际使用的Product ID（从HTTP配置请求获取）
    properties: Dict = field(default_factory=dict)  # 原始属性字典

    # 门控特有字段
    mqtt_connected: bool = False         # MQTT连接状态
    mqtt_ip: Optional[str] = None        # MQTT服务器IP

    def get_type_display(self) -> str:
        """获取设备类型显示名称"""
        return DEVICE_TYPE_DISPLAY.get(self.type, "未知设备")

    def get_product_id(self) -> str:
        """获取设备的 Product ID

        优先使用设备实际配置的 product_id（从HTTP配置请求获取），
        如果没有则根据设备类型映射
        """
        if self.product_id:
            return self.product_id
        return PRODUCT_ID_MAP.get(self.type, "1696")

    def get_display_name(self) -> str:
        """获取设备显示名称"""
        return self.sn if self.sn else "Unknown"

    def is_door_device(self) -> bool:
        """判断是否为门控设备"""
        return self.type == DEVICE_TYPE_SMART_DOOR

    def is_speaker_device(self) -> bool:
        """判断是否为音箱设备"""
        return self.type in [DEVICE_TYPE_SMART_HORN, DEVICE_TYPE_OUTDOOR_SMART_HORN]

    def is_indoor_speaker(self) -> bool:
        """判断是否为室内音箱"""
        return self.type == DEVICE_TYPE_SMART_HORN

    def is_outdoor_speaker(self) -> bool:
        """判断是否为室外音箱"""
        return self.type == DEVICE_TYPE_OUTDOOR_SMART_HORN

    def __str__(self) -> str:
        """字符串表示"""
        return f"Device(sn={self.sn}, type={self.get_type_display()}, ip={self.ip}, port={self.port})"

    def __repr__(self) -> str:
        """调试表示"""
        return self.__str__()
