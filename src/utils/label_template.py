"""产测标签配置与位图渲染；所有坐标按模板 design_dpi 标定。"""

from datetime import datetime
from pathlib import Path

import qrcode
import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import get_resource_path


LABEL_FONT = 'assets/fonts/方正兰亭圆简体.TTF'
LABEL_BACKGROUND = 'config/标签打印底图.png'


def resize_print_bitmap(image: Image.Image, size: tuple) -> Image.Image:
    """二值标签缩小时按最终输出阈值保留细线覆盖。"""
    if image.mode == '1' and (size[0] < image.width or size[1] < image.height):
        # 先计算黑点覆盖率再二值化；不能直接 resize(mode='1')，Pillow
        # 会强制使用最近邻。与内容输出相同阈值，避免抗锯齿边缘额外增粗。
        coverage = image.convert('L').resize(size, Image.Resampling.BOX)
        return coverage.point(lambda value: 255 if value >= 160 else 0, mode='1')
    return image.resize(size, Image.Resampling.NEAREST)


def apply_print_transform(image: Image.Image, dpi: int, printer_config: dict) -> Image.Image:
    """沿用主控校准：整图居中缩放后按毫米偏移，纸张画布尺寸不变。"""
    scale = float(printer_config.get('print_scale', 1.0))
    if scale <= 0:
        raise ValueError('print_scale 必须大于 0')
    background = 1 if image.mode == '1' else 'white'
    if abs(scale - 1.0) >= 1e-9:
        width, height = image.size
        resized = resize_print_bitmap(
            image, (max(1, round(width * scale)), max(1, round(height * scale))))
        canvas = Image.new(image.mode, image.size, background)
        canvas.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
        image = canvas
    offset = (round(float(printer_config.get('offset_x_mm', 0)) * dpi / 25.4),
              round(float(printer_config.get('offset_y_mm', 0)) * dpi / 25.4))
    if offset != (0, 0):
        canvas = Image.new(image.mode, image.size, background)
        canvas.paste(image, offset)
        image = canvas
    return image


def load_product_template(printer_config: dict, product: str) -> dict:
    """每次打印重新读取产品文件，路径相对应用根目录（兼容外置配置）。"""
    references = printer_config.get('label_templates', {})
    if product not in references:
        raise ValueError(f'未配置产品标签: {product}')
    path = Path(get_resource_path(str(references[product])))
    with path.open(encoding='utf-8') as stream:
        template = yaml.safe_load(stream)
    if not isinstance(template, dict) or not isinstance(template.get('fields'), list):
        raise ValueError(f'标签配置必须包含 fields 列表: {path}')
    template['print_background'] = printer_config.get('print_background', False)
    template['background_image'] = printer_config.get('background_image', LABEL_BACKGROUND)
    return template


def field_content(field: dict, sn: str, capacity: str, now: datetime) -> str:
    """字段可组合固定文字、日期和 SN；空字符串用于保留空行。"""
    kind = field.get('type')
    prefix = str(field.get('label', ''))
    if kind == 'date':
        return prefix + now.strftime(field.get('format', '%Y/%m/%d'))
    if kind == 'text':
        value = field.get('value')
        if value is None:
            key = field.get('key', 'sn')
            if key not in ('sn', 'disk_size'):
                raise ValueError(f'未知标签变量: {key}')
            value = sn if key == 'sn' else capacity
        return prefix + str(value or '')
    if kind == 'disk_size':
        return prefix + str(capacity) if capacity else ''
    if kind == 'model':
        return prefix + str(field.get('value', ''))
    raise ValueError(f'未知标签字段类型: {kind}')


