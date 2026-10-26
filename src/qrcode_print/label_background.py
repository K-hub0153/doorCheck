"""扫码打印标签底图：按标签物理尺寸输出打印机点阵。"""

from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple
from uuid import uuid4
import io

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
import qrcode

from src.utils.config import get_resource_path


TEXT_BASE_SIZES = {'1': 8, '2': 10, '3': 12, '4': 14}


def text_font_size(field: dict, dpi: int = 203) -> int:
    """返回文本字段在目标打印分辨率下的像素字号。"""
    explicit_size = field.get('font_size')
    if explicit_size is not None:
        return max(1, round(float(explicit_size) * dpi / 203.0))
    xs = float(field.get('x_scale', 1))
    ys = float(field.get('y_scale', 1))
    base = TEXT_BASE_SIZES.get(str(field.get('font', '1')), 8)
    return max(1, round(base * (xs + ys) / 2 * dpi / 203.0))


def background_path(config_path: str, template: dict) -> Optional[Path]:
    name = template.get('background_image')
    if not name:
        return None
    path = Path(config_path).parent / name
    if not path.is_file():
        raise FileNotFoundError(f"标签底图不存在: {path}")
    return path


def copy_background(config_path: str, template: dict) -> Optional[str]:
    """复制模板底图，返回新模板独占的相对路径。"""
    source = background_path(config_path, template)
    if source is None:
        return None
    config_dir = Path(config_path).parent.resolve()
    try:
        source.resolve().relative_to(config_dir)
    except ValueError as exc:
        raise ValueError("底图路径必须位于当前配置目录") from exc
    relative = Path(template['background_image'])
    destination_name = relative.with_name(
        f"{relative.stem}-{uuid4().hex}{relative.suffix}")
    destination = Path(config_path).parent / destination_name
    destination.write_bytes(source.read_bytes())
    return destination_name.as_posix()


def label_pixels(template: dict, dpi: int) -> Tuple[int, int]:
    return (round(float(template.get('paper_width', 50)) * dpi / 25.4),
            round(float(template.get('paper_height', 30)) * dpi / 25.4))


def import_background(config_path: str, source_path: str, template: dict) -> str:
    """用上传文件的原始内容覆盖模板已有底图，保持配置路径不变。"""
    destination = background_path(config_path, template)
    if destination is None:
        raise ValueError("当前模板没有底图，无法替换")
    try:
        destination.resolve().relative_to(Path(config_path).parent.resolve())
    except ValueError as exc:
        raise ValueError("底图路径必须位于当前配置目录") from exc
    data = Path(source_path).read_bytes()
    with Image.open(io.BytesIO(data)) as source:
        source.verify()
    destination.write_bytes(data)
    return template['background_image']


def label_font_path() -> Path:
    path = Path(get_resource_path('assets/fonts/label.ttf'))
    if not path.is_file():
        raise FileNotFoundError(f"扫码打印字体不存在: {path}")
    return path


def text_field_image(field: dict, qrcode_data: str, dpi: int = 203) -> Image.Image:
    """生成与打印预览一致的透明文本图，原点即字段坐标。"""
    ftype = field.get('type')
    if ftype == 'text':
        content = f"{field.get('label', '')}{qrcode_data}"
    elif ftype == 'date':
        content = datetime.now().strftime(field.get('format', '%Y/%m/%d'))
    elif ftype == 'model':
        content = str(field.get('value', ''))
    else:
        raise ValueError(f"不支持的文本字段类型: {ftype}")
    font = ImageFont.truetype(str(label_font_path()), text_font_size(field, dpi))
    bounds = font.getbbox(content)
    image = Image.new('RGBA', (max(1, bounds[2]), max(1, bounds[3])), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((0, 0), content, fill='black', font=font)
    if field.get('bold', False):
        # 1 dot 实心描边对小字号过重；半强度边缘在预览和打印位图中都更细。
        padding = Image.new('L', (image.width + 2, image.height + 2))
        padding.paste(image.getchannel('A'), (1, 1))
        expanded = padding.filter(ImageFilter.MaxFilter(3))
        alpha = Image.blend(padding, expanded, 0.45)
        image = Image.new('RGBA', (image.width + 1, image.height + 1), (0, 0, 0, 0))
        image.putalpha(alpha.crop((1, 1, image.width + 1, image.height + 1)))
    rotation = int(field.get('rotation', 0)) % 360
    if rotation:
        image = image.rotate(-rotation, expand=True)
    return image


def qrcode_field_image(qrcode_data: str, cell_width: int) -> Image.Image:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=cell_width,
        border=1,
    )
    qr.add_data(qrcode_data)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(fill_color='black', back_color='white').save(buffer, format='PNG')
    buffer.seek(0)
    return Image.open(buffer).convert('RGB')


def barcode_field_image(qrcode_data: str, field: dict) -> Image.Image:
    """生成编辑器与预览共用的条码图像。"""
    try:
        from barcode import Code128
        from barcode.writer import ImageWriter
        code128 = Code128(qrcode_data, writer=ImageWriter())
        return code128.render({
            'module_width': field.get('narrow', 2) / 10.0,
            'module_height': field.get('height', 40),
            'quiet_zone': 1,
            'text_distance': 2,
            'font_size': 8,
        }).convert('RGB')
    except Exception as exc:
        image = Image.new('RGB', (121, 41), 'white')
        draw = ImageDraw.Draw(image)
        color = 'blue' if isinstance(exc, ImportError) else 'red'
        draw.rectangle((0, 0, 120, 40), outline=color)
        draw.text((10, 15), 'BARCODE', fill=color)
        return image


def render_text_fields(image: Image.Image, fields: list, qrcode_data: str,
                       dpi: int = 203) -> None:
    """用项目指定字体渲染动态文本，坐标以 203 DPI 标签点为基准。"""
    scale = dpi / 203.0
    for field in fields:
        if field.get('type') not in ('text', 'date', 'model'):
            continue
        x = round(float(field.get('x', 0)) * scale)
        y = round(float(field.get('y', 0)) * scale)
        overlay = text_field_image(field, qrcode_data, dpi)
        image.paste(overlay, (x, y), overlay)


def load_background(path: Path, template: dict, dpi: int) -> Image.Image:
    """按模板尺寸适配原图，保持完整比例且不裁切。"""
    target = label_pixels(template, dpi)
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGBA')
        image = ImageOps.contain(image, target, Image.Resampling.LANCZOS)
        background = Image.new('RGB', target, 'white')
        background.paste(image, ((target[0] - image.width) // 2,
                                 (target[1] - image.height) // 2), image)
        return background


def bitmap_bytes(image: Image.Image) -> Tuple[int, int, bytes]:
    """TSPL BITMAP 要求每行宽度为整字节，1 表示黑色。"""
    mono = image.convert('L').point(lambda value: 255 if value >= 128 else 0, mode='1')
    width_bytes = (mono.width + 7) // 8
    pixels = mono.load()
    result = bytearray(width_bytes * mono.height)
    for y in range(mono.height):
        for x in range(mono.width):
            if pixels[x, y] == 0:
                result[y * width_bytes + x // 8] |= 0x80 >> (x % 8)
    return width_bytes, mono.height, bytes(result)
