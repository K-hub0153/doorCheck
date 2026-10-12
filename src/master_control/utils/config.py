import yaml
import os
import sys
import threading
from pathlib import Path
from .paths import config_dir, runtime_dir


class Config:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self):
        if not self._loaded:
            self._config = {}
            self._config_path = None
            self._mtime = None
            # 允许 GUI 定时器重载配置的同时，工作线程在读取配置，必须加锁
            self._lock = threading.RLock()
            self.load_config()
            self._loaded = True

    def _resolve_path(self) -> Path:
        """解析配置文件路径（与保存路径保持同一份，避免读写不同文件）"""
        return config_dir() / 'config.yaml'

    def load_config(self, force: bool = False) -> bool:
        """重新读取配置文件

        Args:
            force: True 时忽略 mtime 判断强制重读

        Returns:
            是否真的重新加载了
        """
        config_path = self._resolve_path()

        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        try:
            mtime = config_path.stat().st_mtime
        except OSError:
            mtime = None

        with self._lock:
            # 文件没变化且不是首次加载，直接跳过
            if not force and self._config_path == config_path and self._mtime == mtime and mtime is not None:
                return False

            with open(config_path, 'r', encoding='utf-8') as f:
                new_config = yaml.safe_load(f) or {}

            self._config = new_config
            self._config_path = config_path
            self._mtime = mtime
        return True

    def reload_if_changed(self) -> bool:
        """检测配置文件是否被外部修改，变化了才重载

        供 GUI 定时器调用，实现"改完配置文件实时生效"。
        """
        try:
            return self.load_config(force=False)
        except Exception:
            return False

    @property
    def config_path(self) -> Path:
        """当前生效的配置文件路径"""
        if self._config_path is None:
            self._config_path = self._resolve_path()
        return self._config_path

    def save(self):
        """把当前内存中的配置写回它自己所在的文件"""
        path = self.config_path
        with self._lock:
            with open(path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
            try:
                self._mtime = path.stat().st_mtime
            except OSError:
                self._mtime = None

    def set(self, key, value):
        """按点号路径写值，例如 set('printer.dpi', 203)"""
        keys = key.split('.')
        with self._lock:
            node = self._config
            for k in keys[:-1]:
                if not isinstance(node.get(k), dict):
                    node[k] = {}
                node = node[k]
            node[keys[-1]] = value

    def get(self, key, default=None):
        keys = key.split('.')
        with self._lock:
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
    def ssh_username(self):
        return self.get('ssh.username', 'root')

    @property
    def ssh_password(self):
        return self.get('ssh.password', '')

    @property
    def ssh_timeout(self):
        return self.get('ssh.timeout', 10)

    @property
    def poe_subnet(self):
        return self.get('network.poe_subnet', '198.19.0.0/24')

    @property
    def scan_subnet(self):
        return self.get('network.scan_subnet', '192.168.1.0/24')

    @property
    def mdns_service_type(self):
        return self.get('network.mdns_service_type', '_master._tcp.local.')

    @property
    def test_timeout(self):
        return self.get('test.timeout', 10)

    @property
    def wifi_signal_threshold(self):
        return self.get('test.wifi_signal_threshold', -30)

    @property
    def database_path(self):
        db_path = self.get('database.path', './mc_checker.db')
        if not os.path.isabs(db_path):
            return str(runtime_dir() / db_path)
        return db_path

    @property
    def printer_config(self):
        return self.get('printer', {})

    @property
    def print_require_all_passed(self):
        """打印标签是否要求所有必需检测项全部通过

        生产调试或复测场景下，需要在不通过时也能打标签，可把
        config.yaml 里 printer.require_all_passed 设为 false。
        """
        value = self.get('printer.require_all_passed', True)
        if isinstance(value, str):
            return value.strip().lower() not in ('false', '0', 'no', 'off')
        return bool(value)

    @staticmethod
    def _as_bool(value, default=True):
        """把配置里的布尔值统一解析，兼容 'false'/'0'/'no'/'off' 等写法"""
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ('false', '0', 'no', 'off', '')
        return bool(value)

    @property
    def print_confirm_poe_unplugged(self):
        """打印前是否弹窗提示拔掉所有 POE 设备

        由 config.yaml 的 printer.confirm_poe_unplugged 控制，
        设为 false 时直接打印，不弹确认框。
        """
        return self._as_bool(self.get('printer.confirm_poe_unplugged', True))

    @property
    def print_cleanup_after_print(self):
        """打印成功后是否清除设备本地数据（app.db / onvif 录像）

        由 config.yaml 的 printer.cleanup_after_print 控制，
        设为 false 时跳过清理步骤。
        """
        return self._as_bool(self.get('printer.cleanup_after_print', True))

    @property
    def battery_i2c_bus(self):
        try:
            return int(self.get('battery.i2c_bus', 0))
        except (TypeError, ValueError):
            return 0

    @property
    def battery_i2c_addr(self):
        """返回形如 '0x0b' 的地址字符串，供 i2cget 直接使用

        注意 YAML 会把 0x0b 解析成整数 11，这里统一还原成两位十六进制，
        保证下发的命令与产线文档中的 i2cget -y 0 0x0b ... 完全一致。
        """
        addr = self.get('battery.i2c_addr', 0x0b)
        if isinstance(addr, str):
            try:
                return f"0x{int(addr, 16):02x}"
            except (TypeError, ValueError):
                return '0x0b'
        try:
            return f"0x{int(addr):02x}"
        except (TypeError, ValueError):
            return '0x0b'

    @property
    def upload_retry_times(self):
        """数据上传失败重试次数（针对网关限流页等可恢复错误）"""
        try:
            return max(0, int(self.get('upload.retry_times', 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def upload_retry_delay(self):
        """数据上传重试间隔（秒）"""
        try:
            return max(0.0, float(self.get('upload.retry_delay', 2.0)))
        except (TypeError, ValueError):
            return 2.0


config = Config()
