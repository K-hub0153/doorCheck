"""
打印预览对话框
显示标签打印的预览效果
"""
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QGroupBox)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap, QImage
from datetime import datetime
from PIL import Image, ImageDraw
from src.qrcode_print.label_background import (background_path, barcode_field_image,
                                       label_pixels, load_background,
                                       qrcode_field_image, render_text_fields)
import io

class PrintPreviewDialog(QDialog):
    """打印预览对话框"""

    def __init__(self, qrcode_data: str, template_name: str = "default", parent=None):
        super().__init__(parent)
        self.qrcode_data = qrcode_data
        self.template_name = template_name

        # 从父窗口获取config和template
        if parent and hasattr(parent, 'config'):
            self.config = parent.config
            self.template = self.config.get(f'qrcode_print.templates.{template_name}', {})
        else:
            self.config = None
            self.template = {}

        self.init_ui()

    def init_ui(self):
        """初始化UI"""
        self.setWindowTitle("打印预览")
        self.setFixedSize(700, 600)

        layout = QVBoxLayout()

        # 信息组
        info_group = QGroupBox("标签信息")
        info_layout = QVBoxLayout()

        paper_w = self.template.get('paper_width', 50)
        paper_h = self.template.get('paper_height', 30)

        info_text = f"""
        二维码内容: {self.qrcode_data}
        模板: {self.template_name} - {self.template.get('description', '')}
        生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        标签尺寸: {paper_w}mm x {paper_h}mm
        """

        info_label = QLabel(info_text)
        info_label.setStyleSheet("padding: 10px; background-color: #F5F5F5; border-radius: 5px;")
        info_layout.addWidget(info_label)
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)

        # 预览图组
        preview_group = QGroupBox("预览效果")
        preview_layout = QVBoxLayout()

        # 生成预览图
        try:
            preview_pixmap = self.generate_preview()
            preview_label = QLabel()
            preview_label.setPixmap(preview_pixmap)
            preview_label.setAlignment(Qt.AlignCenter)
            preview_label.setStyleSheet("background-color: white; padding: 20px;")
            preview_layout.addWidget(preview_label)
        except Exception as e:
            error_label = QLabel(f"预览生成失败: {e}")
            error_label.setAlignment(Qt.AlignCenter)
            error_label.setStyleSheet("color: red; padding: 20px;")
            preview_layout.addWidget(error_label)
            import traceback
            traceback.print_exc()

        preview_group.setLayout(preview_layout)
        layout.addWidget(preview_group)

        # 按钮
        button_layout = QHBoxLayout()

        close_btn = QPushButton("关闭")
        close_btn.setMinimumWidth(100)
        close_btn.clicked.connect(self.accept)
        button_layout.addStretch()
        button_layout.addWidget(close_btn)

        layout.addLayout(button_layout)

        self.setLayout(layout)

    def generate_preview(self) -> QPixmap:
        """
        根据模板生成打印预览图

        Returns:
            QPixmap: 预览图像
        """
        # 获取纸张尺寸（mm）
        paper_w = self.template.get('paper_width', 50)
        paper_h = self.template.get('paper_height', 30)

        # 以打印分辨率的两倍绘制，避免小字号文字先生成低分辨率图再放大。
        dpi = 406
        scale = dpi / 203.0
        width, height = label_pixels(self.template, dpi)

        # 底图与打印使用同一尺寸换算，字段覆盖在底图上。
        path = background_path(self.config.config_path, self.template) if self.config else None
        img = load_background(path, self.template, dpi) if path else Image.new('RGB', (width, height), 'white')
        draw = ImageDraw.Draw(img)

        if not path:
            draw.rectangle([round(2 * scale), round(2 * scale),
                            width - round(2 * scale), height - round(2 * scale)],
                           outline='gray', width=round(scale))

        # 获取字段列表
        fields = self.template.get('fields', [])

        render_text_fields(img, fields, self.qrcode_data, dpi)
        for field in fields:
            if field.get('type') in ('qrcode', 'barcode'):
                self._render_field(img, field, scale)

        # 转换PIL Image为QPixmap
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)

        qimage = QImage.fromData(buffer.read())
        pixmap = QPixmap.fromImage(qimage)

        # 缩放到合适的显示大小
        display_width = min(600, round(width / scale * 2))
        pixmap = pixmap.scaledToWidth(display_width, Qt.SmoothTransformation)

        return pixmap

    def _render_field(self, img: Image, field: dict, scale: float = 1.0):
        """渲染单个字段"""
        ftype = field.get('type')
        x = round(float(field.get('x', 0)) * scale)
        y = round(float(field.get('y', 0)) * scale)

        if ftype == 'qrcode':
            # 二维码
            cell_width = max(1, round(float(field.get('cell_width', 6)) * scale))
            img.paste(qrcode_field_image(self.qrcode_data, cell_width), (x, y))

        elif ftype == 'barcode':
            barcode = barcode_field_image(self.qrcode_data, field)
            if scale != 1:
                barcode = barcode.resize(
                    (round(barcode.width * scale), round(barcode.height * scale)),
                    Image.Resampling.NEAREST)
            img.paste(barcode, (x, y))
