#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门控设备控制脚本
支持MQTT和HTTP协议控制门锁设备
"""

"""
requests>=2.31.0
paho-mqtt>=1.6.1
zeroconf>=0.135.0
"""

import sys
import os
import json
import time
import uuid
import hmac
import hashlib
import base64
import secrets
import ssl
import requests
import paho.mqtt.client as mqtt
from typing import Optional, Dict, Any, List
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class DeviceDiscovery(ServiceListener):
    """设备发现监听器"""

    def __init__(self):
        self.devices = []

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        print(f"  [DEBUG] add_service called: type={type_}, name={name}")
        info = zc.get_service_info(type_, name)
        if info:
            try:
                addresses = info.parsed_addresses()
                if not addresses:
                    print(f"  [DEBUG] no addresses for {name}")
                    return
                ip = addresses[0]
                port = info.port
                print(f"  [DEBUG] raw properties: {info.properties}")
                properties = {k.decode('utf-8'): v.decode('utf-8') for k, v in info.properties.items()}
                print(f"  [DEBUG] parsed properties: {properties}")
                sn = properties.get('sn', '')
                model = properties.get('model', '')

                if not sn and 'device' in properties:
                    try:
                        device_data = json.loads(properties['device'])
                        sn = device_data.get('sn', '')
                        model = model or device_data.get('model', '')
                    except Exception:
                        pass

                print(f"  [DEBUG] sn={sn}, model={model}, ip={ip}, port={port}")
                if sn:
                    self.devices.append({
                        'sn': sn,
                        'ip': ip,
                        'port': port,
                        'model': model or 'Unknown',
                        'name': name
                    })
            except Exception as e:
                print(f"  [DEBUG] exception: {e}")
        else:
            print(f"  [DEBUG] get_service_info returned None for {name}")

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def discover_devices(timeout: int = 5) -> List[Dict[str, str]]:
    """发现局域网内的设备"""
    print(f"正在扫描设备... (等待{timeout}秒)")

    zeroconf = Zeroconf()
    listener = DeviceDiscovery()
    browser = ServiceBrowser(zeroconf, "_mqtt._tcp.local.", listener)

    time.sleep(timeout)

    browser.cancel()
    zeroconf.close()

    return listener.devices


def get_device_config(device_ip: str, sn: str, device_type: str = "SMART_DOOR", model: str = "CH390-LOCK") -> Optional[Dict[str, Any]]:
    """通过HTTP获取设备MQTT配置"""
    try:
        url = f"http://{device_ip}:8081/api/device/config"
        params = {"sn": sn, "type": device_type, "model": model}
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        if data.get("code") == 0:
            return data["data"]
        else:
            print(f"获取设备配置失败: {data.get('message')}")
            return None
    except Exception as e:
        print(f"获取设备配置异常: {e}")
        return None



class DoorController:
    """门控设备控制器"""

    def __init__(self, device_ip: str, mqtt_broker: str = "127.0.0.1",
                 mqtt_port: int = 1881, psk: str = "weidian_24h", http_port: int = 57020,
                 mqtt_username: str = None, mqtt_password: str = None,
                 mqtt_topics: Dict[str, str] = None):
        self.device_ip = device_ip
        self.http_port = http_port
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port
        self.psk = psk
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.mqtt_topics = mqtt_topics or {}
        self.product_id = "1696"
        self.device_sn = None
        self.mqtt_client = None
        self.response_received = False
        self.response_data = None

    def _generate_nonce(self) -> str:
        """生成16字节随机nonce"""
        return secrets.token_hex(8)

    def _body_to_sign_json(self, body: Dict[str, Any]) -> str:
        """将body转换为签名用的JSON字符串（cJSON格式）"""
        return self._cjson_serialize(body)

    def _cjson_serialize(self, obj) -> str:
        """序列化为cJSON_PrintUnformatted兼容格式"""
        if isinstance(obj, dict):
            items = []
            for k in sorted(obj.keys()):
                items.append(f'"{k}":{self._cjson_serialize(obj[k])}')
            return "{" + ",".join(items) + "}"
        elif isinstance(obj, list):
            elements = [self._cjson_serialize(item) for item in obj]
            return "[" + ", ".join(elements) + "]"
        elif isinstance(obj, str):
            return json.dumps(obj, ensure_ascii=False)
        elif isinstance(obj, bool):
            return "true" if obj else "false"
        elif isinstance(obj, int):
            return str(obj)
        elif isinstance(obj, float):
            return str(obj)
        elif obj is None:
            return "null"
        else:
            return json.dumps(obj, ensure_ascii=False)

    def _calculate_signature(self, ver: str, mid: str, ts: int, action: str,
                             body: Dict[str, Any], nonce: str) -> str:
        """计算消息签名"""
        body_json = self._body_to_sign_json(body)
        sign_data = f"{ver}{mid}{ts}{action}{body_json}{nonce}{self.psk}"
        signature = hmac.new(
            self.psk.encode('utf-8'),
            sign_data.encode('utf-8'),
            hashlib.sha256
        ).digest()
        return base64.b64encode(signature).decode('utf-8')

    def _build_message(self, action: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """构建协议消息"""
        ver = "1.0"
        mid = str(uuid.uuid4()).replace('-', '')
        ts = int(time.time() * 1000)
        nonce = self._generate_nonce()
        sig = self._calculate_signature(ver, mid, ts, action, body, nonce)

        return {
            "header": {
                "ver": ver,
                "mid": mid,
                "ts": ts,
                "nonce": nonce,
                "type": "req",
                "action": action,
                "sig": sig
            },
            "body": body
        }

    def _on_mqtt_message(self, client, userdata, msg):
        """MQTT消息回调"""
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            print(f"收到响应: {json.dumps(payload, indent=2, ensure_ascii=False)}")
            self.response_data = payload
            self.response_received = True
        except Exception as e:
            print(f"解析响应失败: {e}")

    def send_mqtt_command(self, action: str, body: Dict[str, Any]) -> bool:
        """通过MQTT发送命令"""
        if not self.device_sn:
            print("错误: 请先输入设备SN")
            return False

        try:
            message = self._build_message(action, body)
            topic = self.mqtt_topics.get("command") or f"{self.product_id}/{self.device_sn}/command"

            client = mqtt.Client(client_id=f"door_ctl_{int(time.time())}")
            client.on_message = self._on_mqtt_message

            if self.mqtt_username and self.mqtt_password:
                client.username_pw_set(self.mqtt_username, self.mqtt_password)

            # 配置SSL
            if self.mqtt_port in [1881, 8883]:
                ca_cert = os.path.join(os.path.dirname(__file__), '..', 'certs', 'ca.crt')
                if os.path.exists(ca_cert):
                    client.tls_set(ca_certs=ca_cert, cert_reqs=ssl.CERT_NONE)
                    client.tls_insecure_set(True)
                    print(f"已启用SSL: {ca_cert}")

            print(f"连接MQTT服务器: {self.mqtt_broker}:{self.mqtt_port}")
            print(f"MQTT用户名: {self.mqtt_username}")
            client.connect(self.mqtt_broker, self.mqtt_port, 60)

            reply_topic = self.mqtt_topics.get("reply") or f"{self.product_id}/{self.device_sn}/reply"
            print(f"订阅reply topic: {reply_topic}")
            client.subscribe(reply_topic, qos=1)
            client.loop_start()

            print(f"发送命令到: {topic}")
            print(f"消息内容: {json.dumps(message, indent=2, ensure_ascii=False)}")

            client.publish(topic, json.dumps(message), qos=1)

            timeout = 10
            start = time.time()
            self.response_received = False

            while not self.response_received and (time.time() - start) < timeout:
                time.sleep(0.1)

            client.loop_stop()
            client.disconnect()

            if self.response_received:
                print("✓ 命令执行成功")
                return True
            else:
                print("✗ 等待响应超时")
                return False

        except Exception as e:
            print(f"✗ 发送命令失败: {e}")
            return False

    def send_http_command(self, endpoint: str, action: str, body: Dict[str, Any]) -> bool:
        """通过HTTP发送命令"""
        try:
            message = self._build_message(action, body)
            url = f"http://{self.device_ip}:{self.http_port}{endpoint}"

            print(f"发送HTTP请求: {url}")
            print(f"消息内容: {json.dumps(message, indent=2, ensure_ascii=False)}")

            response = requests.post(url, json=message, timeout=10)

            print(f"响应状态码: {response.status_code}")
            print(f"响应内容: {response.text}")

            if response.status_code == 200:
                print("✓ 命令执行成功")
                return True
            else:
                print(f"✗ 命令执行失败")
                return False

        except Exception as e:
            print(f"✗ HTTP请求失败: {e}")
            return False


def show_menu():
    """显示命令菜单"""
    print("\n" + "="*50)
    print("门控设备控制脚本")
    print("="*50)
    print("\nMQTT命令:")
    print("  1. 开门 (MQTT)")
    print("  2. 锁门 (MQTT)")
    print("  3. 查询状态 (MQTT)")
    print("  4. 遥控器配对 (MQTT)")
    print("  5. OTA升级 (MQTT)")
    print("\nHTTP命令:")
    print("  6. 开门 (HTTP)")
    print("  7. 重启设备 (HTTP)")
    print("  8. OTA升级 (HTTP)")
    print("  9. 切换网络通道 (HTTP)")
    print("\n  0. 退出")
    print("="*50)


def main():
    print("门控设备控制脚本")
    print("-" * 50)

    # 发现设备
    devices = discover_devices(timeout=10)

    device_ip = None
    device_sn = None
    http_port = 57020

    if devices:
        print(f"\n发现 {len(devices)} 个设备:")
        for i, dev in enumerate(devices, 1):
            print(f"  {i}. SN: {dev['sn']}, IP: {dev['ip']}:{dev['port']}, 型号: {dev['model']}")

        if len(devices) == 1:
            device_ip = devices[0]['ip']
            device_sn = devices[0]['sn']
            http_port = devices[0]['port']
            print(f"\n自动选择设备: {device_sn} ({device_ip}:{http_port})")
        else:
            choice = input(f"\n请选择设备 (1-{len(devices)}): ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(devices):
                    device_ip = devices[idx]['ip']
                    device_sn = devices[idx]['sn']
                    http_port = devices[idx]['port']
                    print(f"已选择设备: {device_sn} ({device_ip}:{http_port})")
                else:
                    print("无效的选择")
            except ValueError:
                print("无效的输入")
    else:
        print("\n未发现设备")

    # 如果没有自动发现，手动输入
    if not device_ip:
        device_ip = input("\n请手动输入设备IP地址: ").strip()
        if not device_ip:
            print("错误: 设备IP地址不能为空")
            return

    if not device_sn:
        device_sn = input("请输入设备SN: ").strip()

    config_server_ip = input("请输入配置服务器IP [22.0.0.1]: ").strip() or "22.0.0.1"
    mqtt_broker = "22.0.0.1"
    mqtt_port = 1881
    mqtt_username = None
    mqtt_password = None
    mqtt_topics = {}
    psk = "weidian_24h"

    if device_sn and device_ip:
        url = f"http://{config_server_ip}:8081/api/device/config?sn={device_sn}&type=SMART_DOOR&model=CH390-LOCK"
        print(f"正在获取设备 {device_sn} 的MQTT配置: {url}")
        config = get_device_config(config_server_ip, device_sn)
        if config:
            mqtt_cfg = config.get("mqtt", {})
            mqtt_broker = mqtt_cfg.get("broker", mqtt_broker)
            mqtt_port = mqtt_cfg.get("port", mqtt_port)
            mqtt_username = mqtt_cfg.get("username")
            mqtt_password = mqtt_cfg.get("password")
            raw_topics = config.get("topics", {})
            mqtt_topics = {k: v.replace("lock-00000001", device_sn) for k, v in raw_topics.items()}
            print(f"MQTT服务器: {mqtt_broker}:{mqtt_port}")
        else:
            mqtt_broker = input("获取配置失败，请手动输入MQTT服务器地址 [127.0.0.1]: ").strip() or "127.0.0.1"
            mqtt_port = int(input("请输入MQTT端口 [1881]: ").strip() or "1881")

    psk = input("请输入PSK密钥 [weidian_24h]: ").strip() or "weidian_24h"

    controller = DoorController(device_ip, mqtt_broker, mqtt_port, psk, http_port,
                                mqtt_username, mqtt_password, mqtt_topics)
    if device_sn:
        controller.device_sn = device_sn

    while True:
        show_menu()
        choice = input("\n请选择命令 (0-9): ").strip()

        if choice == "0":
            print("退出程序")
            break

        elif choice == "1":
            if not controller.device_sn:
                controller.device_sn = input("请输入设备SN: ").strip()
            duration = input("开门持续时间(ms) [5000]: ").strip() or "5000"
            controller.send_mqtt_command("open", {"duration": int(duration)})

        elif choice == "2":
            if not controller.device_sn:
                controller.device_sn = input("请输入设备SN: ").strip()
            controller.send_mqtt_command("close", {})

        elif choice == "3":
            if not controller.device_sn:
                controller.device_sn = input("请输入设备SN: ").strip()
            controller.send_mqtt_command("query", {
                "query_type": "status",
                "fields": ["status", "battery", "temperature"]
            })

        elif choice == "4":
            if not controller.device_sn:
                controller.device_sn = input("请输入设备SN: ").strip()
            duration = input("配对持续时间(秒) [100]: ").strip() or "100"
            controller.send_mqtt_command("remote_pairing", {"duration": int(duration)})

        elif choice == "5":
            if not controller.device_sn:
                controller.device_sn = input("请输入设备SN: ").strip()
            tftp_url = input("请输入TFTP URL: ").strip()
            file_size = input("请输入文件大小(字节): ").strip()
            controller.send_mqtt_command("ota_upgrade", {
                "tftp_url": tftp_url,
                "file_size": int(file_size)
            })

        elif choice == "6":
            duration = input("开门持续时间(ms) [5000]: ").strip() or "5000"
            controller.send_http_command("/api/door/open", "door_open", {"duration": int(duration)})

        elif choice == "7":
            confirm = input("确认重启设备? (y/n): ").strip().lower()
            if confirm == 'y':
                controller.send_http_command("/api/reboot", "reboot", {})

        elif choice == "8":
            tftp_url = input("请输入TFTP URL: ").strip()
            file_size = input("请输入文件大小(字节): ").strip()
            md5 = input("请输入MD5校验值(可选): ").strip()
            body = {"tftp_url": tftp_url, "file_size": int(file_size)}
            if md5:
                body["md5"] = md5
            controller.send_http_command("/api/ota/upgrade", "ota_upgrade", body)

        elif choice == "9":
            print("\n网络类型:")
            print("  1. 有线网络 (Wired/Spinet)")
            print("  2. WiFi")
            print("  3. 星闪 (StarFlash)")
            print("  4. 蓝牙 (Bluetooth)")
            network_type = input("请选择网络类型 (1-4): ").strip()
            if network_type in ["1", "2", "3", "4"]:
                controller.send_http_command("/api/switch_network", "switch_network",
                                             {"network_type": int(network_type)})
            else:
                print("错误: 无效的网络类型")

        else:
            print("错误: 无效的选择")

        input("\n按回车键继续...")


if __name__ == "__main__":
    main()

