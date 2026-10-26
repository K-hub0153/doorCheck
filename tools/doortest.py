#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门锁设备测试脚本
支持MQTT协议，自动发现或手动输入SN
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── 设备发现（复用 door_ctl2 逻辑）──────────────────────────────────────────

class DeviceDiscovery(ServiceListener):
    def __init__(self):
        self.devices = []

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if not info:
            return
        try:
            addresses = info.parsed_addresses()
            if not addresses:
                return
            ip = addresses[0]
            properties = {k.decode('utf-8'): v.decode('utf-8') for k, v in info.properties.items()}
            sn = properties.get('sn', '')
            model = properties.get('model', '')
            if not sn and 'device' in properties:
                try:
                    d = json.loads(properties['device'])
                    sn = d.get('sn', '')
                    model = model or d.get('model', '')
                except Exception:
                    pass
            if sn:
                self.devices.append({'sn': sn, 'ip': ip, 'port': info.port, 'model': model or 'Unknown'})
        except Exception:
            pass

    def remove_service(self, zc, type_, name): pass
    def update_service(self, zc, type_, name): pass


def discover_devices(timeout: int = 5) -> List[Dict[str, str]]:
    print(f"正在扫描设备... (等待{timeout}秒)")
    zc = Zeroconf()
    listener = DeviceDiscovery()
    browser = ServiceBrowser(zc, "_mqtt._tcp.local.", listener)
    time.sleep(timeout)
    browser.cancel()
    zc.close()
    return listener.devices


# ── 签名（与工程实现一致）────────────────────────────────────────────────────

def _cjson_serialize(obj) -> str:
    if isinstance(obj, dict):
        return "{" + ",".join(f'"{k}":{_cjson_serialize(obj[k])}' for k in sorted(obj)) + "}"
    if isinstance(obj, list):
        return "[" + ", ".join(_cjson_serialize(i) for i in obj) + "]"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        return str(obj)
    if obj is None:
        return "null"
    return json.dumps(obj, ensure_ascii=False)


def _sign(ver, mid, ts, action, body: dict, nonce, psk) -> str:
    body_json = _cjson_serialize(body)
    data = f"{ver}{mid}{ts}{action}{body_json}{nonce}{psk}"
    sig = hmac.new(psk.encode(), data.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()


def build_message(action: str, body: dict, psk: str) -> dict:
    ver, mid = "1.0", uuid.uuid4().hex
    ts = int(time.time() * 1000)
    nonce = secrets.token_hex(8)
    return {
        "header": {
            "ver": ver, "mid": mid, "ts": ts,
            "nonce": nonce, "type": "req",
            "action": action, "sig": _sign(ver, mid, ts, action, body, nonce, psk)
        },
        "body": body
    }


# ── 配置获取 ─────────────────────────────────────────────────────────────────

def get_device_config(server_ip: str, sn: str) -> Optional[Dict[str, Any]]:
    try:
        url = f"http://{server_ip}:8081/api/device/config"
        resp = requests.get(url, params={"sn": sn, "type": "SMART_DOOR", "model": "CH390-LOCK"}, timeout=5)
        data = resp.json()
        if data.get("code") == 0:
            return data["data"]
        print(f"获取配置失败: {data.get('message')}")
    except Exception as e:
        print(f"获取配置异常: {e}")
    return None


# ── MQTT 发送 ─────────────────────────────────────────────────────────────────

class MQTTSession:
    def __init__(self, broker, port, username, password, topics, psk, ca_cert=None):
        self.broker = broker
        self.port = port
        self.topics = topics
        self.psk = psk
        self._response = None

        try:
            self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"doortest_{int(time.time())}")
        except AttributeError:
            self.client = mqtt.Client(client_id=f"doortest_{int(time.time())}")
        self.client.on_message = self._on_message
        if username:
            self.client.username_pw_set(username, password)
        if ca_cert and os.path.exists(ca_cert):
            self.client.tls_set(ca_certs=ca_cert, cert_reqs=ssl.CERT_NONE)
            self.client.tls_insecure_set(True)
        self.client.connect(broker, port, 60)
        self.client.subscribe(topics["reply"], qos=1)
        self.client.loop_start()
        print(f"已连接 MQTT {broker}:{port}")

    def _on_message(self, client, userdata, msg):
        try:
            self._response = json.loads(msg.payload.decode())
            print(f"收到响应:\n{json.dumps(self._response, indent=2, ensure_ascii=False)}")
        except Exception as e:
            print(f"解析响应失败: {e}")

    def send(self, action: str, body: dict, timeout: int = 10) -> bool:
        msg = build_message(action, body, self.psk)
        topic = self.topics["command"]
        print(f"\n发送 [{action}] -> {topic}")
        print(json.dumps(msg, indent=2, ensure_ascii=False))
        self._response = None
        self.client.publish(topic, json.dumps(msg), qos=1)
        deadline = time.time() + timeout
        while self._response is None and time.time() < deadline:
            time.sleep(0.1)
        if self._response is None:
            print("等待响应超时")
            return False
        return True

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


