"""
统一的MQTT客户端

支持门控和音箱两种设备的MQTT通信需求
"""

import paho.mqtt.client as mqtt
import json
import threading
import ssl
import os
import time
import uuid
from typing import Callable, Optional, Dict
from ..utils.logger import logger
from ..utils.config import Config
from ..utils.paths import get_app_dir


def get_resource_path(relative_path: str) -> str:
    """获取资源文件路径（兼容打包后的环境）"""
    import sys
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(base_path, relative_path)


class MQTTClient:
    """
    统一的MQTT客户端

    支持特性:
    - SSL/TLS连接
    - 消息回调机制
    - 请求/响应模式（门控专用）
    - 通配符订阅
    """

    def __init__(
        self,
        broker: str,
        port: int,
        product_id: str,
        device_sn: str,
        client_id_prefix: str = "doorcheck"
    ):
        self.broker = broker
        self.port = port
        self.product_id = product_id
        self.device_sn = device_sn
        self.client_id_prefix = client_id_prefix
        self.client = None
        self.connected = False
        self.message_callbacks = {}
        self._lock = threading.Lock()
        self._connect_event = threading.Event()
        self._pending: Dict[str, dict] = {}  # mid -> {event, response} 用于request/response模式

        # 标准MQTT主题
        self.command_topic = f"{product_id}/{device_sn}/command"
        self.reply_topic = f"{product_id}/{device_sn}/reply"
        self.status_topic = f"{product_id}/{device_sn}/status"
        self.event_topic = f"{product_id}/{device_sn}/event"
        self.log_topic = f"{product_id}/{device_sn}/log"

    def connect(self, timeout: int = 5, use_wildcard: bool = False) -> bool:
        """
        连接到MQTT Broker

        Args:
            timeout: 连接超时时间（秒）
            use_wildcard: 是否使用通配符订阅（+/+/topic）
                         True: 音箱模式，订阅所有设备
                         False: 门控模式，只订阅自己的设备
        """
        try:
            self._connect_event.clear()

            # 生成Client ID（使用旧格式以保持兼容性）
            client_id = f"{self.client_id_prefix}_{self.device_sn}"
            self.client = mqtt.Client(client_id=client_id)

            # SSL/TLS配置
            if self.port in [1881, 8883]:
                # 优先使用ca.crt（兼容release分支）
                ca_cert = os.path.join(get_app_dir(), 'certs', 'ca.crt')
                if not os.path.exists(ca_cert):
                    # 降级使用mqtt_server.crt
                    ca_cert = get_resource_path(os.path.join('certs', 'mqtt_server.crt'))

                if os.path.exists(ca_cert):
                    self.client.tls_set(ca_certs=ca_cert, cert_reqs=ssl.CERT_NONE)
                    self.client.tls_insecure_set(True)
                    logger.info(f"MQTT SSL已启用: {ca_cert}")
                else:
                    logger.warning(f"MQTT证书文件未找到: {ca_cert}，将尝试无SSL连接")

            # 设置回调
            self.client.on_connect = lambda c, u, f, rc: self._on_connect(c, u, f, rc, use_wildcard)
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message

            logger.info(f"连接MQTT Broker: {self.broker}:{self.port}")
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()

            if self._connect_event.wait(timeout):
                logger.info(f"MQTT连接已建立: {self.device_sn}")
                return True
            else:
                logger.error(f"MQTT连接超时: {self.device_sn}")
                return False

        except Exception as e:
            logger.error(f"MQTT连接失败: {e}")
            return False

    def disconnect(self):
        """断开MQTT连接"""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            logger.info("MQTT已断开")

    def _on_connect(self, client, userdata, flags, rc, use_wildcard: bool = False):
        """连接成功回调"""
        if rc == 0:
            self.connected = True
            logger.info("MQTT连接成功")

            # 订阅主题
            if use_wildcard:
                # 音箱模式：订阅所有设备
                self.client.subscribe("+/+/reply", qos=1)
                self.client.subscribe("+/+/status", qos=1)
                self.client.subscribe("+/+/event", qos=1)
                self.client.subscribe("+/+/log", qos=1)
                logger.info("订阅主题: +/+/reply, +/+/status, +/+/event, +/+/log")
            else:
                # 门控模式：只订阅自己的设备
                self.client.subscribe(self.reply_topic, qos=1)
                self.client.subscribe(self.status_topic, qos=1)
                self.client.subscribe(self.event_topic, qos=1)
                self.client.subscribe(self.log_topic, qos=1)
                logger.info(f"订阅主题: {self.reply_topic}, {self.status_topic}, {self.event_topic}, {self.log_topic}")

            self._connect_event.set()
        else:
            logger.error(f"MQTT连接失败，错误码: {rc}")
            self._connect_event.set()

    def _on_disconnect(self, client, userdata, rc):
        """断开连接回调"""
        self.connected = False
        logger.warning(f"MQTT断开连接，错误码: {rc}")

    def _on_message(self, client, userdata, msg):
        """消息接收回调"""
        try:
            payload = msg.payload.decode('utf-8')
            logger.debug(f"收到消息 [{msg.topic}]: {payload}")
            try:
                message = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug(f"收到非JSON消息，已忽略 [{msg.topic}]: {payload[:100]}")
                return

            # 只有 reply topic 才走 mid 匹配
            if msg.topic == self.reply_topic or msg.topic == self.status_topic:
                mid = message.get('header', {}).get('mid', '')
                msg_type = message.get('header', {}).get('type', '')
                action = message.get('header', {}).get('action', '')
                # 只匹配 resp 类型，排除 heartbeat/notify
                if mid and msg_type == 'resp':
                    logger.info(f"收到响应 [{action}] topic={msg.topic}")
                    with self._lock:
                        pending = self._pending.get(mid)
                    if pending:
                        pending['response'] = message
                        pending['event'].set()
                        return

            # 调用注册的回调
            with self._lock:
                for callback in self.message_callbacks.values():
                    callback(msg.topic, message)

        except Exception as e:
            logger.error(f"处理消息失败: {e}")

    def publish(self, message: str, timeout: float = 5.0) -> bool:
        """
        发布消息

        Args:
            message: 消息内容（JSON字符串）
            timeout: 发布超时时间（秒）
        """
        if not self.connected:
            logger.error("MQTT未连接")
            return False

        try:
            result = self.client.publish(self.command_topic, message, qos=1)
            if timeout > 0:
                result.wait_for_publish(timeout=timeout)
            logger.info(f"发送消息到 {self.command_topic}: {message}")
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False

    def request(self, message: str, mid: str, timeout: float = 10.0) -> Optional[Dict]:
        """
        发送请求并等待响应（门控专用）

        Args:
            message: 请求消息（JSON字符串）
            mid: 消息ID
            timeout: 等待响应超时时间（秒）

        Returns:
            响应消息字典，超时返回None
        """
        if not self.connected:
            logger.error("MQTT未连接")
            return None

        # 注册等待
        event = threading.Event()
        with self._lock:
            self._pending[mid] = {'event': event, 'response': None}

        try:
            # 发布请求
            if not self.publish(message, timeout=timeout):
                return None

            # 等待响应
            if event.wait(timeout):
                with self._lock:
                    response = self._pending[mid]['response']
                    del self._pending[mid]
                return response
            else:
                logger.warning(f"等待响应超时: mid={mid}")
                with self._lock:
                    if mid in self._pending:
                        del self._pending[mid]
                return None

        except Exception as e:
            logger.error(f"请求失败: {e}")
            with self._lock:
                if mid in self._pending:
                    del self._pending[mid]
            return None

    def register_callback(self, name: str, callback: Callable):
        """
        注册消息回调

        Args:
            name: 回调名称
            callback: 回调函数 callback(topic: str, message: dict)
        """
        with self._lock:
            self.message_callbacks[name] = callback

    def unregister_callback(self, name: str):
        """取消注册回调"""
        with self._lock:
            if name in self.message_callbacks:
                del self.message_callbacks[name]
