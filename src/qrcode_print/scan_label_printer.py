"""
TSPL 标签打印机实现
专用于扫码打印工具，支持模板可配置化
"""
import os
import platform
import re
from datetime import datetime
from typing import Union

from PIL import Image

from src.qrcode_print.label_background import (background_path, bitmap_bytes, label_pixels,
                                       load_background, render_text_fields)
from src.utils.logger import logger

IS_WINDOWS = platform.system() == 'Windows'

if IS_WINDOWS:
    try:
        import win32print
    except ImportError:
        logger.warning("win32print 未安装，将使用模拟模式")
        IS_WINDOWS = False


def has_chinese(text: str) -> bool:
    """
    检测文本中是否包含中文字符（汉字、中文标点等）

    包含的Unicode范围：
    - CJK 统一汉字: U+4E00-U+9FFF
    - CJK 扩展A: U+3400-U+4DBF
    - CJK 兼容: U+F900-U+FAFF
    - 中文标点: U+3000-U+303F, U+FF00-U+FFEF
    """
    pattern = re.compile(r'[一-鿿㐀-䶿豈-﫿　-〿＀-￯]')
    return bool(pattern.search(text))


class TSPLPrinter:

    DPI = 203  # TSPL打印机标准DPI（203 DPI）
    GAP_MM = 2
    SPEED = 4
    DENSITY = 10

    def __init__(self, config=None):
        # 默认打印机名
        if IS_WINDOWS:
            try:
                default_printer = win32print.GetDefaultPrinter()
            except Exception:
                default_printer = ""
        else:
            default_printer = "模拟打印机 (macOS/Linux)"

        self.protocol = 'tspl'
        self.zpl_dpi = 203  # ZPL打印机实际DPI，默认203
        self.chinese_font = "55"  # 默认中文字体
        self.config_path = getattr(config, 'config_path', None)

        if config is not None:
            if hasattr(config, 'get'):
                self.printer_name = config.get('printer.printer_name', default_printer) or default_printer
                self.protocol = config.get('printer.protocol', 'tspl') or 'tspl'
                self.zpl_dpi = int(config.get('printer.zpl_dpi', 203) or 203)
                self.chinese_font = config.get('printer.chinese_font', '55') or '55'
            elif isinstance(config, dict):
                printer_cfg = config.get('printer', {})
                self.printer_name = printer_cfg.get('printer_name', default_printer) or default_printer
                self.protocol = printer_cfg.get('protocol', 'tspl') or 'tspl'
                self.zpl_dpi = int(printer_cfg.get('zpl_dpi', 203) or 203)
                self.chinese_font = printer_cfg.get('chinese_font', '55') or '55'
            else:
                self.printer_name = default_printer
        else:
            self.printer_name = default_printer

        logger.info(f"[TSPLPrinter] 打印机={self.printer_name}, 协议={self.protocol}, ZPL DPI={self.zpl_dpi}, 中文字体={self.chinese_font}")

    @staticmethod
    def mm_to_dots(mm: float) -> int:
        return int(mm * TSPLPrinter.DPI / 25.4)

    # ------------------------------------------------------------------
    # 从模板配置构建 TSPL 指令（新接口）
    # ------------------------------------------------------------------

    def build_label_from_template(self, sn: str, template: dict) -> str:
        """根据模板配置字典生成 TSPL 指令"""
        width_mm = template.get('paper_width', 50)
        height_mm = template.get('paper_height', 30)

        tspl = "CLS\n"
        tspl += f"SIZE {width_mm} mm, {height_mm} mm\n"
        tspl += f"GAP {self.GAP_MM} mm, 0 mm\n"
        tspl += "DIRECTION 1\n"
        tspl += f"SPEED {self.SPEED}\n"
        tspl += f"DENSITY {self.DENSITY}\n"
        tspl += "REFERENCE 0,0\n"
        tspl += "SET TEAR ON\n"
        # 添加中文支持：设置代码页为 GBK/GB2312
        tspl += "CODEPAGE 936\n"
        tspl += "CLS\n"

        for field in template.get('fields', []):
            tspl += self._render_field(field, sn)

        tspl += "PRINT 1,1\n"
        tspl += "CUT\n"
        return tspl

    def build_label_with_background(self, sn: str, template: dict) -> bytes:
        """将底图或空白标签转换为 TSPL 位图，再叠加可变字段。"""
        path = (background_path(self.config_path, template)
                if template.get('background_image') else None)
        image = (load_background(path, template, self.DPI) if path else
                 Image.new('RGB', label_pixels(template, self.DPI), 'white'))
        fields = template.get('fields', [])
        render_text_fields(image, fields, sn, self.DPI)
        width_bytes, height, pixels = bitmap_bytes(image)
        prefix = self.build_label_from_template(sn, {**template, 'fields': []})
        prefix = prefix.split('PRINT 1,1\n', 1)[0]
        header = f'BITMAP 0,0,{width_bytes},{height},0,'.encode('ascii')
        variable_fields = ''.join(
            self._render_field(field, sn) for field in fields
            if field.get('type') in ('qrcode', 'barcode')
        )
        return prefix.encode('ascii') + header + pixels + b'\n' + variable_fields.encode('gbk') + b'PRINT 1,1\nCUT\n'

    def _render_field(self, field: dict, sn: str) -> str:
        ftype = field.get('type')
        x = field.get('x', 0)
        y = field.get('y', 0)

        if ftype == 'text':
            rotation = field.get('rotation', 0)
            xs = field.get('x_scale', 1)
            ys = field.get('y_scale', 1)
            label = field.get('label', '')
            content = f"{label}{sn}"

            # 如果内容包含中文，使用中文字体
            if has_chinese(content):
                return f'TEXT {x},{y},"{self.chinese_font}",{rotation},{xs},{ys},"{content}"\n'
            else:
                # 英文使用固定字体'1'
                return f'TEXT {x},{y},"1",{rotation},{xs},{ys},"{content}"\n'

        elif ftype == 'date':
            fmt = field.get('format', '%Y/%m/%d')
            date_str = datetime.now().strftime(fmt)
            rotation = field.get('rotation', 0)
            xs = field.get('x_scale', 1)
            ys = field.get('y_scale', 1)

            # 如果格式化后的日期包含中文，使用中文字体
            if has_chinese(fmt):
                return f'TEXT {x},{y},"{self.chinese_font}",{rotation},{xs},{ys},"{date_str}"\n'
            else:
                # 英文使用固定字体'1'
                return f'TEXT {x},{y},"1",{rotation},{xs},{ys},"{date_str}"\n'

        elif ftype == 'qrcode':
            cell_width = field.get('cell_width', 6)
            return f'QRCODE {x},{y},M,{cell_width},A,0,M2,S7,"{sn}"\n'

        elif ftype == 'barcode':
            height = field.get('height', 40)
            narrow = field.get('narrow', 2)
            wide = field.get('wide', 4)
            return f'BARCODE {x},{y},"128",{height},1,0,{narrow},{wide},"{sn}"\n'

        elif ftype == 'model':
            value = field.get('value', '')
            rotation = field.get('rotation', 0)
            xs = field.get('x_scale', 1)
            ys = field.get('y_scale', 1)

            # 检查是否包含中文
            if has_chinese(value):
                # 包含中文：使用配置的中文字体
                return f'TEXT {x},{y},"{self.chinese_font}",{rotation},{xs},{ys},"{value}"\n'
            else:
                # 纯英文/数字：使用固定字体'1'
                return f'TEXT {x},{y},"1",{rotation},{xs},{ys},"{value}"\n'

        logger.warning(f"未知字段类型: {ftype}")
        return ''

    # ------------------------------------------------------------------
    # 直接发送原始 TSPL 指令
    # ------------------------------------------------------------------

    def send_raw(self, tspl: Union[str, bytes], printer_name: str = None) -> bool:
        """printer_name：本次发送的目标打印机，不传则用实例属性（来自配置）"""
        logger.debug(f"打印指令长度: {len(tspl)}")

        target = printer_name or self.printer_name

        if IS_WINDOWS:
            try:
                # TSPL 中文用 GBK（兼容 GB2312 且支持更多汉字）；
                # ZPL 的 ^CI28 声明的是 UTF-8，按 GBK 发送会与声明冲突导致中文乱码。
                # 位图数据(bytes)已是最终字节，原样发送。
                if isinstance(tspl, bytes):
                    data = tspl
                elif tspl.startswith('^XA'):
                    data = tspl.encode('utf-8')
                else:
                    try:
                        data = tspl.encode('gbk')
                    except UnicodeEncodeError:
                        data = tspl.encode('utf-8')

                hPrinter = win32print.OpenPrinter(target)
                try:
                    win32print.StartDocPrinter(hPrinter, 1, ("Label", None, "RAW"))
                    win32print.StartPagePrinter(hPrinter)
                    win32print.WritePrinter(hPrinter, data)
                    win32print.EndPagePrinter(hPrinter)
                    win32print.EndDocPrinter(hPrinter)
                    logger.info(f"打印任务已发送到打印机 [{target}]")
                    logger.info(f"发送数据（前200字符）: {tspl[:200] if isinstance(tspl, str) else '<位图数据>'}")
                    return True
                finally:
                    win32print.ClosePrinter(hPrinter)
            except Exception as e:
                logger.error(f"打印失败: {e}")
                return False
        else:
            output_dir = "logs/print_commands"
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{output_dir}/print_{timestamp}.{'bin' if isinstance(tspl, bytes) else 'txt'}"
            if isinstance(tspl, bytes):
                with open(filename, 'wb') as f:
                    f.write(tspl)
            else:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(tspl)
            logger.info(f"[模拟模式] 打印指令已保存到: {filename}")
            logger.info(f"指令内容（前200字符）: {tspl[:200] if isinstance(tspl, str) else '<位图数据>'}")
            return True

    def build_label_from_template_zpl(self, sn: str, template: dict, dpi: int = None) -> str:
        """根据模板配置字典生成 ZPL 指令，坐标从 203 DPI 换算到实际 DPI"""
        actual_dpi = int(dpi) if dpi else self.zpl_dpi
        scale = actual_dpi / 203.0
        width_mm = template.get('paper_width', 50)
        height_mm = template.get('paper_height', 30)
        # 用实际 DPI 计算标签尺寸
        width_dots  = int(width_mm  * actual_dpi / 25.4)
        height_dots = int(height_mm * actual_dpi / 25.4)

        zpl = "^XA\n"
        zpl += f"^PW{width_dots}\n"
        zpl += f"^LL{height_dots}\n"
        zpl += "^LH0,0\n"
        # 设置中文编码支持
        zpl += "^CI28\n"  # UTF-8 编码

        for field in template.get('fields', []):
            zpl += self._render_field_zpl(field, sn, scale)

        zpl += "^XZ\n"
        return zpl

    def build_zpl_with_background(self, sn: str, template: dict, dpi: int = None) -> str:
        actual_dpi = int(dpi) if dpi else self.zpl_dpi
        path = (background_path(self.config_path, template)
                if template.get('background_image') else None)
        image = (load_background(path, template, actual_dpi) if path else
                 Image.new('RGB', label_pixels(template, actual_dpi), 'white'))
        fields = template.get('fields', [])
        render_text_fields(image, fields, sn, actual_dpi)
        width_bytes, height, pixels = bitmap_bytes(image)
        scale = actual_dpi / self.DPI
        width_dots = image.width
        zpl = f'^XA\n^PW{width_dots}\n^LL{height}\n^LH0,0\n'
        zpl += f'^FO0,0^GFA,{len(pixels)},{len(pixels)},{width_bytes},{pixels.hex().upper()}^FS\n'
        for field in fields:
            if field.get('type') in ('qrcode', 'barcode'):
                zpl += self._render_field_zpl(field, sn, scale)
        return zpl + '^XZ\n'

    def _render_field_zpl(self, field: dict, sn: str, scale: float = 1.0) -> str:
        ftype = field.get('type')
        # 模板坐标是 203 DPI dots，换算到实际 DPI
        x = int(field.get('x', 0) * scale)
        y = int(field.get('y', 0) * scale)
        # TSPL font 编号 → ZPL 基础点大小映射（203 DPI 基准）
        font_base = {'1': 24, '2': 32, '3': 40, '4': 56}

        if ftype in ('text', 'date', 'model'):
            base = font_base.get(str(field.get('font', '1')), 24)
            h = int(base * field.get('y_scale', 1) * scale)
            w = int(base * field.get('x_scale', 1) * scale)
            rotation_map = {0: 'N', 90: 'R', 180: 'I', 270: 'B'}
            rot = rotation_map.get(field.get('rotation', 0), 'N')

            # 获取内容
            if ftype == 'text':
                content = f"{field.get('label', '')}{sn}"
            elif ftype == 'date':
                content = datetime.now().strftime(field.get('format', '%Y/%m/%d'))
            else:
                content = field.get('value', '')

            # 检测是否包含中文
            if has_chinese(content):
                # 包含中文：使用 ^CW 指令加载中文字体
                # ZPL 打印机通常需要预装中文字体（如 simsun.ttf）
                # ^CW 命令格式：^CWa,d:o.x 其中 a 是字体标识符
                return f"^FO{x},{y}^A@{rot},{h},{w},E:SIMSUN.FNT^FD{content}^FS\n"
            else:
                # 纯英文/数字：使用标准字体
                return f"^FO{x},{y}^A0{rot},{h},{w}^FD{content}^FS\n"

        elif ftype == 'qrcode':
            cell_width = max(1, int(field.get('cell_width', 6) * scale))
            return f"^FO{x},{y}^BQN,2,{cell_width}^FDQA,{sn}^FS\n"

        elif ftype == 'barcode':
            height = int(field.get('height', 40) * scale)
            return f"^FO{x},{y}^BCN,{height},Y,N,N^FD{sn}^FS\n"

        logger.warning(f"未知字段类型: {ftype}")
        return ''

    # ------------------------------------------------------------------
    # 高层接口
    # ------------------------------------------------------------------

    def print_label(self, device_sn: str, template: dict = None, status=None,
                    printer_name: str = None, protocol: str = None,
                    dpi: int = None) -> bool:
        """打印标签

        printer_name/protocol/dpi 由界面传入，不传则用实例属性（来自配置）。
        """
        proto = protocol or self.protocol
        actual_dpi = int(dpi) if dpi else self.zpl_dpi

        has_background = template is not None and template.get('background_image')
        has_text = template is not None and any(
            field.get('type') in ('text', 'date', 'model')
            for field in template.get('fields', []))
        # 所有模板文字与编辑器共用字体和字号，避免内置字体忽略 font_size。
        use_bitmap = has_background or has_text
        if has_background and not self.config_path:
            raise ValueError('底图模板缺少配置文件路径')
        if proto == 'zpl':
            if template is not None:
                commands = (self.build_zpl_with_background(device_sn, template, actual_dpi) if use_bitmap
                            else self.build_label_from_template_zpl(device_sn, template, actual_dpi))
            else:
                # ZPL 无内置默认布局时降级为 TSPL
                commands = self._build_default_label(device_sn)
        else:
            if template is not None:
                commands = (self.build_label_with_background(device_sn, template) if use_bitmap
                            else self.build_label_from_template(device_sn, template))
            else:
                commands = self._build_default_label(device_sn)

        success = self.send_raw(commands, printer_name=printer_name)
        if success:
            logger.info(f"打印任务已发送 [{proto.upper()}]" if IS_WINDOWS else f"模拟打印完成 [{proto.upper()}]")
        else:
            logger.error("打印失败")
        return success

    def _build_default_label(self, device_sn: str) -> str:
        """内置默认布局（无模板配置时的后备）"""
        date_str = datetime.now().strftime("%Y/%m/%d")
        tspl = "CLS\n"
        tspl += "SIZE 50 mm, 30 mm\n"
        tspl += f"GAP {self.GAP_MM} mm, 0 mm\n"
        tspl += "DIRECTION 1\n"
        tspl += f"SPEED {self.SPEED}\n"
        tspl += f"DENSITY {self.DENSITY}\n"
        tspl += "REFERENCE 0,0\n"
        tspl += "SET TEAR ON\n"
        tspl += "CLS\n"
        tspl += f'TEXT 120,124,"1",0,1,1,"{date_str}"\n'
        tspl += f'TEXT 34,178,"1",0,1,1,"SN:{device_sn}"\n'
        tspl += f'QRCODE 245,69,M,6,A,0,M2,S7,"{device_sn}"\n'
        tspl += "PRINT 1,1\n"
        tspl += "CUT\n"
        return tspl

    @staticmethod
    def list_printers() -> list:
        """返回系统可用打印机列表"""
        if IS_WINDOWS:
            try:
                printers = win32print.EnumPrinters(
                    win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
                )
                return [p[2] for p in printers]
            except Exception as e:
                logger.error(f"获取打印机列表失败: {e}")
                return []
        return ["模拟打印机 (macOS/Linux)"]


# Alias for backward compatibility
LabelPrinter = TSPLPrinter
