import html
import json
import re
import time

import requests
from typing import List, Dict, Tuple
from datetime import datetime
from src.master_control.utils.logger import logger


class DataUploader:
    """测试数据上传工具"""

    # 接口地址
    DEFAULT_API_URL = "http://ishop-oqa.weidian.com/checkData/addBatch"

    # 与 mac_allocator 保持一致：网关对裸 python-requests UA 可能不友好
    HEADERS = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": "mcChecker/1.0",
    }

    # 日志里正文最多保留多少字符（限流页动辄几 KB，不能整段写日志）
    BODY_PREVIEW = 200

    def __init__(self, api_url: str = DEFAULT_API_URL,
                 timeout: int = 10,
                 retry_times: int = 2,
                 retry_delay: float = 2.0):
        self.api_url = api_url
        self.timeout = timeout
        self.retry_times = max(0, int(retry_times))
        self.retry_delay = max(0.0, float(retry_delay))

    # ------------------------------------------------------------------ 对外入口

    def upload_test_data(self, records: List[Dict]) -> Tuple[bool, str]:
        """
        上传测试数据到服务器

        Args:
            records: 数据库记录列表

        Returns:
            (success, message): 上传结果和消息
        """
        if not records:
            return False, "没有测试数据可上传"

        try:
            payload = self._convert_records(records)
        except Exception as e:
            error_msg = f"上传数据组装失败: {e}"
            logger.error(error_msg, exc_info=True)
            return False, error_msg

        logger.info(f"准备上传 {len(payload)} 条测试数据")
        logger.debug(f"上传数据: {payload}")

        last_message = "上传失败"
        for attempt in range(self.retry_times + 1):
            if attempt:
                logger.warning(f"上传重试 {attempt}/{self.retry_times}，{self.retry_delay} 秒后重试")
                time.sleep(self.retry_delay)

            success, message, retryable = self._post_once(payload)
            if success:
                logger.info("数据上传成功")
                return True, "数据上传成功"

            last_message = message
            if not retryable:
                # 业务错误（如字段不合法）：重试没有意义，直接返回
                logger.error(message)
                return False, message

            logger.warning(f"上传失败（可重试）: {message}")

        logger.error(f"上传失败（已重试 {self.retry_times} 次）: {last_message}")
        return False, last_message

    # ------------------------------------------------------------------ 单次请求

    def _post_once(self, payload: List[Dict]) -> Tuple[bool, str, bool]:
        """发送一次上传请求

        Returns:
            (success, message, retryable): 是否成功 / 失败描述 / 是否值得重试
        """
        try:
            response = requests.post(
                self.api_url,
                json=payload,
                headers=self.HEADERS,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            return False, f"上传超时（{self.timeout} 秒），请检查网络连接", True
        except requests.exceptions.ConnectionError as e:
            return False, f"上传失败：无法连接服务器 {self.api_url}（{e}）", True
        except requests.exceptions.RequestException as e:
            return False, f"上传失败: {e}", True
        except Exception as e:
            return False, f"上传异常: {e}", False

        body = response.text or ""

        # 网关限流/降级页：HTTP 可能仍是 200，但内容不是接口响应
        if self._looks_like_html(body):
            reason = self._extract_gateway_reason(response)
            return False, f"网关返回限流/降级页，非接口响应：{reason}（稍后重试通常即可）", True

        if response.status_code != 200:
            preview = self._preview(body)
            retryable = response.status_code >= 500 or response.status_code == 429
            return False, f"上传失败: HTTP {response.status_code} - {preview}", retryable

        if self._is_success_body(body):
            return True, "数据上传成功", False

        # HTTP 200 但内容不是成功标志：可能是业务错误，也可能是被改写过的响应
        preview = self._preview(body)
        logger.error(f"服务器 200 但响应内容无法识别为成功，正文: {preview}")
        return False, f"上传失败：服务器返回非预期内容 {preview}", True

    # ------------------------------------------------------------------ 响应判定

    @staticmethod
    def _is_success_body(body: str) -> bool:
        """判断响应体是否表示成功

        兼容两种返回形态：
        - 纯文本 "OK"（当前线上接口）
        - JSON {"code": 200, ...}（与 /api/mac/allocate 同风格）
        """
        text = (body or "").strip()
        if not text:
            return False

        # JSON 形态
        if text.startswith('{') or text.startswith('['):
            try:
                data = json.loads(text)
                if isinstance(data, dict) and data.get("code") == 200:
                    return True
            except Exception:
                pass

        # 纯文本形态：整串就是 OK，或短正文里含 OK
        return text.upper() == "OK" or "OK" in text[:64].upper()

    @staticmethod
    def _looks_like_html(body: str) -> bool:
        head = (body or "")[:512].lower()
        return ("<!doctype" in head or "<html" in head
                or ("<div" in head and "</" in head))

    def _extract_gateway_reason(self, response) -> str:
        """从网关错误页里抽出人能看懂的短原因

        页面通常是 GBK 编码，requests 对没有 charset 的 text/html
        会按 ISO-8859-1 解码，于是中文变成 å¾®åº 这种乱码，必须用原始字节重解。
        """
        raw = response.content or b""
        text = None
        # utf-8 与前缀判断都很快；gb18030 几乎不报错，放最后兜底
        for enc in ("utf-8", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        if text is None:
            text = response.text or ""

        # 优先取 <title> 与 <p> 的文本
        parts = []
        for pattern in (r"<title[^>]*>(.*?)</title>", r"<p[^>]*>(.*?)</p>"):
            for m in re.findall(pattern, text, re.S | re.I):
                clean = html.unescape(re.sub(r"<[^>]+>", "", m)).strip()
                if clean and clean not in parts:
                    parts.append(clean)
            if parts:
                break

        if not parts:
            parts = [html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()[:80]]

        return " | ".join(parts)[:120] or "无正文"

    @classmethod
    def _preview(cls, body: str) -> str:
        """截断正文，避免把整页 HTML 写进日志"""
        text = re.sub(r"\s+", " ", (body or "").strip())
        if len(text) > cls.BODY_PREVIEW:
            text = text[:cls.BODY_PREVIEW] + "...(已截断)"
        return text or "(空响应)"

    # ------------------------------------------------------------------ 数据转换

    def _convert_records(self, records: List[Dict]) -> List[Dict]:
        """将数据库记录转换为API格式"""
        payload = []

        device_name_map = {
            'POE': 'POE',
            'DISK': 'DISK',
            '4G': '4G',
            'WIFI': 'WIFI',
            'BLE': 'BLE',
            'SLE': 'SLE',
            'MAC': 'MAC',
            'FAN': 'FAN',
            'HDMI': 'HDMI',
            'BAT': 'BAT'
        }

        for record in records:
            device_name = device_name_map.get(record['sub_type'], record['sub_type'])
            result = "true" if record['results'] == 'PASS' else "false"

            create_time = self._parse_timestamp(record.get('create_time'))
            update_time = self._parse_timestamp(record.get('update_time'))

            payload.append({
                "cur_createTime": create_time,
                "cur_updateTime": update_time,
                "deviceName": device_name,
                "result": result,
                "sn": record['sn']
            })

        return payload

    @staticmethod
    def _parse_timestamp(time_str) -> int:
        """将时间字符串转换为毫秒时间戳"""
        if not time_str:
            return int(datetime.now().timestamp() * 1000)

        try:
            if isinstance(time_str, str):
                dt = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                return int(dt.timestamp() * 1000)
            elif isinstance(time_str, datetime):
                return int(time_str.timestamp() * 1000)
            else:
                return int(datetime.now().timestamp() * 1000)
        except Exception:
            return int(datetime.now().timestamp() * 1000)
