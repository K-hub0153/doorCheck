#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MQTT设备校时模块
通过MQTT设置室外音箱的系统时间
协议: MQTT (action: set_system_datetime)
"""

import json
import time
import uuid
import secrets
import threading
from typing import Optional, Dict
from datetime import datetime

from .mqtt_client import MQTTClient
from ..utils.logger import logger


class MqttDatetimeSyncClient:
    """通过MQTT同步设备系统时间（仅用于室外音箱）

    请求 -> {product_id}/{sn}/command
    响应 <- {product_id}/{sn}/reply
    """

    def __init__(self, broker: str, port: int, product_id: str, device_sn: str, device_model: str = ""):
        self.product_id = product_id
        self.device_sn = device_sn
        self.device_model = device_model or "UNKNOWN"
        self.mqtt_client = MQTTClient(broker, port, product_id, device_sn)
        self._response = None
        self._response_event = threading.Event()
        self._connected = False

    def connect(self, timeout: int = 5) -> bool:
        """连接到MQTT Broker"""
        self._connected = self.mqtt_client.connect(timeout)
        if self._connected:
            self.mqtt_client.register_callback("datetime_sync", self._on_message)
        return self._connected

    def disconnect(self):
        """断开MQTT连接"""
        try:
            self.mqtt_client.unregister_callback("datetime_sync")
            self.mqtt_client.disconnect()
        except Exception:
            pass
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.mqtt_client.connected

    def _on_message(self, topic: str, message: Dict):
        """处理响应消息"""
        try:
            logger.info(f"[校时] 收到MQTT消息: topic={topic}")
            logger.debug(f"[校时] 消息内容: {message}")

            if self.device_sn not in topic:
                logger.debug(f"[校时] 跳过消息: device_sn({self.device_sn}) 不在 topic({topic}) 中")
                return

            if 'reply' not in topic:
                logger.debug(f"[校时] 跳过消息: topic({topic}) 不包含 'reply'")
                return

            header = message.get('header', {})
            action = header.get('action', '')

            logger.info(f"[校时] 消息匹配，action={action}")

            if action == 'set_system_datetime':
                self._response = message
                self._response_event.set()
                logger.info(f"[校时] 校时响应已接收: {self.device_sn}")
            else:
                logger.debug(f"[校时] action不匹配: 期望 'set_system_datetime'，实际 '{action}'")

        except Exception as e:
            logger.error(f"处理校时响应失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _build_request(self) -> str:
        """构建校时请求消息"""
        ts = int(time.time() * 1000)

        # 获取当前系统时间
        now = datetime.now()

        msg = {
            "data": {
                "header": {
                    "ver": "1.0",
                    "mid": uuid.uuid4().hex,
                    "ts": ts,
                    "nonce": secrets.token_hex(8).upper(),
                    "type": "req",
                    "action": "set_system_datetime",
                    "sessionId": 10001,
                    "sig": "",
                    "device": {
                        "model": "MASTER",
                        "sn": "master_device_001"
                    }
                },
                "body": {
                    "params": {
                        "datetime_type": "Manual",
                        "daylight_savings": False,
                        "timezone": "GMT+8:00",
                        "utc_datetime": {
                            "year": now.year,
                            "month": now.month,
                            "day": now.day,
                            "hour": now.hour,
                            "minute": now.minute,
                            "second": now.second
                        }
                    }
                }
            },
            "messageChannelType": "MQTT_LOCAL"
        }

        return json.dumps(msg, ensure_ascii=False)

    def sync_datetime(self, timeout: int = 10) -> bool:
        """
        同步系统时间到设备

        Args:
            timeout: 超时时间（秒），默认10秒

        Returns:
            bool: 成功返回True，失败返回False
        """
        if not self.connected:
            logger.error(f"MQTT未连接，无法同步时间: {self.device_sn}")
            return False

        self._response = None
        self._response_event.clear()

        # 构建并发送请求
        payload = self._build_request()

        logger.info(f"发送校时请求到设备: {self.device_sn}")
        logger.debug(f"请求内容: {payload}")

        if not self.mqtt_client.publish(payload):
            logger.error(f"发送校时请求失败: {self.device_sn}")
            return False

        # 等待响应
        if not self._response_event.wait(timeout):
            logger.error(f"校时请求超时: {self.device_sn}")
            return False

        # 解析响应
        try:
            if not self._response:
                logger.error(f"未收到校时响应: {self.device_sn}")
                return False

            # 响应格式可能是 {"header": {...}, "body": {...}} 或包装在 data 中
            response_data = self._response
            if 'data' in response_data:
                response_data = response_data['data']

            body = response_data.get('body', {})
            params = body.get('params', {})
            code = params.get('code')
            msg = params.get('msg', '')

            if code == 0:
                logger.info(f"设备校时成功: {self.device_sn}")
                return True
            else:
                logger.error(f"设备校时失败: {self.device_sn}, code={code}, msg={msg}")
                return False

        except Exception as e:
            logger.error(f"解析校时响应失败: {self.device_sn} -> {e}")
            return False


def sync_speaker_datetime(broker: str, port: int, product_id: str, device_sn: str, device_model: str = "") -> bool:
    """
    便捷函数：同步音箱系统时间

    Args:
        broker: MQTT Broker地址
        port: MQTT Broker端口
        product_id: 产品ID
        device_sn: 设备SN
        device_model: 设备型号（可选）

    Returns:
        bool: 成功返回True，失败返回False
    """
    client = None
    try:
        client = MqttDatetimeSyncClient(broker, port, product_id, device_sn, device_model)

        if not client.connect(timeout=5):
            logger.error(f"连接MQTT Broker失败: {device_sn}")
            return False

        result = client.sync_datetime(timeout=10)
        return result

    except Exception as e:
        logger.error(f"校时异常: {device_sn} -> {e}")
        return False

    finally:
        if client:
            client.disconnect()
