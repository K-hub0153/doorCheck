from flask import Flask, jsonify, request
from typing import Dict
import socket
import threading
from werkzeug.serving import make_server
from ..utils.logger import logger


class ConfigServer:
    def __init__(self, host: str, port: int, mqtt_broker: str, mqtt_port: int, secret_key: str, on_device_config_callback=None, broker_mode: str = 'local', mqtt_username: str = "", mqtt_password: str = ""):
        self.app = Flask(__name__)
        self.host = host
        self.port = port
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port
        self.secret_key = secret_key
        self.broker_mode = broker_mode  # 新增：broker模式（local/remote）
        self.on_device_config_callback = on_device_config_callback  # 回调函数：(sn, product_id) -> None
        # 下发给设备的MQTT凭据（门锁固件要求非空，且需为base64(AES-128-ECB)加密后的值）。
        # 音箱工具不传，保持默认空串，行为与从前一致。
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.server = None
        self.server_thread = None
        self._setup_routes()
    
    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return socket.gethostbyname(socket.gethostname())
    
    def _setup_routes(self):
        @self.app.route('/api/device/config', methods=['GET'])
        def get_device_config():
            sn = request.args.get('sn')
            device_type = request.args.get('type', '')
            product_id_from_device = request.args.get('productId', '')

            if not sn:
                logger.warning("配置请求缺少sn参数")
                return jsonify({"code": 400, "message": "缺少sn参数"}), 400

            # 根据设备类型确定正确的 product_id（我们主动下发）
            if device_type:
                from .device_info import PRODUCT_ID_MAP
                product_id = PRODUCT_ID_MAP.get(device_type, '1696')
                logger.info(f"设备请求配置: sn={sn}, type={device_type}, 下发product_id={product_id} (类型映射)")
            elif product_id_from_device:
                # 如果没有 type 但设备提供了 productId，使用设备的（兼容旧版本）
                product_id = product_id_from_device
                logger.info(f"设备请求配置: sn={sn}, 下发product_id={product_id} (设备提供)")
            else:
                product_id = '1696'
                logger.info(f"设备请求配置: sn={sn}, 下发product_id={product_id} (默认值)")

            # 根据broker模式决定使用的IP
            if self.broker_mode == 'remote':
                # 远程模式：直接使用配置的broker地址
                broker_ip = self.mqtt_broker
                logger.info(f"远程Broker模式，使用配置的broker地址: {broker_ip}")
            else:
                broker_ip = self._get_local_ip()
                logger.info(f"本地Broker模式({device_type or '未知类型'})，动态获取本机IP: {broker_ip}")

            config = {
                "code": 0,
                "message": "success",
                "data": {
                    "mqtt": {
                        "broker": broker_ip,
                        "port": self.mqtt_port,
                        "username": self.mqtt_username,
                        "password": self.mqtt_password,
                        "clientId": f"device_{sn}",
                        "keepAlive": 60,
                        "cleanSession": True,
                        "ssl": True,
                        "protocol": "ssl",
                        "verifyCert": False
                    },
                    "topics": {
                        "command": f"{product_id}/{sn}/command",
                        "reply": f"{product_id}/{sn}/reply",
                        "status": f"{product_id}/{sn}/status",
                        "event": f"{product_id}/{sn}/event"
                    },
                    "secretKey": self.secret_key,
                    "heartbeatInterval": 30
                }
            }
            cred_state = "凭据已下发" if self.mqtt_username else "无凭据(匿名)"
            logger.info(f"返回MQTT配置: broker={broker_ip}:{self.mqtt_port}, topics使用product_id={product_id}, {cred_state}")

            # 通知主窗口更新设备的 product_id
            if self.on_device_config_callback:
                try:
                    self.on_device_config_callback(sn, product_id)
                except Exception as e:
                    logger.error(f"调用 on_device_config_callback 失败: {e}")

            return jsonify(config)
        
        @self.app.route('/health', methods=['GET'])
        def health_check():
            return jsonify({"status": "ok"})
    
    def start(self):
        """启动HTTP服务器（非阻塞）"""
        try:
            self.server = make_server(self.host, self.port, self.app, threaded=True)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            logger.info(f"HTTP配置服务启动: {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"HTTP服务器启动失败: {e}")

    def stop(self):
        """停止HTTP服务器"""
        if self.server:
            try:
                logger.info("正在停止HTTP配置服务...")
                self.server.shutdown()
                if self.server_thread:
                    self.server_thread.join(timeout=2)
                logger.info("HTTP配置服务已停止")
            except Exception as e:
                logger.error(f"停止HTTP服务器失败: {e}")
            finally:
                self.server = None
                self.server_thread = None
