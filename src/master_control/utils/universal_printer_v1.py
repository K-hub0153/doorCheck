"""
通用打印模块
支持 ZEBRA (ZPL) 和 TSPL 标签打印机
自动检测打印机参数并适配协议
"""
import socket
import re
from datetime import datetime
from typing import Optional
from src.master_control.utils.logger import logger

try:
    import win32print
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False


class UniversalPrinter:
    """通用标签打印机类"""
    
    def __init__(self, config: dict = None):
        """
        初始化打印机
        
        Args:
            config: 打印配置字典，包含：
                - enabled: 是否启用打印
                - protocol: 打印协议 'zpl' 或 'tspl'，None 为自动检测
                - printer_ip: 打印机 IP（网络打印）
                - printer_port: 打印机端口，默认 9100
                - printer_name: Windows 打印机名称（本地打印）
                - dpi: 打印分辨率，默认 600
                - paper_width_mm: 纸张宽度（毫米）
                - paper_height_mm: 纸张高度（毫米）
                - tspl_layout: TSPL 布局配置（可选）
        """
        self.config = config or {}
        self.enabled = self.config.get('enabled', False)
        
        if not self.enabled:
            logger.warning("打印功能未启用")
            return
        
        # 打印机连接参数
        self.printer_ip = self.config.get('printer_ip')
        self.printer_port = self.config.get('printer_port', 9100)
        self.printer_name = self.config.get('printer_name')
        
        # 打印参数
        self.dpi = self.config.get('dpi', 600)
        self.paper_width_mm = self.config.get('paper_width_mm', 50)
        self.paper_height_mm = self.config.get('paper_height_mm', 30)
        
        # TSPL / ZPL 布局配置
        self.tspl_layout = self.config.get('tspl_layout', {})
        self.zpl_layout = self.config.get('zpl_layout', {})
        
        # 协议类型（None 表示自动检测）
        self.protocol = self.config.get('protocol')
        
        # 如果未指定协议，尝试自动检测
        if not self.protocol:
            self.protocol = self._detect_protocol()
        
        logger.info(f"打印机初始化: 协议={self.protocol}, DPI={self.dpi}, "
                   f"纸张={self.paper_width_mm}x{self.paper_height_mm}mm")
    
    # 协议识别规则表，按优先级排列（先匹配先返回）
    # 每项: (协议, [关键词列表])  —— 名称转小写后做子串匹配
    _PROTOCOL_RULES = [
        # Zebra / ZPL
        # 驱动名通常为 "ZDesigner <model>" 或直接含 "zebra" / "zpl"
        ('zpl',  ['zdesigner', 'zebra', 'zpl']),
        # 芯烨 / Xprinter / TSC / TSPL
        ('tspl', ['xprinter', 'xp-t', 'xp-n', 'xp-d', 'tsc', 'tspl']),
    ]

    def _detect_protocol(self) -> str:
        """
        自动检测打印机协议。

        匹配优先级：配置 protocol 字段 > 打印机名称关键词 > 默认 ZPL。
        支持的关键词见 _PROTOCOL_RULES。

        Returns:
            'zpl' 或 'tspl'
        """
        if self.printer_name:
            name_lower = self.printer_name.lower()
            for protocol, keywords in self._PROTOCOL_RULES:
                if any(kw in name_lower for kw in keywords):
                    matched = next(kw for kw in keywords if kw in name_lower)
                    logger.info(
                        f"根据打印机名称 '{self.printer_name}' 中的关键词 '{matched}' "
                        f"检测到 {protocol.upper()} 协议"
                    )
                    return protocol

        logger.warning(
            f"无法从打印机名称 '{self.printer_name}' 识别协议，默认使用 ZPL"
        )
        return 'zpl'
    
    def print_label(self, sn: str, disk_size: str = None) -> bool:
        """
        打印标签
        
        Args:
            sn: 设备序列号
            disk_size: 硬盘容量（可选，如 '512G'）
        
        Returns:
            bool: 是否打印成功
        """
        if not self.enabled:
            logger.info("打印功能未启用")
            return False
        
        try:
            logger.info(f"开始打印标签: SN={sn}, Disk={disk_size}")
            
            if self.protocol == 'zpl':
                commands = self._build_zpl_commands(sn, disk_size)
            else:
                commands = self._build_tspl_commands(sn, disk_size)
            
            # 优先网络打印，其次本地打印
            if self.printer_ip:
                success = self._send_to_network_printer(commands)
            elif self.printer_name and WIN32_AVAILABLE:
                success = self._send_to_windows_printer(commands)
            else:
                logger.warning("未配置打印机 IP 或打印机名称")
                return False
            
            if success:
                logger.info("打印完成")
            return success
            
        except Exception as e:
            logger.error(f"打印失败: {e}")
            return False
    
    def _build_zpl_commands(self, sn: str, disk_size: Optional[str]) -> str:
        """
        构建 ZPL 打印指令（ZEBRA 打印机）
        布局参数全部从 zpl_layout 配置读取，与 tspl_layout 结构对齐。

        Returns:
            ZPL 指令字符串
        """
        # 纸张尺寸（dots）
        width_dots  = int(self.paper_width_mm  * self.dpi / 25.4)
        height_dots = int(self.paper_height_mm * self.dpi / 25.4)

        # 从 zpl_layout 读取各元素配置
        qr_cfg   = self.zpl_layout.get('qrcode',    {})
        date_cfg = self.zpl_layout.get('date',      {})
        sn_cfg   = self.zpl_layout.get('sn',        {})
        disk_cfg = self.zpl_layout.get('disk_size', {})

        # 二维码位置与大小（目标 15mm）
        qr_x = qr_cfg.get('x', 30)
        qr_y = qr_cfg.get('y', 30)
        if 'magnification' in qr_cfg:
            qr_magnification = qr_cfg['magnification']
        else:
            target_qr_dots   = int(15 * self.dpi / 25.4)
            qr_magnification = max(1, (target_qr_dots + 24) // 25)

        # 日期
        date_x  = date_cfg.get('x',           int(width_dots * 0.3))
        date_y  = date_cfg.get('y',           int(height_dots * 0.52))
        date_fh = date_cfg.get('font_height', 80)
        date_fw = date_cfg.get('font_width',  80)

        # SN
        sn_x  = sn_cfg.get('x',           40)
        sn_y  = sn_cfg.get('y',           height_dots - 180)
        sn_fh = sn_cfg.get('font_height', 80)
        sn_fw = sn_cfg.get('font_width',  80)

        # 硬盘容量
        disk_x  = disk_cfg.get('x',           int(width_dots * 0.7))
        disk_y  = disk_cfg.get('y',           height_dots - 130)
        disk_fh = disk_cfg.get('font_height', 75)
        disk_fw = disk_cfg.get('font_width',  75)

        # 日期字符串
        from datetime import datetime
        date_str = datetime.now().strftime('%Y/%m/%d')

        zpl  = "^XA\n"
        zpl += "^JUS\n"
        zpl += "^MCY\n"
        zpl += "^CI28\n"
        zpl += "^MNY\n"
        zpl += "^LH0,0\n"
        zpl += f"^PW{width_dots}\n"
        zpl += f"^LL{height_dots}\n"

        # 二维码
        zpl += f"^FO{qr_x},{qr_y}\n"
        zpl += f"^BQN,2,{qr_magnification}\n"
        zpl += f"^FDQA,{sn}^FS\n"

        # 日期
        zpl += f"^CF0,{date_fh},{date_fw}\n"
        zpl += f"^FO{date_x},{date_y}^FD{date_str}^FS\n"

        # SN
        zpl += f"^CF0,{sn_fh},{sn_fw}\n"
        zpl += f"^FO{sn_x},{sn_y}^FDS{sn}^FS\n"

        # 硬盘容量（可选）
        if disk_size:
            zpl += f"^CF0,{disk_fh},{disk_fw}\n"
            zpl += f"^FO{disk_x},{disk_y}^FD{disk_size}^FS\n"

        zpl += "^PQ1\n"
        zpl += "^XZ"

        return zpl
    
    def _build_tspl_commands(self, sn: str, disk_size: Optional[str]) -> str:
        """
        构建 TSPL 打印指令（TSC 打印机）
        
        Returns:
            TSPL 指令字符串
        """
        # 从 tspl_layout 读取配置，fallback 到默认值
        qr_cfg = self.tspl_layout.get('qrcode', {})
        date_cfg = self.tspl_layout.get('date', {})
        sn_cfg = self.tspl_layout.get('sn', {})
        disk_cfg = self.tspl_layout.get('disk_size', {})

        # 二维码 cell_width：目标 15mm，由配置决定
        # 若配置未设置，则根据 DPI 自动计算最接近 15mm 的 cell_width
        if 'cell_width' in qr_cfg:
            qr_cell_width = qr_cfg['cell_width']
        else:
            # 15mm -> dots，再除以典型 QR Version1(21模块) 得到 cell_width
            # 保守按 25 模块估算（Version 2），向上取整接近 15mm
            target_dots = int(15 * self.dpi / 25.4)
            qr_cell_width = max(1, (target_dots + 24) // 25)

        qr_x = qr_cfg.get('x', 245)
        qr_y = qr_cfg.get('y', 69)

        date_x = date_cfg.get('x', 120)
        date_y = date_cfg.get('y', 124)
        date_font = date_cfg.get('font', '1')
        date_xs = date_cfg.get('x_scale', 1)
        date_ys = date_cfg.get('y_scale', 1)

        sn_x = sn_cfg.get('x', 34)
        sn_y = sn_cfg.get('y', 178)
        sn_font = sn_cfg.get('font', '1')
        sn_xs = sn_cfg.get('x_scale', 1)
        sn_ys = sn_cfg.get('y_scale', 1)

        disk_x = disk_cfg.get('x', 245)
        disk_y = disk_cfg.get('y', 220)
        disk_font = disk_cfg.get('font', '1')
        disk_xs = disk_cfg.get('x_scale', 1)
        disk_ys = disk_cfg.get('y_scale', 1)

        tspl = "CLS\n"
        tspl += f"SIZE {self.paper_width_mm} mm,{self.paper_height_mm} mm\n"
        tspl += "GAP 2 mm,0 mm\n"
        tspl += "DIRECTION 1\n"
        tspl += "SPEED 4\n"
        tspl += "DENSITY 10\n"
        tspl += "REFERENCE 0,0\n"
        tspl += "SET TEAR ON\n"
        tspl += "CLS\n"
        
        # 日期
        date_str = datetime.now().strftime("%Y/%m/%d")
        tspl += f'TEXT {date_x},{date_y},"{date_font}",0,{date_xs},{date_ys},"{date_str}"\n'
        
        # SN
        tspl += f'TEXT {sn_x},{sn_y},"{sn_font}",0,{sn_xs},{sn_ys},"{sn}"\n'
        
        # 二维码（仅包含 SN，与 TSPLPrinter 保持一致）
        tspl += f'QRCODE {qr_x},{qr_y},M,{qr_cell_width},A,0,M2,S7,"{sn}"\n'
        
        # 硬盘容量
        if disk_size:
            tspl += f'TEXT {disk_x},{disk_y},"{disk_font}",0,{disk_xs},{disk_ys},"{disk_size}"\n'
        
        tspl += "PRINT 1,1\n"
        tspl += "CUT\n"
        
        return tspl
    
    def _send_to_network_printer(self, commands: str) -> bool:
        """通过网络发送打印指令"""
        try:
            logger.info(f"发送到网络打印机: {self.printer_ip}:{self.printer_port}")
            logger.debug(f"打印指令:\n{commands}")
            
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect((self.printer_ip, self.printer_port))
                sock.sendall(commands.encode('utf-8'))
            
            logger.info("网络打印成功")
            return True
            
        except socket.timeout:
            logger.error(f"连接打印机超时: {self.printer_ip}:{self.printer_port}")
            return False
        except socket.error as e:
            logger.error(f"网络打印失败: {e}")
            return False
    
    def _send_to_windows_printer(self, commands: str) -> bool:
        """通过 Windows 打印机发送打印指令"""
        try:
            logger.info(f"发送到 Windows 打印机: {self.printer_name}")
            logger.debug(f"打印指令:\n{commands}")
            
            hPrinter = win32print.OpenPrinter(self.printer_name)
            try:
                win32print.StartDocPrinter(hPrinter, 1, ("Label", None, "RAW"))
                win32print.StartPagePrinter(hPrinter)
                win32print.WritePrinter(hPrinter, commands.encode('utf-8'))
                win32print.EndPagePrinter(hPrinter)
                win32print.EndDocPrinter(hPrinter)
                logger.info("Windows 打印成功")
                return True
            finally:
                win32print.ClosePrinter(hPrinter)
                
        except Exception as e:
            logger.error(f"Windows 打印失败: {e}")
            return False


def create_printer_from_config(config_dict: dict) -> UniversalPrinter:
    """
    从配置字典创建打印机实例（工厂函数）
    
    使用示例:
        config = {
            'enabled': True,
            'protocol': 'zpl',      # 'zpl' 或 'tspl'，None 自动检测
            'printer_ip': '192.168.1.104',
            'printer_port': 9100,
            'dpi': 600,
            'paper_width_mm': 50,
            'paper_height_mm': 30,
        }
        printer = create_printer_from_config(config)
        printer.print_label('orangepi-9c5e8ab4f5c60925', disk_size='512G')
    """
    return UniversalPrinter(config_dict)
