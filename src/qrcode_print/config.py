"""扫码打印独立配置。"""

import os
import yaml

from src.utils.config import Config
from src.utils.paths import get_app_dir


class QRCodePrintConfig(Config):
    """扫码打印独立配置，与门控、音箱配置分别加载和保存。"""

    _instance = None
    _config = None

    @property
    def config_path(self) -> str:
        return os.path.join(get_app_dir(), 'config', 'printer', 'config.yaml')

    def load_config(self, config_path: str = None):
        with open(config_path or self.config_path, 'r', encoding='utf-8') as file:
            self._config = yaml.safe_load(file)

    def get_print_templates(self) -> dict:
        """获取所有打印模板"""
        return self.get('qrcode_print.templates', {})

    def save_print_template(self, template_name: str, template_config: dict) -> bool:
        """保存单个打印模板到配置文件"""
        try:
            config_path = self.config_path
            if 'qrcode_print' not in self._config:
                self._config['qrcode_print'] = {}
            if 'templates' not in self._config['qrcode_print']:
                self._config['qrcode_print']['templates'] = {}
            self._config['qrcode_print']['templates'][template_name] = template_config
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
            return True
        except Exception as e:
            print(f"保存模板失败: {e}")
            return False

    def delete_print_template(self, template_name: str) -> bool:
        """删除指定模板"""
        try:
            config_path = self.config_path
            templates = self._config.get('qrcode_print', {}).get('templates', {})
            if template_name in templates:
                del templates[template_name]
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
            return True
        except Exception as e:
            print(f"删除模板失败: {e}")
            return False

    def save_default_template(self, template_name: str) -> bool:
        """设置默认使用的模板"""
        try:
            config_path = self.config_path
            if 'qrcode_print' not in self._config:
                self._config['qrcode_print'] = {}
            self._config['qrcode_print']['template'] = template_name
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
            return True
        except Exception as e:
            print(f"保存默认模板失败: {e}")
            return False

    def save_printer_settings(self, printer_name: str = None, protocol: str = None, zpl_dpi: int = None) -> bool:
        """保存打印机名称、协议和DPI到配置文件"""
        try:
            config_path = self.config_path
            if 'printer' not in self._config:
                self._config['printer'] = {}
            if printer_name is not None:
                self._config['printer']['printer_name'] = printer_name
            if protocol is not None:
                self._config['printer']['protocol'] = protocol
            if zpl_dpi is not None:
                self._config['printer']['zpl_dpi'] = zpl_dpi
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
            return True
        except Exception as e:
            print(f"保存打印机配置失败: {e}")
            return False

    def save_auto_print(self, enabled: bool) -> bool:
        try:
            self._config.setdefault('qrcode_print', {})['auto_print'] = enabled
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
            return True
        except Exception as e:
            print(f"保存自动打印设置失败: {e}")
            return False
