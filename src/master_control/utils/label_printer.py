from PIL import Image, ImageDraw, ImageFont
import qrcode
import os
import sys
from datetime import datetime
from src.master_control.utils.config import config
from src.master_control.utils.logger import logger
from pathlib import Path

try:
    import win32print
    import win32ui
    from PIL import ImageWin
    WINDOWS_PRINT_AVAILABLE = True
except ImportError:
    WINDOWS_PRINT_AVAILABLE = False

try:
    import win32print as wp
    TSPL_PRINT_AVAILABLE = True
except ImportError:
    TSPL_PRINT_AVAILABLE = False

# Windows 系统常见字体回退列表
_FALLBACK_FONTS = [
    'msyh.ttc',      # 微软雅黑
    'simhei.ttf',    # 黑体
    'simsun.ttc',    # 宋体
    'arial.ttf',     # Arial
]


def _resolve_font(font_family: str, font_size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按配置字体 -> 系统常见字体 -> 默认字体的顺序尝试加载"""
    candidates = []
    if font_family:
        candidates.append(font_family)
    candidates.extend(_FALLBACK_FONTS)

    for name in candidates:
        try:
            font = ImageFont.truetype(name, font_size)
            return font
        except (OSError, IOError):
            continue

    logger.warning(f"无法加载任何字体，使用默认字体 (目标大小: {font_size})")
    return ImageFont.load_default(size=font_size)


class LabelPrinter:

    def __init__(self):
        self.printer_config = config.printer_config
        if not self.printer_config.get('enabled', False):
            logger.warning("打印功能未启用")
            return

        self.paper_width_mm = self.printer_config.get('paper_width', 50)
        self.paper_height_mm = self.printer_config.get('paper_height', 30)
        self.dpi = self.printer_config.get('dpi', 600)
        self.label_config = self.printer_config.get('label_config', {})

        self.width_px = int(self.label_config.get('labelWidth', self.paper_width_mm * self.dpi / 25.4))
        self.height_px = int(self.label_config.get('labelHeight', self.paper_height_mm * self.dpi / 25.4))

        logger.info(f"标签打印初始化: {self.paper_width_mm}x{self.paper_height_mm}mm @ {self.dpi}dpi")

    def create_label_image(self, sn: str, disk_size: str = None) -> Image.Image:
        """创建标签图像"""
        image = Image.new('RGB', (self.width_px, self.height_px), 'white')
        draw = ImageDraw.Draw(image)

        if self.label_config.get('drawBorder', True):
            draw.rectangle([(0, 0), (self.width_px - 1, self.height_px - 1)],
                           outline='black', width=3)

        self._draw_sn_text(draw, sn)
        self._draw_date_text(draw)
        self._draw_qrcode(image, sn)
        if disk_size:
            self._draw_disk_size_text(draw, disk_size)

        return image

    def _draw_sn_text(self, draw: ImageDraw.ImageDraw, sn: str):
        """绘制 SN 文本"""
        sn_config = self.label_config.get('snTextConfig', {})
        x = sn_config.get('x', 115)
        y = sn_config.get('y', 503)
        font_size = sn_config.get('fontSize', 32)
        font_family = sn_config.get('fontFamily', '')
        bold = sn_config.get('fontBold', False)

        font = _resolve_font(font_family, font_size, bold)
        stroke_w = max(1, font_size // 15) if bold else 0

        draw.text((x, y), f"{sn}", fill='black', font=font,
                  stroke_width=stroke_w, stroke_fill='black')

    def _draw_disk_size_text(self, draw: ImageDraw.ImageDraw, disk_size: str):
        """绘制磁盘大小文本（在二维码下方）"""
        disk_config = self.label_config.get('diskSizeConfig', {})
        x = disk_config.get('x', 960)
        y = disk_config.get('y', 642)
        font_size = disk_config.get('fontSize', 32)
        font_family = disk_config.get('fontFamily', '')
        bold = disk_config.get('fontBold', False)

        font = _resolve_font(font_family, font_size, bold)
        stroke_w = max(1, font_size // 15) if bold else 0

        draw.text((x, y), disk_size, fill='black', font=font, anchor='mt',
                  stroke_width=stroke_w, stroke_fill='black')

    def _draw_date_text(self, draw: ImageDraw.ImageDraw):
        """绘制日期文本"""
        date_config = self.label_config.get('dateConfig', {})
        x = date_config.get('x', 371)
        y = date_config.get('y', 344)
        font_size = date_config.get('fontSize', 32)
        font_family = date_config.get('fontFamily', '')
        bold = date_config.get('fontBold', False)

        date_str = datetime.now().strftime('%Y/%m/%d')

        font = _resolve_font(font_family, font_size, bold)
        stroke_w = max(1, font_size // 15) if bold else 0
        draw.text((x, y), date_str, fill='black', font=font,
                  stroke_width=stroke_w, stroke_fill='black')

    def _draw_qrcode(self, image: Image.Image, sn: str):
        """绘制二维码"""
        qr_config = self.label_config.get('qrCodeConfig', {})
        x = qr_config.get('x', 740)
        y = qr_config.get('y', 181)
        size = qr_config.get('width', 441)

        qr = qrcode.QRCode(version=1, box_size=10, border=1)
        qr.add_data(sn)
        qr.make(fit=True)

        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_img = qr_img.resize((size, size))

        image.paste(qr_img, (x, y))

    def print_label(self, sn: str, disk_size: str = None) -> bool:
        """打印标签：生成图像并发送到打印机"""
        if not self.printer_config.get('enabled', False):
            logger.info("打印功能未启用")
            return False

        try:
            logger.info(f"开始打印标签: {sn}")

            label_image = self.create_label_image(sn, disk_size)

            if os.name == 'nt':
                return self._print_windows(label_image, sn)
            else:
                logger.warning("当前系统不支持打印，仅保存图片")
                return self._save_image(label_image, sn)

        except Exception as e:
            logger.error(f"打印失败: {e}")
            return False

    def create_label(self, sn: str, disk_size: str = None, output_path: str = None) -> str:
        """创建标签图片并保存到文件（兼容旧接口）"""
        if not self.printer_config.get('enabled', False):
            logger.warning("打印功能未启用，无法创建标签")
            return None

        label_image = self.create_label_image(sn, disk_size)

        if output_path is None:
            output_dir = self._get_output_dir()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(output_dir, f"label_{sn}_{timestamp}.png")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        label_image.save(output_path)

        logger.info(f"标签已生成: {output_path}")
        return output_path

    def _print_windows(self, image: Image.Image, sn: str = "temp") -> bool:
        """Windows 平台物理打印"""
        if not WINDOWS_PRINT_AVAILABLE:
            logger.warning("Windows打印模块未安装(pywin32)，保存图片代替")
            return self._save_image(image, sn)

        try:
            import win32con

            printer_name = win32print.GetDefaultPrinter()
            logger.info(f"使用打印机: {printer_name}")

            hdc = win32ui.CreateDC()
            hdc.CreatePrinterDC(printer_name)

            # 获取打印机真实 DPI
            printer_dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
            printer_dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
            logger.info(f"打印机DPI: {printer_dpi_x}x{printer_dpi_y}")

            # 按打印机真实 DPI 计算纸张输出区域（设备单位）
            paper_w = int(self.paper_width_mm / 25.4 * printer_dpi_x)
            paper_h = int(self.paper_height_mm / 25.4 * printer_dpi_y)
            logger.info(f"输出区域: {paper_w}x{paper_h} dots")

            hdc.StartDoc("Label Print")
            hdc.StartPage()

            dib = ImageWin.Dib(image)
            dib.draw(hdc.GetHandleOutput(), (0, 0, paper_w, paper_h))

            hdc.EndPage()
            hdc.EndDoc()
            hdc.DeleteDC()

            logger.info("打印成功")
            return True
        except Exception as e:
            logger.error(f"Windows打印失败: {e}")
            return False

    def _save_image(self, image: Image.Image, sn: str) -> bool:
        """保存标签图片到本地"""
        try:
            output_dir = self._get_output_dir()
            os.makedirs(output_dir, exist_ok=True)

            filename = f"label_{sn}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            filepath = os.path.join(output_dir, filename)

            image.save(filepath)
            logger.info(f"标签图片已保存: {filepath}")
            return True
        except Exception as e:
            logger.error(f"保存图片失败: {e}")
            return False

    @staticmethod
    def _get_output_dir() -> str:
        """获取输出目录"""
        from .paths import runtime_dir
        output_dir = str(runtime_dir() / 'reports')
        os.makedirs(output_dir, exist_ok=True)
        return output_dir


class TSPLPrinter:
    """基于 TSPL 协议的标签打印机"""

    def __init__(self):
        self.printer_config = config.printer_config
        if not self.printer_config.get('enabled', False):
            logger.warning("打印功能未启用")
            return

        # 基础配置
        self.printer_name = None
        if TSPL_PRINT_AVAILABLE:
            try:
                self.printer_name = wp.GetDefaultPrinter()
            except Exception as e:
                logger.warning(f"无法获取默认打印机: {e}")

        self.paper_width_mm = self.printer_config.get('paper_width', 50)
        self.paper_height_mm = self.printer_config.get('paper_height', 30)
        self.dpi = self.printer_config.get('dpi', 600)

        # TSPL 打印参数
        self.density = 10
        self.speed = 4
        self.gap_mm = 2

        # 从配置加载 TSPL 布局
        tspl_layout = self.printer_config.get('tspl_layout', {})

        # 边框配置
        border = tspl_layout.get('border', {})
        self._border_x1 = border.get('x1', 2)
        self._border_y1 = border.get('y1', 2)
        self._border_x2 = border.get('x2', 398)
        self._border_y2 = border.get('y2', 238)
        self._border_thickness = border.get('thickness', 2)

        # 日期配置
        date_cfg = tspl_layout.get('date', {})
        self._date_x = date_cfg.get('x', 120)
        self._date_y = date_cfg.get('y', 124)
        self._date_font = date_cfg.get('font', '1')
        self._date_rotation = date_cfg.get('rotation', 0)
        self._date_x_scale = date_cfg.get('x_scale', 1)
        self._date_y_scale = date_cfg.get('y_scale', 1)

        # SN 配置
        sn_cfg = tspl_layout.get('sn', {})
        self._sn_x = sn_cfg.get('x', 34)
        self._sn_y = sn_cfg.get('y', 178)
        self._sn_font = sn_cfg.get('font', '1')
        self._sn_rotation = sn_cfg.get('rotation', 0)
        self._sn_x_scale = sn_cfg.get('x_scale', 1)
        self._sn_y_scale = sn_cfg.get('y_scale', 1)
        self._sn_max_width = sn_cfg.get('max_width', 0)  # 0 = 不限制

        # 二维码配置
        qr_cfg = tspl_layout.get('qrcode', {})
        self._qr_x = qr_cfg.get('x', 245)
        self._qr_y = qr_cfg.get('y', 69)
        self._qr_cell_width = qr_cfg.get('cell_width', 6)

        # 硬盘大小配置
        disk_cfg = tspl_layout.get('disk_size', {})
        self._disk_x = disk_cfg.get('x', 245)
        self._disk_y = disk_cfg.get('y', 220)
        self._disk_font = disk_cfg.get('font', '1')
        self._disk_rotation = disk_cfg.get('rotation', 0)
        self._disk_x_scale = disk_cfg.get('x_scale', 1)
        self._disk_y_scale = disk_cfg.get('y_scale', 1)
        self._disk_align = disk_cfg.get('align', 'center')
        self._disk_width = disk_cfg.get('width', 145)  # QR 码区域宽度，居中计算用

        logger.info(f"TSPL 标签打印初始化: {self.paper_width_mm}x{self.paper_height_mm}mm @ {self.dpi}dpi")

    def _draw_border(self):
        """生成边框 TSPL 指令"""
        x1, y1 = self._border_x1, self._border_y1
        x2, y2 = self._border_x2, self._border_y2
        t = self._border_thickness
        return f"BOX {x1},{y1},{x2},{y2},{t}\n"

    def _draw_sn(self, device_sn: str):
        """生成 SN 文本 TSPL 指令，自动缩小 x_scale 以适应 max_width"""
        x, y = self._sn_x, self._sn_y
        font = self._sn_font
        r, ys = self._sn_rotation, self._sn_y_scale
        text = f"{device_sn}"
        xs = self._sn_x_scale
        # TSPL 字体"1"每字符基础宽约 8 dots，x_scale 成比例缩放
        if self._sn_max_width > 0:
            char_w = 8  # dots per char at x_scale=1
            needed = len(text) * char_w
            if needed > self._sn_max_width:
                xs = max(1, self._sn_max_width // (len(text) * char_w))
        return f'TEXT {x},{y},"{font}",{r},{xs},{ys},"{text}"\n'

    def _draw_date(self):
        """生成日期文本 TSPL 指令"""
        date_str = datetime.now().strftime("%Y/%m/%d")
        x, y = self._date_x, self._date_y
        font = self._date_font
        r, xs, ys = self._date_rotation, self._date_x_scale, self._date_y_scale
        return f'TEXT {x},{y},"{font}",{r},{xs},{ys},"{date_str}"\n'

    def _draw_qrcode(self, device_sn: str):
        """生成二维码 TSPL 指令"""
        x, y = self._qr_x, self._qr_y
        cw = self._qr_cell_width
        return f'QRCODE {x},{y},M,{cw},A,0,M2,S7,"{device_sn}"\n'

    def _draw_disk_size(self, disk_size: str):
        """生成硬盘大小文本 TSPL 指令，支持居中对齐"""
        font = self._disk_font
        r, xs, ys = self._disk_rotation, self._disk_x_scale, self._disk_y_scale
        y = self._disk_y
        x = self._disk_x
        # 居中：在 QR 码区域内水平居中
        if self._disk_align == 'center' and self._disk_width > 0:
            char_w = 8 * xs
            text_w = len(disk_size) * char_w
            x = self._disk_x + (self._disk_width - text_w) // 2
        return f'TEXT {x},{y},"{font}",{r},{xs},{ys},"{disk_size}"\n'

    def build_label(self, device_sn: str, disk_size: str = None):
        """构建完整的 TSPL 指令"""
        tspl = "CLS\n"
        tspl += f"SIZE {self.paper_width_mm} mm,{self.paper_height_mm} mm\n"
        tspl += f"GAP {self.gap_mm} mm,0 mm\n"
        tspl += "DIRECTION 1\n"
        tspl += f"SPEED {self.speed}\n"
        tspl += f"DENSITY {self.density}\n"
        tspl += "REFERENCE 0,0\n"
        tspl += "SET TEAR ON\n"
        tspl += "CLS\n"

        # 绘制内容
        # tspl += self._draw_border()  # 可选：绘制边框
        tspl += self._draw_date()
        tspl += self._draw_sn(device_sn)
        tspl += self._draw_qrcode(device_sn)
        if disk_size:
            tspl += self._draw_disk_size(disk_size)

        tspl += "PRINT 1,1\n"
        tspl += "CUT\n"
        return tspl

    def send_raw(self, tspl: str):
        """发送原始 TSPL 指令到打印机"""
        if not TSPL_PRINT_AVAILABLE or not self.printer_name:
            logger.warning("TSPL 打印不可用，仅输出指令")
            logger.info("\n生成 TSPL 指令如下：")
            logger.info("=" * 60)
            logger.info(tspl)
            logger.info("=" * 60)
            return False

        try:
            logger.info("\n生成 TSPL 指令如下：")
            logger.info("=" * 60)
            logger.info(tspl)
            logger.info("=" * 60)

            data = tspl.encode('utf-8')
            hPrinter = wp.OpenPrinter(self.printer_name)
            try:
                wp.StartDocPrinter(hPrinter, 1, ("Label", None, "RAW"))
                wp.StartPagePrinter(hPrinter)
                wp.WritePrinter(hPrinter, data)
                wp.EndPagePrinter(hPrinter)
                wp.EndDocPrinter(hPrinter)
                logger.info("TSPL 指令发送成功")
                return True
            finally:
                wp.ClosePrinter(hPrinter)
        except Exception as e:
            logger.error(f"发送 TSPL 指令失败: {e}")
            return False

    def print_label(self, sn: str, disk_size: str = None) -> bool:
        """打印标签（兼容 LabelPrinter 接口）"""
        if not self.printer_config.get('enabled', False):
            logger.info("打印功能未启用")
            return False

        try:
            logger.info(f"开始打印标签 (TSPL): {sn}")
            tspl = self.build_label(sn, disk_size)
            success = self.send_raw(tspl)
            if success:
                logger.info("打印完成")
            return success
        except Exception as e:
            logger.error(f"打印失败: {e}")
            return False

    def create_label(self, sn: str, disk_size: str = None, output_path: str = None) -> str:
        """创建标签（保存 TSPL 指令到文件，兼容 LabelPrinter 接口）"""
        if not self.printer_config.get('enabled', False):
            logger.warning("打印功能未启用，无法创建标签")
            return None

        try:
            tspl = self.build_label(sn, disk_size)

            if output_path is None:
                output_dir = self._get_output_dir()
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = os.path.join(output_dir, f"label_{sn}_{timestamp}.tspl")

            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(tspl)

            logger.info(f"TSPL 指令已保存: {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"保存 TSPL 指令失败: {e}")
            return None

    @staticmethod
    def _get_output_dir() -> str:
        """获取输出目录"""
        from .paths import runtime_dir
        output_dir = str(runtime_dir() / 'reports')
        os.makedirs(output_dir, exist_ok=True)
        return output_dir
