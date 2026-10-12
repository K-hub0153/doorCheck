import yaml
import os
import sys
from typing import Dict, Any
from .paths import get_app_dir


def get_resource_path(relative_path):
    """获取资源文件的绝对路径，兼容PyInstaller打包"""
    base_path = get_app_dir()

    external_path = os.path.join(base_path, relative_path)
    if os.path.exists(external_path):
        return external_path

    if getattr(sys, 'frozen', False):
        internal_path = os.path.join(sys._MEIPASS, relative_path)
        if os.path.exists(internal_path):
            return internal_path

    return external_path


class Config:
    _instance = None
    _config = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._config is None:
            self.load_config()

    def load_config(self, config_path: str = None):
        if config_path is None:
            config_path = os.path.join(get_app_dir(), 'config', 'config.yaml')
        
        with open(config_path, 'r', encoding='utf-8') as f:
            self._config = yaml.safe_load(f)

    def get(self, key: str, default=None) -> Any:
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    @property
    def app_name(self) -> str:
        return self.get('app.name', '智能门锁产测工具')

    @property
    def app_version(self) -> str:
        return self.get('app.version', '1.0.0')

    @property
    def device_psk(self) -> str:
        return self.get('device.psk', 'weidian_24h')

    @property
    def product_id(self) -> str:
        return self.get('device.product_id', '1696')

    @property
    def mqtt_broker(self) -> str:
        return self.get('mqtt.broker', '22.0.0.1')

    @property
    def mqtt_port(self) -> int:
        return self.get('mqtt.port', 1881)

    @property
    def mdns_service_type(self) -> str:
        return self.get('mdns.service_type', '_mqtt._tcp.local.')

    @property
    def test_open_duration(self) -> int:
        return self.get('test.open_duration', 5000)

    @property
    def test_timeout(self) -> int:
        return self.get('test.test_timeout', 30)

    @property
    def http_port(self) -> int:
        return self.get('http.port', 8081)

    @property
    def printer_enabled(self) -> bool:
        return self.get('printer.enabled', True)

    @property
    def printer_config(self) -> Dict:
        return self.get('printer', {})

    @property
    def wifi_rssi_threshold(self) -> int:
        return self.get('test.wifi_rssi_threshold', -40)

    @property
    def ble_rssi_threshold(self) -> int:
        return self.get('test.ble_rssi_threshold', -70)

    @property
    def sle_min_count(self) -> int:
        return self.get('test.sle_min_count', 1)

    @property
    def discover_duration(self) -> int:
        return self.get('test.discover_duration', 5000)

    @property
    def ir_strict_verify(self) -> bool:
        return self.get('test.ir_strict_verify', False)

    @property
    def broker_mode(self) -> str:
        """获取MQTT Broker模式（local/remote）"""
        return self.get('mqtt.broker_mode', 'local')

    def save_broker_mode(self, mode: str):
        """保存MQTT Broker模式到配置文件"""
        try:
            config_path = os.path.join(get_app_dir(), 'config', 'config.yaml')

            # 确保mqtt节点存在
            if 'mqtt' not in self._config:
                self._config['mqtt'] = {}

            self._config['mqtt']['broker_mode'] = mode

            # 写回配置文件
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)

            return True
        except Exception as e:
            print(f"保存MQTT Broker模式失败: {e}")
            return False

    @property
    def speaker_type(self) -> str:
        """获取音箱类型配置"""
        return self.get('ui.speaker_type', 'indoor')

    def save_speaker_type(self, speaker_type: str):
        """保存音箱类型到配置文件"""
        try:
            config_path = os.path.join(get_app_dir(), 'config', 'config.yaml')

            # 确保ui节点存在
            if 'ui' not in self._config:
                self._config['ui'] = {}

            self._config['ui']['speaker_type'] = speaker_type

            # 写回配置文件
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)

            return True
        except Exception as e:
            print(f"保存音箱类型失败: {e}")
            return False

    def get_layout_mode(self) -> int:
        """获取布局模式配置（1/4/9宫格）"""
        return self.get('ui.layout_mode', 4)

    def get_remember_layout(self) -> bool:
        """获取是否记忆布局模式配置"""
        return self.get('ui.remember_layout', False)

    def save_layout_mode(self, layout_mode: int):
        """保存布局模式到配置文件"""
        try:
            config_path = os.path.join(get_app_dir(), 'config', 'config.yaml')

            # 确保ui节点存在
            if 'ui' not in self._config:
                self._config['ui'] = {}

            self._config['ui']['layout_mode'] = layout_mode

            # 写回配置文件
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)

            return True
        except Exception as e:
            print(f"保存布局模式失败: {e}")
            return False