def render_qrcode(data: str, size: int, error_correction: str) -> Image.Image:
    ecc = getattr(qrcode.constants, 'ERROR_CORRECT_' + error_correction.upper())
    qr = qrcode.QRCode(error_correction=ecc, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    count = len(matrix)
    if size < count:
        raise ValueError(f'二维码目标 {size} 点装不下 {count}x{count} 模块')
    image = Image.new('1', (size, size), 1)
    draw = ImageDraw.Draw(image)
    edges = [round(i * size / count) for i in range(count + 1)]
    for row, values in enumerate(matrix):
        for col, dark in enumerate(values):
            if dark:
                draw.rectangle((edges[col], edges[row], edges[col + 1] - 1,
                                edges[row + 1] - 1), fill=0)
    return image


def render_template(template: dict, dpi: int, paper_width: float,
                    paper_height: float, sn: str, capacity: str = '') -> Image.Image:
    """底图开关只影响底图；固定文字、动态字段与二维码始终渲染。"""
    label_size = (max(1, round(paper_width * dpi / 25.4)),
                  max(1, round(paper_height * dpi / 25.4)))
    content_size = (max(1, round(float(template.get('paper_width', paper_width)) * dpi / 25.4)),
                    max(1, round(float(template.get('paper_height', paper_height)) * dpi / 25.4)))
    rgb = Image.new('RGB', content_size, 'white')
    if template.get('print_background', False):
        path = get_resource_path(template.get('background_image', LABEL_BACKGROUND))
        with Image.open(path) as source:
            source = source.convert('RGBA')
            source.thumbnail(content_size, Image.Resampling.LANCZOS)
            rgb.paste(source, ((rgb.width - source.width) // 2,
                               (rgb.height - source.height) // 2), source)

    scale = dpi / float(template.get('design_dpi', 203) or 203)

    def scaled(value, minimum=0):
        return max(minimum, round(float(value) * scale))

    now = datetime.now()
    for field in template['fields']:
        x = scaled(field.get('x', 0))
        y = (scaled(template.get('text_origin_y', 0))
             + round(float(field['line']) * float(template['line_spacing_pt']) * dpi / 72.0)
             if 'line' in field else scaled(field.get('y', 0)))
        y += round(float(field.get('offset_y_pt', 0)) * dpi / 72.0)
        if field.get('type') == 'qrcode':
            qr = render_qrcode(sn, scaled(field.get('size', 80), 1),
                               str(field.get('error_correction', 'M')))
            rgb.paste(qr.convert('RGB'), (x, y))
            continue
        content = field_content(field, sn, capacity, now)
        if not content:
            continue
        font_path = get_resource_path(field.get('font_path', template.get('font_path', LABEL_FONT)))
        font_size_pt = field.get('font_size_pt', template.get('font_size_pt'))
        font_size = (round(float(font_size_pt) * dpi / 72.0)
                     if font_size_pt is not None
                     else scaled(field.get('font_size', 16), 1))
        bold_width_pt = field.get('bold_width_pt', template.get('bold_width_pt'))
        stroke = (round(float(bold_width_pt) * dpi / 72.0)
                  if bold_width_pt is not None
                  else scaled(field.get('bold_width', 1 if field.get('bold') else 0)))
        anchor = {'center': 'mt', 'right': 'rt'}.get(field.get('align'))
        font = ImageFont.truetype(font_path, max(1, font_size))
        # 按实际 DPI 单次绘制完整字形，不放大缩小文字，避免插值产生边缘杂点。
        mask = Image.new('L', content_size, 255)
        ImageDraw.Draw(mask).text((x, y), content, font=font, fill=0, anchor=anchor)
        threshold = int(field.get('text_threshold', template.get('threshold', 160)))
        mask = mask.point(lambda value: 255 if value >= threshold else 0)
        if stroke > 0:
            # 在二值字形上扩展整点描边，保留原笔画且不引入灰度边缘。
            mask = mask.filter(ImageFilter.MinFilter(stroke * 2 + 1))
        rgb.paste('black', mask=mask.point(lambda value: 255 - value))

    threshold = int(template.get('threshold', 160))
    rendered = rgb.convert('L').point(lambda value: 255 if value >= threshold else 0, mode='1')
    if rendered.size == label_size:
        return rendered
    canvas = Image.new('1', label_size, 1)
    canvas.paste(rendered, ((label_size[0] - rendered.width) // 2,
                            (label_size[1] - rendered.height) // 2))
    return canvas


def tspl_bitmap(image: Image.Image, paper_width: float, paper_height: float) -> bytes:
    """沿用 TSPL 的速度、浓度、间隙和方向，发送整张标签的二进制位图。"""
    width_bytes = (image.width + 7) // 8
    bitmap = bytearray(width_bytes * image.height)
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            if pixels[x, y] == 0:
                bitmap[y * width_bytes + x // 8] |= 0x80 >> (x % 8)
    prefix = (
        'CLS\n'
        f'SIZE {paper_width:g} mm, {paper_height:g} mm\n'
        'GAP 2 mm, 0 mm\nDIRECTION 1\nSPEED 4\nDENSITY 10\n'
        'REFERENCE 0,0\nSET TEAR ON\nCLS\n'
        f'BITMAP 0,0,{width_bytes},{image.height},0,'
    ).encode('ascii')
    return prefix + bytes(bitmap) + b'\nPRINT 1,1\n'
