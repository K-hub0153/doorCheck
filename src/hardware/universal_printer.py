import os
import sys
from datetime import datetime
from typing import Optional

import qrcode
from PIL import Image, ImageDraw, ImageFont

# 使用统一的 logger，确保日志能被记录
from ..utils.logger import logger

# win32print 只在 Windows 存在，非 Windows 下置 None，由调用处报错
if sys.platform == 'win32':
    import win32print
else:
    win32print = None


class UniversalPrinter:
    """
    通用标签打印机，支持 ZPL (Zebra) 和 TSPL (Xprinter/TSC) 协议
    根据打印机名称自动检测协议，或手动指定
    """

    # 协议检测规则（与配置对话框保持一致）
    _PROTOCOL_RULES = [
        ('zpl',  ['zdesigner', 'zebra', 'zpl']),
        ('tspl', ['xprinter', 'xp-t', 'xp-n', 'xp-d', 'tsc', 'tspl']),
    ]

    def __init__(self, config=None):
        """
        初始化打印机
        
        Args:
            config: Config对象或字典，包含printer配置
        """
        self.printer_name = None
        self.dpi = 600
        # zpl_layout 里的坐标/字号按此 DPI 标定，实际渲染时按 dpi/design_dpi 等比缩放。
        # 换打印机只需改 dpi，不用重标坐标。
        self.design_dpi = 600
        self.protocol = None
        self.paper_width = 50
        self.paper_height = 30
        self.tspl_layout = {}
        self.zpl_layout = {}

        # 从配置加载参数
        if config is not None:
            self._load_config(config)

        # 如果没有配置打印机名称，使用系统默认
        if not self.printer_name:
            try:
                self.printer_name = win32print.GetDefaultPrinter()
                logger.info(f"使用系统默认打印机: {self.printer_name}")
            except Exception as e:
                logger.error(f"获取默认打印机失败: {e}")
                self.printer_name = ""

        # 自动检测协议
        if not self.protocol or self.protocol == 'auto':
            self.protocol = self._detect_protocol()

        logger.info(f"打印机初始化: name={self.printer_name}, protocol={self.protocol}, dpi={self.dpi}")

    def _load_config(self, config):
        """从 Config 对象或字典加载配置"""
        # 区分 Config 对象和普通字典
        if hasattr(config, 'load_config') or hasattr(config, 'printer_config'):
            # Config 对象 - 使用点号路径
            self.printer_name = config.get('printer.printer_name', None)
            self.dpi = config.get('printer.dpi', 600)
            self.design_dpi = config.get('printer.design_dpi', 600) or 600
            self.protocol = config.get('printer.protocol', None)
            self.paper_width = config.get('printer.paper_width', 50)
            self.paper_height = config.get('printer.paper_height', 30)
            self.tspl_layout = config.get('printer.tspl_layout', {})
            self.zpl_layout = config.get('printer.zpl_layout', {})
        elif isinstance(config, dict):
            # 普通字典
            printer_cfg = config.get('printer', {})
            self.printer_name = printer_cfg.get('printer_name', None)
            self.dpi = printer_cfg.get('dpi', 600)
            self.design_dpi = printer_cfg.get('design_dpi', 600) or 600
            self.protocol = printer_cfg.get('protocol', None)
            self.paper_width = printer_cfg.get('paper_width', 50)
            self.paper_height = printer_cfg.get('paper_height', 30)
            self.tspl_layout = printer_cfg.get('tspl_layout', {})
            self.zpl_layout = printer_cfg.get('zpl_layout', {})

    def _detect_protocol(self) -> str:
        """根据打印机名称检测协议"""
        if self.printer_name:
            name_lower = self.printer_name.lower()
            for protocol, keywords in self._PROTOCOL_RULES:
                for kw in keywords:
                    if kw in name_lower:
                        logger.info(f"检测到 {protocol.upper()} 协议（关键字: '{kw}'）")
                        return protocol
        logger.warning(f"无法检测协议（打印机: '{self.printer_name}'），默认使用 ZPL")
        return 'zpl'

    def print_label(self, sn: str, capacity: str = '', status: str = None) -> bool:
        """
        打印标签
        
        Args:
            sn: 设备序列号
            capacity: 容量信息（可选）
            status: 状态（可选）
            
        Returns:
            打印是否成功
        """
        try:
            if self.protocol == 'zpl':
                return self._print_zpl(sn, capacity)
            else:
                return self._print_tspl(sn, capacity)
        except Exception as e:
            logger.error(f"打印失败: {e}")
            return False

    def _build_tspl_commands(self, sn: str, capacity: str = '') -> str:
        """构建 TSPL 打印指令"""
        tspl = "CLS\n"
        tspl += f"SIZE {self.paper_width} mm, {self.paper_height} mm\n"
        tspl += "GAP 2 mm, 0 mm\n"
        tspl += "DIRECTION 1\n"
        tspl += "SPEED 4\n"
        tspl += "DENSITY 10\n"
        tspl += "REFERENCE 0,0\n"
        tspl += "SET TEAR ON\n"
        tspl += "CLS\n"

        # 从配置读取布局
        date_cfg = self.tspl_layout.get('date', {})
        date_x = date_cfg.get('x', 120)
        date_y = date_cfg.get('y', 124)
        date_font = date_cfg.get('font', '1')
        date_rotation = date_cfg.get('rotation', 0)
        date_x_scale = date_cfg.get('x_scale', 1)
        date_y_scale = date_cfg.get('y_scale', 1)

        sn_cfg = self.tspl_layout.get('sn', {})
        sn_x = sn_cfg.get('x', 34)
        sn_y = sn_cfg.get('y', 178)
        sn_font = sn_cfg.get('font', '1')
        sn_rotation = sn_cfg.get('rotation', 0)
        sn_x_scale = sn_cfg.get('x_scale', 1)
        sn_y_scale = sn_cfg.get('y_scale', 1)

        qr_cfg = self.tspl_layout.get('qrcode', {})
        qr_x = qr_cfg.get('x', 245)
        qr_y = qr_cfg.get('y', 69)
        
        # QR码尺寸计算：目标15mm
        if 'cell_width' in qr_cfg:
            qr_cell_width = qr_cfg['cell_width']
        else:
            target_dots = int(15 * self.dpi / 25.4)
            qr_cell_width = max(1, (target_dots + 24) // 25)

        # 日期
        date_str = datetime.now().strftime("%Y/%m/%d")
        tspl += f'TEXT {date_x},{date_y},"{date_font}",{date_rotation},{date_x_scale},{date_y_scale},"{date_str}"\n'

        # 序列号
        tspl += f'TEXT {sn_x},{sn_y},"{sn_font}",{sn_rotation},{sn_x_scale},{sn_y_scale},"SN:{sn}"\n'

        # QR码
        tspl += f'QRCODE {qr_x},{qr_y},M,{qr_cell_width},A,0,M2,S7,"{sn}"\n'

        tspl += "PRINT 1,1\n"
        
        return tspl

    def _build_zpl_commands(self, sn: str, capacity: str = '') -> str:
        """构建 ZPL 打印指令

        支持两种渲染模式（由 zpl_layout.render_mode 决定）：
        - bitmap: 用 PIL 在电脑端渲染日期/SN(指定字体)和二维码为位图，
                  转 ^GFA 发送。字体和二维码完全可控，不受打印机内置字体限制。
        - native: 使用打印机内置字体(^A/^BQN)，兼容旧行为。
        """
        render_mode = self.zpl_layout.get('render_mode', 'native')
        if render_mode == 'bitmap':
            return self._build_zpl_bitmap(sn, capacity)
        return self._build_zpl_native(sn, capacity)

    def _build_zpl_native(self, sn: str, capacity: str = '') -> str:
        """构建 ZPL 打印指令（打印机内置字体模式）"""
        label_w = int(self.paper_width * self.dpi / 25.4)
        label_h = int(self.paper_height * self.dpi / 25.4)

        zpl = "^XA\n"  # 标签开始
        zpl += f"^PW{label_w}\n"  # 标签宽度（点数）
        zpl += f"^LL{label_h}\n"  # 标签长度（点数）

        # 边框（与 TSPL 模板保持一致，绘制外框）
        border_cfg = self.zpl_layout.get('border', {})
        if border_cfg.get('enabled', False):
            b_thickness = border_cfg.get('thickness', 6)
            b_x = border_cfg.get('x', 6)
            b_y = border_cfg.get('y', 6)
            b_w = border_cfg.get('width', label_w - b_x * 2)
            b_h = border_cfg.get('height', label_h - b_y * 2)
            zpl += f"^FO{b_x},{b_y}\n"
            zpl += f"^GB{b_w},{b_h},{b_thickness}^FS\n"

        # 从配置读取布局
        qr_cfg = self.zpl_layout.get('qrcode', {})
        qr_x = qr_cfg.get('x', 724)
        qr_y = qr_cfg.get('y', 204)
        # 纠错等级（与 TSPL 模板的 M 级保持一致）
        qr_ecc = qr_cfg.get('error_correction', 'M')

        # QR码放大倍数：目标15mm
        if 'magnification' in qr_cfg:
            qr_mag = qr_cfg['magnification']
        else:
            target_dots = int(15 * self.dpi / 25.4)
            qr_mag = max(1, (target_dots + 24) // 25)

        date_cfg = self.zpl_layout.get('date', {})
        date_x = date_cfg.get('x', 354)
        date_y = date_cfg.get('y', 366)
        date_font = date_cfg.get('font', 'D')
        date_font_h = date_cfg.get('font_height', 35)
        date_font_w = date_cfg.get('font_width', 24)

        sn_cfg = self.zpl_layout.get('sn', {})
        sn_x = sn_cfg.get('x', 100)
        sn_y = sn_cfg.get('y', 526)
        sn_font = sn_cfg.get('font', 'D')
        sn_font_h = sn_cfg.get('font_height', 35)
        sn_font_w = sn_cfg.get('font_width', 24)

        # 加粗宽度(点)：ZPL 内置字体无加粗指令，用偏移重复打印模拟
        default_bold = self.zpl_layout.get('bold_width', 2)

        def draw_bold_field(x, y, font, font_h, font_w, text, bold_width):
            out = ""
            offsets = [(0, 0)]
            if bold_width and bold_width > 0:
                b = int(bold_width)
                offsets += [(dx, dy)
                            for dx in range(0, b + 1)
                            for dy in range(0, b + 1)
                            if (dx, dy) != (0, 0)]
            for dx, dy in offsets:
                out += f"^FO{x + dx},{y + dy}\n"
                out += f"^A{font}N,{font_h},{font_w}\n"
                out += f"^FD{text}^FS\n"
            return out

        # 日期
        # 使用 ^A<font> 指定内置点阵字体(A~H)
        date_str = datetime.now().strftime("%Y/%m/%d")
        zpl += draw_bold_field(date_x, date_y, date_font, date_font_h, date_font_w,
                               date_str, date_cfg.get('bold_width', default_bold))

        # 序列号
        zpl += draw_bold_field(sn_x, sn_y, sn_font, sn_font_h, sn_font_w,
                               f"SN: {sn}", sn_cfg.get('bold_width', default_bold))

        # QR码（^FD 首字符为纠错等级，与 TSPL 模板保持一致）
        zpl += f"^FO{qr_x},{qr_y}\n"
        zpl += f"^BQN,2,{qr_mag}\n"
        zpl += f"^FD{qr_ecc}A,{sn}^FS\n"

        # 容量信息（如果提供且配置中有）
        if capacity:
            disk_cfg = self.zpl_layout.get('disk_size', {})
            if disk_cfg:
                disk_x = disk_cfg.get('x', 827)
                disk_y = disk_cfg.get('y', 649)
                disk_font_h = disk_cfg.get('font_height', 75)
                disk_font_w = disk_cfg.get('font_width', 75)
                zpl += f"^FO{disk_x},{disk_y}\n"
                zpl += f"^CF0,{disk_font_h},{disk_font_w}\n"
                zpl += f"^FD{capacity}^FS\n"

        zpl += "^XZ\n"  # 标签结束
        return zpl

    def _resolve_font_path(self) -> Optional[str]:
        """解析字体文件路径（支持相对工程根目录或绝对路径）"""
        font_path = self.zpl_layout.get('font_path')
        if not font_path:
            return None
        if os.path.isabs(font_path) and os.path.exists(font_path):
            return font_path
        # 相对路径：以工程根目录(本文件上溯三级)为基准
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidate = os.path.join(project_root, font_path)
        if os.path.exists(candidate):
            return candidate
        logger.warning(f"字体文件未找到: {font_path} (已尝试 {candidate})")
        return None

    def _render_label_bitmap(self, sn: str):
        """用 PIL 渲染整张标签的可变内容(日期/SN/二维码)为 1-bit 位图

        返回 PIL Image(模式 '1')。zpl_layout 坐标按 design_dpi 标定，
        渲染时按 dpi/design_dpi 缩放到当前打印机分辨率。
        仅渲染可变字段，预印内容由标签纸自带。
        """
        label_w = int(self.paper_width * self.dpi / 25.4)
        label_h = int(self.paper_height * self.dpi / 25.4)

        # 布局坐标按 design_dpi 标定，换算到当前打印机 DPI
        scale = self.dpi / self.design_dpi if self.design_dpi else 1.0
        if scale != 1.0:
            logger.info(
                f"标签布局按 DPI 缩放: {self.design_dpi} -> {self.dpi} "
                f"(系数 {scale:.3f}), 画布 {label_w}x{label_h}"
            )

        def s(value, minimum=1):
            """按 DPI 比例缩放尺寸/坐标，minimum 防止塌缩到 0"""
            return max(minimum, int(round(value * scale)))

        # 白底(1-bit: 255=白/不打印, 0=黑/打印)
        img = Image.new('1', (label_w, label_h), 1)
        draw = ImageDraw.Draw(img)

        font_path = self._resolve_font_path()

        def load_font(size):
            if font_path:
                try:
                    return ImageFont.truetype(font_path, size)
                except Exception as e:
                    logger.error(f"加载字体失败({font_path}): {e}")
            return ImageFont.load_default()

        # 加粗宽度(点)：0=不加粗，越大越粗。可全局设置，也可按字段单独覆盖
        default_bold = self.zpl_layout.get('bold_width', 2)

        def scaled_bold(cfg):
            """加粗宽度随 DPI 缩放；配置为 0 时保持不加粗"""
            raw = cfg.get('bold_width', default_bold)
            return s(raw, 0) if raw else 0

        def draw_bold_text(xy, text, font, bold_width):
            """描边方式实现加粗，热敏/热转印下比单笔画更清晰"""
            if bold_width and bold_width > 0:
                draw.text(xy, text, font=font, fill=0,
                          stroke_width=int(bold_width), stroke_fill=0)
            else:
                draw.text(xy, text, font=font, fill=0)

        # 日期
        date_cfg = self.zpl_layout.get('date', {})
        date_str = datetime.now().strftime("%Y/%m/%d")
        dfont = load_font(s(date_cfg.get('font_size', 40)))
        draw_bold_text((s(date_cfg.get('x', 300), 0), s(date_cfg.get('y', 388), 0)),
                       date_str, dfont, scaled_bold(date_cfg))

        # SN
        sn_cfg = self.zpl_layout.get('sn', {})
        sfont = load_font(s(sn_cfg.get('font_size', 40)))
        draw_bold_text((s(sn_cfg.get('x', 77), 0), s(sn_cfg.get('y', 512), 0)),
                       f"SN: {sn}", sfont, scaled_bold(sn_cfg))

        # 二维码：外框尺寸恒定，SN 变长只增加模块密度，不改变二维码大小。
        # 不能用"每模块固定像素"(box_size)：那样 QR 版本每升一级外框就放大 19%，
        # 预印标签上的白框会放不下。这里按目标物理尺寸反算模块大小。
        qr_cfg = self.zpl_layout.get('qrcode', {})
        ecc_map = {
            'L': qrcode.constants.ERROR_CORRECT_L,
            'M': qrcode.constants.ERROR_CORRECT_M,
            'Q': qrcode.constants.ERROR_CORRECT_Q,
            'H': qrcode.constants.ERROR_CORRECT_H,
        }
        ecc = ecc_map.get(qr_cfg.get('error_correction', 'M'),
                          qrcode.constants.ERROR_CORRECT_M)

        qr_x = s(qr_cfg.get('x', 770), 0)
        qr_y = s(qr_cfg.get('y', 300), 0)

        avail_w, avail_h = label_w - qr_x, label_h - qr_y
        if avail_w <= 0 or avail_h <= 0:
            logger.error(f"二维码起点({qr_x},{qr_y})已在画布({label_w}x{label_h})外，跳过绘制")
            return img

        target_px = self._qr_target_px(qr_cfg, scale)
        if target_px > min(avail_w, avail_h):
            clamped = min(avail_w, avail_h)
            logger.warning(
                f"二维码目标 {target_px} 点超出可用区域 {avail_w}x{avail_h}，收到 {clamped} 点"
            )
            target_px = clamped

        img.paste(self._render_qr_fixed_size(sn, target_px, ecc), (qr_x, qr_y))

        return img

    def _qr_target_px(self, qr_cfg: dict, scale: float) -> int:
        """二维码目标外框边长(点)

        以 size_mm 为准，与 DPI 无关。仅配了旧的 box_size 时按版本1(21模块)折算，
        保证老配置打出来的实际尺寸不变，不会因为升级而撑破预印白框。
        """
        if 'size_mm' in qr_cfg:
            return max(1, int(round(float(qr_cfg['size_mm']) * self.dpi / 25.4)))
        if 'box_size' in qr_cfg:
            legacy = max(1, int(round(float(qr_cfg['box_size']) * scale))) * 21
            logger.warning(
                f"zpl_layout.qrcode.box_size 已废弃(每模块像素数，SN 变长会整体放大)，"
                f"按版本1折算为 {legacy} 点，建议改用 size_mm"
            )
            return legacy
        return max(1, int(round(15 * self.dpi / 25.4)))

    @staticmethod
    def _render_qr_fixed_size(data: str, size_px: int, error_correction):
        """把二维码光栅化进固定 size_px 的方框

        模块边界按比例取整后落在像素网格上：外框恒为 size_px，相邻模块边长最多差 1 点，
        且全程不做位图重采样，模块边缘保持锐利（resize 会让边缘错位、掉扫码率）。
        """
        qr = qrcode.QRCode(error_correction=error_correction, border=0)
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n = len(matrix)

        if size_px < n:
            logger.error(f"二维码目标 {size_px} 点装不下 {n}x{n} 模块，已撑到 {n} 点")
            size_px = n
        elif size_px < n * 2:
            logger.warning(
                f"二维码每模块不足 2 点({size_px}/{n})，扫码率可能下降，"
                f"建议增大 size_mm 或缩短 SN"
            )

        img = Image.new('1', (size_px, size_px), 1)
        draw = ImageDraw.Draw(img)
        edges = [round(i * size_px / n) for i in range(n + 1)]
        for r, row in enumerate(matrix):
            y0, y1 = edges[r], edges[r + 1]
            for c, is_dark in enumerate(row):
                if is_dark:
                    draw.rectangle([edges[c], y0, edges[c + 1] - 1, y1 - 1], fill=0)

        logger.info(
            f"二维码: 版本{qr.version} {n}x{n} 模块, 外框 {size_px}x{size_px} 点 "
            f"(每模块 {size_px / n:.2f} 点)"
        )
        return img

    @staticmethod
    def _bitmap_to_gfa(img) -> str:
        """把 1-bit PIL 位图转成 ZPL ^GFA 指令(ASCII hex)"""
        width, height = img.size
        bytes_per_row = (width + 7) // 8
        total_bytes = bytes_per_row * height

        # ZPL: 0 bit = 不打印, 1 bit = 打印(黑)。PIL '1' 模式 0=黑,需取反
        pixels = img.load()
        rows_hex = []
        for y in range(height):
            row = bytearray(bytes_per_row)
            for x in range(width):
                if pixels[x, y] == 0:  # 黑点 -> 需要打印
                    row[x // 8] |= (0x80 >> (x % 8))
            rows_hex.append(row.hex().upper())
        data = ''.join(rows_hex)

        return f"^GFA,{total_bytes},{total_bytes},{bytes_per_row},{data}"

    def _build_zpl_bitmap(self, sn: str, capacity: str = '') -> str:
        """构建 ZPL 打印指令（位图渲染模式）"""
        label_w = int(self.paper_width * self.dpi / 25.4)
        label_h = int(self.paper_height * self.dpi / 25.4)

        img = self._render_label_bitmap(sn)

        # 全白位图说明布局坐标落在画布外或渲染失败，此时打出来是空标签。
        # mode '1' 下 0=黑/打印、1=白；getextrema()[0] != 0 表示一个黑点都没有
        if img.getextrema()[0] != 0:
            logger.error(
                f"渲染结果为空白位图，打印将输出空标签！"
                f"画布 {label_w}x{label_h} (dpi={self.dpi}, design_dpi={self.design_dpi})，"
                f"请检查 zpl_layout 坐标是否超出画布"
            )

        gfa = self._bitmap_to_gfa(img)

        zpl = "^XA\n"
        zpl += f"^PW{label_w}\n"
        zpl += f"^LL{label_h}\n"
        zpl += "^FO0,0\n"
        zpl += gfa + "^FS\n"
        zpl += "^XZ\n"
        return zpl

    def _print_tspl(self, sn: str, capacity: str = '') -> bool:
        """TSPL 协议打印"""
        tspl = self._build_tspl_commands(sn, capacity)
        logger.info(f"TSPL 指令:\n{tspl}")
        return self._send_raw(tspl)

    def _print_zpl(self, sn: str, capacity: str = '') -> bool:
        """ZPL 协议打印"""
        zpl = self._build_zpl_commands(sn, capacity)
        logger.info(f"ZPL 指令:\n{zpl}")
        return self._send_raw(zpl)

    def _send_raw(self, commands: str) -> bool:
        """发送原始打印指令"""
        if not self.printer_name:
            logger.error("打印机名称未设置")
            return False

        try:
            data = commands.encode('utf-8')
            hPrinter = win32print.OpenPrinter(self.printer_name)
            try:
                win32print.StartDocPrinter(hPrinter, 1, ("Label", None, "RAW"))
                win32print.StartPagePrinter(hPrinter)
                win32print.WritePrinter(hPrinter, data)
                win32print.EndPagePrinter(hPrinter)
                win32print.EndDocPrinter(hPrinter)
                logger.info(f"打印成功: {self.printer_name}")
                return True
            finally:
                win32print.ClosePrinter(hPrinter)
        except Exception as e:
            logger.error(f"打印失败: {e}")
            return False