# ── 菜单 ──────────────────────────────────────────��───────────────────────────

def show_menu():
    print("\n" + "="*40)
    print("  1. 开门")
    print("  2. 锁门")
    print("  3. 查询状态")
    print("  4. 遥控器配对")
    print("  5. OTA升级")
    print("  0. 退出")
    print("="*40)


def main():
    print("门锁设备测试脚本")
    print("-" * 40)

    # 1. 确定 SN 和 IP
    sn_input = input("请输入设备SN（留空则自动扫描）: ").strip()

    device_sn = None
    device_ip = None

    if sn_input:
        device_sn = sn_input
        device_ip = input("请输入设备IP（用于HTTP获取配置）: ").strip() or None
    else:
        devices = discover_devices(timeout=5)
        if not devices:
            print("未发现设备，请手动输入")
            device_sn = input("设备SN: ").strip()
            device_ip = input("设备IP: ").strip() or None
        elif len(devices) == 1:
            device_sn = devices[0]['sn']
            device_ip = devices[0]['ip']
            print(f"自动选择: SN={device_sn}  IP={device_ip}")
        else:
            print(f"\n发现 {len(devices)} 个设备:")
            for i, d in enumerate(devices, 1):
                print(f"  {i}. SN={d['sn']}  IP={d['ip']}  型号={d['model']}")
            idx = int(input(f"请选择 (1-{len(devices)}): ").strip()) - 1
            device_sn = devices[idx]['sn']
            device_ip = devices[idx]['ip']

    if not device_sn:
        print("错误: SN不能为空")
        return

    # 2. 获取 MQTT 配置（配置服务器固定为 22.0.0.1）
    config_ip = input("配置服务器IP [22.0.0.1]: ").strip() or "22.0.0.1"
    config = get_device_config(config_ip, device_sn)

    if config:
        mqtt_cfg = config["mqtt"]
        broker = mqtt_cfg["broker"]
        port = mqtt_cfg["port"]
        username = mqtt_cfg.get("username")
        password = mqtt_cfg.get("password")
        raw_topics = config.get("topics", {})
        topics = {k: v.replace("lock-00000001", device_sn) for k, v in raw_topics.items()}
        print(f"MQTT: {broker}:{port}")
    else:
        broker = input("MQTT服务器地址 [127.0.0.1]: ").strip() or "127.0.0.1"
        port = int(input("MQTT端口 [1881]: ").strip() or "1881")
        username = password = None
        topics = {
            "command": f"1696/{device_sn}/command",
            "reply":   f"1696/{device_sn}/reply",
        }

    psk = input("PSK密钥 [weidian_24h]: ").strip() or "weidian_24h"

    # 3. 建立 MQTT 连接
    ca_cert = os.path.join(os.path.dirname(__file__), '..', 'certs', 'ca.crt')
    try:
        session = MQTTSession(broker, port, username, password, topics, psk,
                              ca_cert=ca_cert if os.path.exists(ca_cert) else None)
    except Exception as e:
        print(f"MQTT连接失败: {e}")
        return

    # 4. 命令循环
    while True:
        show_menu()
        choice = input("请选择 (0-5): ").strip()

        if choice == "0":
            break
        elif choice == "1":
            dur = int(input("开门时长(ms) [5000]: ").strip() or "5000")
            session.send("open", {"duration": dur})
        elif choice == "2":
            session.send("close", {})
        elif choice == "3":
            session.send("query", {"query_type": "status", "fields": ["status", "battery", "temperature"]})
        elif choice == "4":
            dur = int(input("配对时长(秒) [100]: ").strip() or "100")
            session.send("remote_pairing", {"duration": dur})
        elif choice == "5":
            url = input("TFTP URL: ").strip()
            size = int(input("文件大小(字节): ").strip())
            session.send("ota_upgrade", {"tftp_url": url, "file_size": size})
        else:
            print("无效选择")

        input("\n按回车继续...")

    session.close()
    print("已退出")


if __name__ == "__main__":
    main()
