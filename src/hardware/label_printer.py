import win32print
from datetime import datetime


class TSPLPrinter:

    def __init__(self, config=None):
        self.printer_name = win32print.GetDefaultPrinter()
        self.label_width_mm = 50
        self.label_height_mm = 30
        self.dpi = 600
        self.label_width_px = 1181
        self.label_height_px = 708
        self.density = 10
        self.speed = 4
        self.gap_mm = 2

        # Load TSPL layout from config, fall back to defaults
        tspl_layout = {}
        if config is not None:
            # Support both Config object (with .get()) and plain dict
            if hasattr(config, 'get') and callable(config.get):
                try:
                    result = config.get('printer.tspl_layout', {})
                    if isinstance(result, dict):
                        tspl_layout = result
                except Exception:
                    pass
            elif isinstance(config, dict) and 'printer' in config:
                tspl_layout = config['printer'].get('tspl_layout', {})

        border = tspl_layout.get('border', {}) if isinstance(tspl_layout, dict) else {}
        self._border_x1 = border.get('x1', 2)
        self._border_y1 = border.get('y1', 2)
        self._border_x2 = border.get('x2', 398)
        self._border_y2 = border.get('y2', 238)
        self._border_thickness = border.get('thickness', 2)

        date_cfg = tspl_layout.get('date', {}) if isinstance(tspl_layout, dict) else {}
        self._date_x = date_cfg.get('x', 120)
        self._date_y = date_cfg.get('y', 124)
        self._date_font = date_cfg.get('font', '1')
        self._date_rotation = date_cfg.get('rotation', 0)
        self._date_x_scale = date_cfg.get('x_scale', 1)
        self._date_y_scale = date_cfg.get('y_scale', 1)

        sn_cfg = tspl_layout.get('sn', {}) if isinstance(tspl_layout, dict) else {}
        self._sn_x = sn_cfg.get('x', 34)
        self._sn_y = sn_cfg.get('y', 178)
        self._sn_font = sn_cfg.get('font', '1')
        self._sn_rotation = sn_cfg.get('rotation', 0)
        self._sn_x_scale = sn_cfg.get('x_scale', 1)
        self._sn_y_scale = sn_cfg.get('y_scale', 1)

        qr_cfg = tspl_layout.get('qrcode', {}) if isinstance(tspl_layout, dict) else {}
        self._qr_x = qr_cfg.get('x', 245)
        self._qr_y = qr_cfg.get('y', 69)
        self._qr_cell_width = qr_cfg.get('cell_width', 6)

    def draw_border(self):
        x1, y1 = self._border_x1, self._border_y1
        x2, y2 = self._border_x2, self._border_y2
        t = self._border_thickness
        return f"BOX {x1},{y1},{x2},{y2},{t}\n"

    def draw_sn(self, device_sn: str):
        x, y = self._sn_x, self._sn_y
        font = self._sn_font
        r, xs, ys = self._sn_rotation, self._sn_x_scale, self._sn_y_scale
        return f'TEXT {x},{y},"{font}",{r},{xs},{ys},"SN:{device_sn}"\n'

    def draw_date(self):
        date_str = datetime.now().strftime("%Y/%m/%d")
        x, y = self._date_x, self._date_y
        font = self._date_font
        r, xs, ys = self._date_rotation, self._date_x_scale, self._date_y_scale
        return f'TEXT {x},{y},"{font}",{r},{xs},{ys},"{date_str}"\n'

    def draw_qrcode(self, device_sn: str):
        x, y = self._qr_x, self._qr_y
        cw = self._qr_cell_width
        return f'QRCODE {x},{y},M,{cw},A,0,M2,S7,"{device_sn}"\n'

    def build_label(self, device_sn: str, status=None):
        tspl = "CLS\n"
        tspl += f"SIZE {self.label_width_mm} mm,{self.label_height_mm} mm\n"
        tspl += f"GAP {self.gap_mm} mm,0 mm\n"
        tspl += "DIRECTION 1\n"
        tspl += f"SPEED {self.speed}\n"
        tspl += f"DENSITY {self.density}\n"
        tspl += "REFERENCE 0,0\n"
        tspl += "SET TEAR ON\n"
        tspl += "CLS\n"
        # tspl += self.draw_border()
        tspl += self.draw_date()
        tspl += self.draw_sn(device_sn)
        tspl += self.draw_qrcode(device_sn)
        tspl += "PRINT 1,1\n"
        tspl += "CUT\n"
        return tspl

    def send_raw(self, tspl: str):
        print("\n生成 TSPL 指令如下：")
        print("=" * 60)
        print(tspl)
        data = tspl.encode('utf-8')
        hPrinter = win32print.OpenPrinter(self.printer_name)
        try:
            win32print.StartDocPrinter(hPrinter, 1, ("Label", None, "RAW"))
            win32print.StartPagePrinter(hPrinter)
            win32print.WritePrinter(hPrinter, data)
            win32print.EndPagePrinter(hPrinter)
            win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)

    def print_label(self, device_sn, status=None):
        print("\n开始生成 TSPL 指令...")
        tspl = self.build_label(device_sn, status)
        self.send_raw(tspl)
        print("打印完成")
        return True


# Alias for backward compatibility
LabelPrinter = TSPLPrinter
