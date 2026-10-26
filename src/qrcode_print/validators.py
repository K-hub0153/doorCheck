"""
数据验证工具
用于验证二维码、序列号等输入数据的格式
"""
import re
from urllib.parse import urlsplit, parse_qs

from src.utils.logger import logger


# 扫码载荷中承载设备 SN 的查询字段，按顺序取第一个存在的
SN_QUERY_FIELDS = ('cuid',)


def extract_sn(payload: str) -> tuple:
    """
    从扫码载荷中提取设备 SN

    支持两种形式：
      - 纯 SN：WD24000123
      - URL 形式：kdweipos://qr/bind?ver=1&cuid=40ce6c...&devType=2

    Args:
        payload: 扫码枪读到的原始内容

    Returns:
        (str | None, str): (SN, 错误消息)。成功时错误消息为空字符串
    """
    if not payload or not payload.strip():
        return None, "扫码内容为空"

    payload = payload.strip()
    parts = urlsplit(payload)

    # 既无 scheme 也无查询串，视为纯 SN 直接返回
    if not parts.scheme and not parts.query:
        return payload, ""

    query = parse_qs(parts.query)
    for field in SN_QUERY_FIELDS:
        values = query.get(field)
        if values and values[0]:
            return values[0], ""

    return None, f"未能从 URL 中提取设备 SN（缺少 {' 或 '.join(SN_QUERY_FIELDS)} 字段）"


def validate_qrcode(qrcode_data: str, pattern: str = None) -> tuple:
    """
    验证二维码格式

    Args:
        qrcode_data: 二维码字符串
        pattern: 正则表达式模式（可选）

    Returns:
        (bool, str): (是否有效, 错误消息)
    """
    # 检查是否为空
    if not qrcode_data or not qrcode_data.strip():
        return False, "二维码内容为空"

    # 去除首尾空格
    qrcode_data = qrcode_data.strip()

    # 检查长度
    if len(qrcode_data) < 3:
        return False, "二维码长度过短（最少3个字符）"

    if len(qrcode_data) > 100:
        return False, f"二维码长度过长（最多100个字符，当前{len(qrcode_data)}个）"

    # 使用自定义模式或默认模式
    if pattern is None:
        # 默认规则：字母、数字、横杠、下划线
        pattern = r'^[A-Za-z0-9\-_]+$'

    if not re.match(pattern, qrcode_data):
        return False, f"二维码格式不正确（仅允许字母、数字、横杠、下划线）"

    logger.debug(f"二维码验证通过: {qrcode_data}")
    return True, ""


def validate_device_sn(sn: str) -> tuple:
    """
    验证设备序列号格式

    Args:
        sn: 设备序列号

    Returns:
        (bool, str): (是否有效, 错误消息)
    """
    # 设备序列号规则：字母数字，10-30位
    if not sn or not sn.strip():
        return False, "序列号为空"

    sn = sn.strip()

    if len(sn) < 10:
        return False, f"序列号长度过短（最少10位，当前{len(sn)}位）"

    if len(sn) > 30:
        return False, f"序列号长度过长（最多30位，当前{len(sn)}位）"

    if not re.match(r'^[A-Za-z0-9]+$', sn):
        return False, "序列号格式不正确（仅允许字母和数字）"

    return True, ""


def sanitize_input(text: str) -> str:
    """
    清理输入数据，移除危险字符

    Args:
        text: 输入文本

    Returns:
        清理后的文本
    """
    if not text:
        return ""

    # 移除控制字符
    text = ''.join(c for c in text if c.isprintable())

    # 去除首尾空格
    text = text.strip()

    return text


def validate_batch_size(size: int) -> tuple:
    """
    验证批量打印数量

    Args:
        size: 批量大小

    Returns:
        (bool, str): (是否有效, 错误消息)
    """
    if size < 1:
        return False, "批量大小不能小于1"

    if size > 100:
        return False, "批量大小不能超过100"

    return True, ""
