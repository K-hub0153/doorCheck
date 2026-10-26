"""
打印模板引擎
从 config.yaml 读取模板配置，驱动 TSPLPrinter 打印
"""
from src.qrcode_print.scan_label_printer import TSPLPrinter
from src.qrcode_print.config import QRCodePrintConfig
from src.utils.logger import logger


class TemplateEngine:

    def __init__(self, config: QRCodePrintConfig):
        self.config = config
        self.printer = TSPLPrinter(config)
        self.templates = config.get('qrcode_print.templates', {})
        self.default_template = config.get('qrcode_print.template', 'default')
        logger.info(f"模板引擎初始化完成，默认模板: {self.default_template}")

    def select_template(self, qrcode_data: str) -> str:
        """根据二维码内容自动选择模板"""
        for name, tmpl in self.templates.items():
            prefix = tmpl.get('prefix', '')
            if prefix and qrcode_data.startswith(prefix):
                logger.info(f"自动选择模板: {name} (匹配前缀: {prefix})")
                return name
        logger.info(f"使用默认模板: {self.default_template}")
        return self.default_template

    def get_template_config(self, template_name: str) -> dict:
        """获取指定模板的配置字典"""
        return self.templates.get(template_name, {})

    def print_label(self, qrcode_data: str, template_name: str = None, copies: int = 1,
                    printer_name: str = None, protocol: str = None,
                    dpi: int = None) -> bool:
        """printer_name/protocol/dpi 由界面传入，透传给打印机；不传则用配置里的值"""
        try:
            if template_name is None:
                template_name = self.select_template(qrcode_data)

            template_config = self.get_template_config(template_name)

            for i in range(copies):
                success = self.printer.print_label(
                    device_sn=qrcode_data,
                    template=template_config if template_config else None,
                    printer_name=printer_name,
                    protocol=protocol,
                    dpi=dpi,
                )
                if not success:
                    logger.error(f"打印第 {i + 1}/{copies} 份失败")
                    return False
                logger.info(f"已打印第 {i + 1}/{copies} 份")

            return True

        except Exception as e:
            logger.error(f"打印失败: {e}")
            return False
