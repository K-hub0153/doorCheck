import hmac
import hashlib
import json
import uuid
import secrets
import base64

from ..utils.logger import logger


def generate_nonce(length: int = 16) -> str:
    return secrets.token_hex(length // 2)


def generate_message_id() -> str:
    return uuid.uuid4().hex


def calculate_hmac_signature(data: str, key: str) -> str:
    signature = hmac.new(
        key.encode('utf-8'),
        data.encode('utf-8'),
        hashlib.sha256
    ).digest()
    return base64.b64encode(signature).decode('utf-8')


def _cjson_serialize(obj) -> str:
    """序列化为cJSON_PrintUnformatted兼容格式。

    对象: key紧凑排列，无空格
    数组: 元素之间逗号后有空格 ", "
    """
    if isinstance(obj, dict):
        items = []
        for k in sorted(obj.keys()):
            items.append(f'"{k}":{_cjson_serialize(obj[k])}')
        return "{" + ",".join(items) + "}"
    elif isinstance(obj, list):
        elements = [_cjson_serialize(item) for item in obj]
        return "[" + ", ".join(elements) + "]"
    elif isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif isinstance(obj, int):
        return str(obj)
    elif isinstance(obj, float):
        return str(obj)
    elif obj is None:
        return "null"
    else:
        return json.dumps(obj, ensure_ascii=False)


def body_to_sign_json(body: dict) -> str:
    """将body序列化为签名用的JSON字符串，匹配设备端cJSON格式。"""
    return _cjson_serialize(body)


def build_sign_data(ver: str, mid: str, ts: int, action: str, body: str, nonce: str, psk: str) -> str:
    return f"{ver}{mid}{ts}{action}{body}{nonce}{psk}"


def verify_signature(message: dict, psk: str) -> bool:
    try:
        header = message.get('header', {})
        body = message.get('body', {})

        body_json = body_to_sign_json(body)

        sign_data = build_sign_data(
            header.get('ver', ''),
            header.get('mid', ''),
            header.get('ts', 0),
            header.get('action', ''),
            body_json,
            header.get('nonce', ''),
            psk
        )

        expected_sig = calculate_hmac_signature(sign_data, psk)
        actual_sig = header.get('sig', '')

        return expected_sig == actual_sig
    except Exception:
        return False


# ==================== MQTT 凭据加密 ====================
#
# 门锁固件（v1.1.7+）在 wd_http_client.c 的 wd_http_parse_mqtt_config() 里
# 要求 mqtt.username / mqtt.password 必须非空，否则整份配置判失败，设备会
# 回到 discovery 重试循环（表现为设备反复请求配置、始终不连 MQTT）。
#
# 固件侧（wd_mqtt_server.c）拿到这两个字段后会先调 wd_decrypt_mqtt_credential()
# 做 Base64 -> AES-128-ECB 解密，解密失败才退回明文。密钥硬编码在 wd_crypto.h：
#
#     WD_AES_KEY = "WD24H-EDGE-MQTT!"    (16 bytes, AES-128)
#     WD_PSK     = "weidian_24h"
#
# 所以下发的必须是 base64(AES-128-ECB(key, pkcs7_pad(明文)))。
# 与固件源码里示例 JSON 的 username "0HGvc4CQeVg8aP7c4ZO6hg==" 对得上（解出 "admin"）。
#
# 音箱工具不使用这两个函数，也不需要装 pycryptodome。

# 固件端 wd_crypto.h 的 WD_AES_KEY，16 字节，AES-128
FIRMWARE_AES_KEY = b"WD24H-EDGE-MQTT!"

_AES_MODULE = None


def _get_aes():
    """延迟导入 pycryptodome。

    只在真正需要加密时才导入，避免音箱工具因为缺少该依赖而启动失败。
    """
    global _AES_MODULE
    if _AES_MODULE is None:
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import pad
            _AES_MODULE = (AES, pad)
        except ImportError as e:
            logger.error(
                f"缺少 pycryptodome 依赖，无法加密MQTT凭据: {e}；"
                f"门锁固件(v1.1.7+)会因此拒绝配置，请执行 pip install pycryptodome"
            )
            _AES_MODULE = False
    return _AES_MODULE or None


def encrypt_mqtt_credential(plaintext: str) -> str:
    """把 MQTT 凭据加密成固件期望的 base64 串。

    Args:
        plaintext: 明文凭据（如 PSK）

    Returns:
        base64 编码的 AES-128-ECB 密文；明文为空或依赖缺失时返回空串
    """
    if not plaintext:
        return ""

    aes = _get_aes()
    if aes is None:
        return ""

    AES, pad = aes
    try:
        cipher = AES.new(FIRMWARE_AES_KEY, AES.MODE_ECB)
        encrypted = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
        return base64.b64encode(encrypted).decode("utf-8")
    except Exception as e:
        logger.error(f"MQTT凭据加密失败: {e}")
        return ""


def mask_credential(credential: str) -> str:
    """生成用于日志的脱敏标记。

    只暴露长度和短哈希，不落明文、也不落完整密文。
    """
    if not credential:
        return "<empty>"

    digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:8]
    return f"<len={len(credential)} sha256={digest}>"
