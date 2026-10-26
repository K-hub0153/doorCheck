"""
统一的协议消息模块

支持门控和音箱的协议消息构建
"""

import json
import time
from typing import Dict, Any, Optional
from .crypto import generate_nonce, generate_message_id, calculate_hmac_signature, build_sign_data, body_to_sign_json


class Message:
    """
    基础消息类

    支持门控和音箱两种模式:
    - 门控模式: ts使用毫秒，不包含device字段
    - 音箱模式: ts使用秒，包含device字段
    """

    def __init__(self, action: str, body: Dict[str, Any], psk: str,
                 device_info: Optional[Dict] = None, use_milliseconds: bool = True):
        """
        初始化消息

        Args:
            action: 动作类型
            body: 消息体
            psk: 预共享密钥
            device_info: 设备信息（音箱模式需要）
            use_milliseconds: True=使用毫秒(门控), False=使用秒(音箱)
        """
        self.ver = "1.0"
        self.mid = generate_message_id()
        self.ts = int(time.time() * 1000) if use_milliseconds else int(time.time())
        self.nonce = generate_nonce()
        self.type = "req"
        self.action = action
        self.body = body
        self.device = device_info or {}
        self.psk = psk
        self.use_milliseconds = use_milliseconds
        self.sig = self._generate_signature()

    def _generate_signature(self) -> str:
        body_json = body_to_sign_json(self.body)
        sign_data = build_sign_data(
            self.ver, self.mid, self.ts, self.action, body_json, self.nonce, self.psk
        )
        return calculate_hmac_signature(sign_data, self.psk)

    def to_dict(self) -> Dict[str, Any]:
        header = {
            "ver": self.ver,
            "mid": self.mid,
            "ts": self.ts,
            "nonce": self.nonce,
            "type": self.type,
            "action": self.action,
            "sig": self.sig
        }

        # 音箱模式添加device字段
        if self.device:
            header["device"] = self.device

        return {
            "header": header,
            "body": self.body
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ==================== 门控消息（使用毫秒） ====================

class OpenDoorMessage(Message):
    """开门消息"""
    def __init__(self, psk: str, duration: int = 5000):
        body = {"duration":duration}
        super().__init__("open", body, psk, use_milliseconds=True)


class CloseDoorMessage(Message):
    """关门消息"""
    def __init__(self, psk: str):
        body = {}
        super().__init__("close", body, psk, use_milliseconds=True)


class QueryStatusMessage(Message):
    """查询状态消息"""
    def __init__(self, psk: str):
        body = {"query_type": "status","fields": ["status","battery","temperature"]}
        super().__init__("query", body, psk, use_milliseconds=True)


class QueryDeviceSnMessage(Message):
    """查询设备序列号消息"""
    def __init__(self, psk: str):
        body = {}
        super().__init__("query_device_sn", body, psk, use_milliseconds=True)


class DiscoverMessage(Message):
    """发现消息"""
    def __init__(self, psk: str):
        body = {}
        super().__init__("discover", body, psk, use_milliseconds=True)


class RemotePairingMessage(Message):
    """远程配对消息"""
    def __init__(self, psk: str, duration: int = 100):
        body = {"duration":duration}
        super().__init__("remote_pairing", body, psk, use_milliseconds=True)


class OTAUpgradeMessage(Message):
    """OTA升级消息"""
    def __init__(self, psk: str, tftp_server: str, tftp_port: int = 69,
                 firmware_file: str = "update.fwpkg", file_size: int = 0, md5: str = None):
        body = {"tftp_url": f"tftp://{tftp_server}:{tftp_port}/{firmware_file}", "file_size": file_size}
        if md5:
            body["md5"] = md5
        super().__init__("ota_upgrade", body, psk, use_milliseconds=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(',', ':'))


class WriteWifiBleMacMessage(Message):
    """写��WiFi/BLE MAC地址"""
    def __init__(self, psk: str, mac: str):
        body = {"mac": mac}
        super().__init__("write_wifi_ble_mac", body, psk, use_milliseconds=True)


class ReadWifiBleMaxMessage(Message):
    """读取WiFi/BLE MAC地址"""
    def __init__(self, psk: str):
        body = {}
        super().__init__("read_wifi_ble_mac", body, psk, use_milliseconds=True)


class WriteSleMaxMessage(Message):
    """写入SLE MAC地址"""
    def __init__(self, psk: str, mac: str):
        body = {"mac": mac}
        super().__init__("write_sle_mac", body, psk, use_milliseconds=True)


class ReadSleMaxMessage(Message):
    """读取SLE MAC地址"""
    def __init__(self, psk: str):
        body = {}
        super().__init__("read_sle_mac", body, psk, use_milliseconds=True)


class ResetConfigMessage(Message):
    """重置配置"""
    def __init__(self, psk: str):
        body = {}
        super().__init__("reset_config", body, psk, use_milliseconds=True)


class BleDiscoverMessage(Message):
    """BLE发现"""
    def __init__(self, psk: str, duration: int = 5000):
        super().__init__("ble_discover", {"duration": duration}, psk, use_milliseconds=True)


class SleDiscoverMessage(Message):
    """SLE发现"""
    def __init__(self, psk: str, duration: int = 5000):
        super().__init__("sle_discover", {"duration": duration}, psk, use_milliseconds=True)


class WifiDiscoverMessage(Message):
    """WiFi发现"""
    def __init__(self, psk: str, duration: int = 5000):
        super().__init__("wifi_discover", {"duration": duration}, psk, use_milliseconds=True)


# ==================== 音箱消息（使用秒+device字段） ====================

class SpeakerOpenDoorMessage(Message):
    """音箱开门消息"""
    def __init__(self, psk: str, duration: int = 5000):
        body = {"duration": duration}
        device_info = {"sn": "master-001", "model": "MASTER"}
        super().__init__("open", body, psk, device_info, use_milliseconds=False)


class SpeakerCloseDoorMessage(Message):
    """音箱关门消息"""
    def __init__(self, psk: str):
        body = {}
        device_info = {"sn": "master-001", "model": "MASTER"}
        super().__init__("close", body, psk, device_info, use_milliseconds=False)


class SpeakerQueryStatusMessage(Message):
    """音箱查询状态消息"""
    def __init__(self, psk: str):
        body = {"query_type": "status", "fields": ["status", "battery", "temperature"]}
        device_info = {"sn": "master-001", "model": "MASTER"}
        super().__init__("query", body, psk, device_info, use_milliseconds=False)


class SpeakerOTAUpgradeMessage(Message):
    """音箱OTA升级消息"""
    def __init__(self, psk: str, tftp_server: str, tftp_port: int = 69,
                 firmware_file: str = "update.fwpkg", file_size: int = 0, md5: str = None):
        body = {
            "tftp_url": f"{tftp_server}:{tftp_port}/{firmware_file}",
            "file_size": file_size
        }
        if md5:
            body["md5"] = md5
        device_info = {"sn": "master-001", "model": "MASTER"}
        super().__init__("ota_upgrade", body, psk, device_info, use_milliseconds=False)
