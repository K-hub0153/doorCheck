import requests
from requests.auth import HTTPBasicAuth
from typing import Optional, Dict, Any
import json
import hashlib
import secrets
import re
import threading
import time
from ..utils.logger import logger


class SpeakerHTTPClient:
    _response_times = {}
    _response_lock = threading.Lock()

    @classmethod
    def last_response_time(cls, ip):
        """任何接口的有效HTTP响应都是在线证据，使用单调时钟比较。"""
        with cls._response_lock:
            return cls._response_times.get(ip, 0)

    def _record_response(self):
        with self._response_lock:
            self._response_times[self.ip] = time.monotonic()

    def __init__(self, ip: str, port: int = 8080, username: str = "admin", password: str = "weidian_24h"):
        self.ip = ip
        self.port = port
        self.base_url = f"http://{ip}:{port}"
        self.username = username
        self.password = password
        self.basic_auth = HTTPBasicAuth(username, password)
        self.default_timeout = 10  # 默认超时10秒
    
    def _parse_www_authenticate(self, auth_header: str) -> Dict[str, str]:
        """解析WWW-Authenticate头"""
        auth_params = {}
        parts = auth_header.split(',')
        
        for part in parts:
            part = part.strip()
            for key in ['realm', 'nonce', 'qop', 'opaque', 'algorithm']:
                if f'{key}="' in part:
                    start = part.index(f'{key}="') + len(key) + 2
                    end = part.index('"', start)
                    auth_params[key] = part[start:end]
                    break
        
        return auth_params
    
    def _generate_digest_auth(self, method: str, uri: str, auth_params: Dict[str, str]) -> str:
        """生成Digest认证头"""
        username = self.username
        password = self.password
        realm = auth_params.get('realm', '')
        nonce = auth_params.get('nonce', '')
        qop = auth_params.get('qop', '')
        opaque = auth_params.get('opaque', '')
        
        cnonce = secrets.token_hex(16)
        nc = '00000001'
        
        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        
        if qop:
            response_hash = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        else:
            response_hash = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        
        auth_str = f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
        auth_str += f'response="{response_hash}"'
        
        if qop:
            auth_str += f', qop={qop}, nc={nc}, cnonce="{cnonce}"'
        if opaque:
            auth_str += f', opaque="{opaque}"'
        
        return auth_str
    
    def _get(self, endpoint: str, params: Optional[Dict] = None, timeout: Optional[int] = None) -> Optional[Dict]:
        try:
            url = f"{self.base_url}{endpoint}"
            logger.debug(f"GET请求: {url}, params={params}")

            actual_timeout = timeout if timeout is not None else self.default_timeout
            prepared_request = requests.Request("GET", url, params=params).prepare()
            digest_uri = prepared_request.path_url
            response = requests.get(url, params=params, timeout=actual_timeout)

            if response.status_code == 401:
                www_auth = response.headers.get('WWW-Authenticate', '')
                logger.debug(f"收到401，WWW-Authenticate: {www_auth}")

                if 'Digest' in www_auth:
                    auth_params = self._parse_www_authenticate(www_auth)
                    # 兼容本地和远程broker：优先尝试带查询参数的URI（本地broker）
                    auth_header = self._generate_digest_auth('GET', digest_uri, auth_params)
                    logger.debug(f"使用Digest认证重试(带参数): {digest_uri}")
                    logger.debug(f"Authorization: ***")

                    headers = {'Authorization': auth_header}
                    response = requests.get(url, params=params, headers=headers, timeout=actual_timeout)

                    # 如果仍然401，尝试不带查询参数的URI（远程broker）
                    if response.status_code == 401 and params:
                        logger.debug(f"带参数认证失败，尝试不带参数的URI（远程broker兼容）")
                        auth_params = self._parse_www_authenticate(response.headers.get('WWW-Authenticate', www_auth))
                        auth_header = self._generate_digest_auth('GET', endpoint, auth_params)
                        logger.debug(f"使用Digest认证重试(不带参数): {endpoint}")

                        headers = {'Authorization': auth_header}
                        response = requests.get(url, params=params, headers=headers, timeout=actual_timeout)
                elif 'Basic' in www_auth:
                    logger.debug(f"使用Basic认证重试: {endpoint}")
                    response = requests.get(url, params=params, auth=self.basic_auth, timeout=actual_timeout)
                else:
                    logger.error(f"不支持的认证类型: {www_auth}")
                    return None

                if response.status_code == 401:
                    try:
                        error_body = response.json()
                        logger.error(f"认证失败 {endpoint}: {error_body.get('message', '用户名或密码错误')}")
                    except:
                        logger.error(f"认证失败 {endpoint}: 用户名或密码错误 (status={response.status_code})")
                    logger.error(f"使用的认证信息 - 用户名: {self.username}, 密码长度: {len(self.password)}")
                    return None
            
            logger.debug(f"HTTP响应状态码: {response.status_code}")
            self._record_response()
            logger.debug(f"HTTP响应头: {dict(response.headers)}")

            # 确保完整读取响应内容
            response.encoding = response.apparent_encoding or 'utf-8'
            content_length = response.headers.get('Content-Length', 'unknown')
            logger.debug(f"Content-Length: {content_length}, 实际读取: {len(response.content)} 字节")

            # 只在DEBUG级别输出响应文本摘要（避免日志过大）
            if len(response.text) > 1000:
                logger.debug(f"HTTP响应文本(前500字符): {response.text[:500]}...")
            else:
                logger.debug(f"HTTP响应原始文本: {response.text}")

            if response.status_code >= 400:
                try:
                    error_body = response.json()
                    logger.error(f"服务器错误 {endpoint} (status={response.status_code}): {error_body}")
                    print(f"服务器错误详情: {error_body}")
                    return error_body
                except:
                    pass
            
            response.raise_for_status()
            
            try:
                return response.json()
            except json.JSONDecodeError as e:
                logger.warning(f"JSON解析失败，尝试修复非法转义: {e}")
                try:
                    text = response.text

                    # 记录原始响应长度和错误位置
                    logger.debug(f"原始响应长度: {len(text)} 字符")
                    logger.debug(f"JSON解析错误位置: {e.pos if hasattr(e, 'pos') else 'unknown'}")

                    # 检查是否被截断（响应长度接近4KB边界）
                    is_truncated = False
                    if len(text) >= 4000 and len(text) <= 4100:
                        logger.warning(f"响应可能被截断（长度={len(text)}），这是设备固件的缓冲区限制问题")
                        is_truncated = True

                    # 尝试多种修复策略

                    # 策略1: 修复 \x 十六进制转义
                    def decode_escape_bytes(match):
                        hex_str = match.group(0).replace('\\x', '')
                        try:
                            byte_data = bytes.fromhex(hex_str)
                            return byte_data.decode('utf-8')
                        except:
                            return match.group(0)

                    fixed_text = re.sub(r'(\\x[0-9a-fA-F]{2})+', decode_escape_bytes, text)

                    # 策略2: 移除控制字符（保留换行、制表符等常见字符）
                    fixed_text = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F]', '', fixed_text)

                    # 策略3: 修复未转义��反斜杠（不在转义序列中的单个反斜杠）
                    # 避免破坏合法的转义序列如 \n \t \" \\
                    fixed_text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', fixed_text)

                    # 策略4: 处理截断的JSON（尝试闭合未完成的结构）
                    if not fixed_text.rstrip().endswith('}'):
                        logger.warning("检测到JSON未正确闭合，尝试修复...")

                        # 找到最后一个完整的对象结束位置
                        # 从后往前扫描，找到截断位置之前的最后一个完整结构
                        truncated_pos = len(fixed_text)

                        # 检查是否在字符串中间被截断
                        # 简单方法：统计所有未转义的引号数量
                        in_string = False
                        escape_next = False
                        for i, char in enumerate(fixed_text):
                            if escape_next:
                                escape_next = False
                                continue
                            if char == '\\':
                                escape_next = True
                                continue
                            if char == '"':
                                in_string = not in_string

                        if in_string:
                            # 我们在字符串中间被截断了，回退到最后一个完整的对象
                            logger.debug("检测到在字符串中间被截断")
                            is_truncated = True
                            # 找到最后一个完整的 }, 或 }]
                            last_complete = max(
                                fixed_text.rfind('},'),
                                fixed_text.rfind('}]'),
                                fixed_text.rfind('"}')
                            )

                            if last_complete > 0:
                                # 截断到最后一个完整的结构
                                if fixed_text[last_complete] == '}' and last_complete + 1 < len(fixed_text):
                                    if fixed_text[last_complete + 1] == ',':
                                        # 移除逗号，准备闭合数组
                                        fixed_text = fixed_text[:last_complete + 1]
                                    elif fixed_text[last_complete + 1] == ']':
                                        # 已经有闭合的数组
                                        fixed_text = fixed_text[:last_complete + 2]
                                else:
                                    fixed_text = fixed_text[:last_complete + 1]

                                logger.debug(f"截断到位置 {last_complete}，移除不完整的数据")
                        else:
                            # 不在字符串中，可能在键值对中间被截断（如 "key": ）
                            logger.debug("检测到在键值对或值中间被截断")
                            is_truncated = True
                            # 找到最后一个完整的对象或数组元素
                            last_complete = max(
                                fixed_text.rfind('},'),
                                fixed_text.rfind('"}'),
                                fixed_text.rfind('],')
                            )

                            if last_complete > 0:
                                # 截断到最后一个完整的结构
                                fixed_text = fixed_text[:last_complete + 1]
                                logger.debug(f"截断到位置 {last_complete}，移除不完整的键值对")

                        # 统计未闭合的大括号和方括号
                        open_braces = fixed_text.count('{') - fixed_text.count('}')
                        open_brackets = fixed_text.count('[') - fixed_text.count(']')

                        # 闭合数组和对象
                        if open_brackets > 0:
                            logger.debug(f"闭合 {open_brackets} 个未闭合的数组")
                            fixed_text += ']' * open_brackets
                        if open_braces > 0:
                            logger.debug(f"闭合 {open_braces} 个未闭合的对象")
                            fixed_text += '}' * open_braces

                    result_data = json.loads(fixed_text)

                    # 如果数据被截断，添加标记
                    if is_truncated and isinstance(result_data, dict):
                        result_data['_truncated'] = True
                        logger.info(f"JSON修复成功，但数据被截断（部分数据丢失）")

                    return result_data
                except Exception as fix_error:
                    logger.error(f"修复JSON失败: {fix_error}")
                    logger.debug(f"原始响应内容类型: {response.headers.get('Content-Type')}")
                    # 输出部分响应内容用于调试（避免日志过大）
                    if len(text) > 500:
                        logger.debug(f"响应内容片段(前200字符): {text[:200]}")
                        logger.debug(f"响应内容片段(后200字符): {text[-200:]}")
                    else:
                        logger.debug(f"完整响应内容: {text}")
                    return None
        except requests.exceptions.Timeout:
            logger.error(f"请求超时: {endpoint}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"请求失败 {endpoint}: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {endpoint}, 错误: {e}")
            return None
    
    def _post(self, endpoint: str, data: Optional[Dict] = None, timeout: Optional[int] = None) -> Optional[Dict]:
        try:
            url = f"{self.base_url}{endpoint}"
            logger.debug(f"POST请求: {url}, data={data}")
            
            actual_timeout = timeout if timeout is not None else self.default_timeout
            response = requests.post(url, json=data, timeout=actual_timeout)
            
            if response.status_code == 401:
                www_auth = response.headers.get('WWW-Authenticate', '')
                logger.debug(f"收到401，WWW-Authenticate: {www_auth}")
                
                if 'Digest' in www_auth:
                    auth_params = self._parse_www_authenticate(www_auth)
                    auth_header = self._generate_digest_auth('POST', endpoint, auth_params)
                    logger.debug(f"使用Digest认证重试: {endpoint}")
                    
                    headers = {'Authorization': auth_header}
                    response = requests.post(url, json=data, headers=headers, timeout=actual_timeout)
                elif 'Basic' in www_auth:
                    logger.debug(f"使用Basic认证重试: {endpoint}")
                    response = requests.post(url, json=data, auth=self.basic_auth, timeout=actual_timeout)
                else:
                    logger.error(f"不支持的认证类型: {www_auth}")
                    return None
                
                if response.status_code == 401:
                    try:
                        error_body = response.json()
                        logger.error(f"认证失败 {endpoint}: {error_body.get('message', '用户名或密码错误')}")
                    except:
                        logger.error(f"认证失败 {endpoint}: 用户名或密码错误 (status={response.status_code})")
                    logger.error(f"使用的认证信息 - 用户名: {self.username}, 密码长度: {len(self.password)}")
                    return None
            
            logger.debug(f"HTTP响应状态码: {response.status_code}")
            self._record_response()
            logger.debug(f"HTTP响应头: {dict(response.headers)}")

            # 确保完整读取响应内容
            response.encoding = response.apparent_encoding or 'utf-8'
            content_length = response.headers.get('Content-Length', 'unknown')
            logger.debug(f"Content-Length: {content_length}, 实际读取: {len(response.content)} 字节")

            # 只在DEBUG级别输出响应文本摘要（避免日志过大）
            if len(response.text) > 1000:
                logger.debug(f"HTTP响应文本(前500字符): {response.text[:500]}...")
            else:
                logger.debug(f"HTTP响应原始文本: {response.text}")

            if response.status_code >= 400:
                try:
                    error_body = response.json()
                    logger.error(f"服务器错误 {endpoint} (status={response.status_code}): {error_body}")
                    print(f"服务器错误详情: {error_body}")
                    return error_body
                except:
                    pass
            
            response.raise_for_status()
            
            try:
                return response.json()
            except json.JSONDecodeError as e:
                logger.warning(f"JSON解析失败，尝试修复非法转义: {e}")
                try:
                    text = response.text

                    # 记录原始响应长度和错误位置
                    logger.debug(f"原始响应长度: {len(text)} 字符")
                    logger.debug(f"JSON解析错误位置: {e.pos if hasattr(e, 'pos') else 'unknown'}")

                    # 检查是否被截断（响应长度接近4KB边界）
                    is_truncated = False
                    if len(text) >= 4000 and len(text) <= 4100:
                        logger.warning(f"响应可能被截断（长度={len(text)}），这是设备固件的缓冲区限制问题")
                        is_truncated = True

                    # 尝试多种修复策略

                    # 策略1: 修复 \x 十六进制转义
                    def decode_escape_bytes(match):
                        hex_str = match.group(0).replace('\\x', '')
                        try:
                            byte_data = bytes.fromhex(hex_str)
                            return byte_data.decode('utf-8')
                        except:
                            return match.group(0)

                    fixed_text = re.sub(r'(\\x[0-9a-fA-F]{2})+', decode_escape_bytes, text)

                    # 策略2: 移除控制字符（保留换行、制表符等常见字符）
                    fixed_text = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F]', '', fixed_text)

                    # 策略3: 修复未转义��反斜杠（不在转义序列中的单个反斜杠）
                    # 避免破坏合法的转义序列如 \n \t \" \\
                    fixed_text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', fixed_text)

                    # 策略4: 处理截断的JSON（尝试闭合未完成的结构）
                    if not fixed_text.rstrip().endswith('}'):
                        logger.warning("检测到JSON未正确闭合，尝试修复...")

                        # 找到最后一个完整的对象结束位置
                        # 从后往前扫描，找到截断位置之前的最后一个完整结构
                        truncated_pos = len(fixed_text)

                        # 检查是否在字符串中间被截断
                        # 简单方法：统计所有未转义的引号数量
                        in_string = False
                        escape_next = False
                        for i, char in enumerate(fixed_text):
                            if escape_next:
                                escape_next = False
                                continue
                            if char == '\\':
                                escape_next = True
                                continue
                            if char == '"':
                                in_string = not in_string

                        if in_string:
                            # 我们在字符串中间被截断了，回退到最后一个完整的对象
                            logger.debug("检测到在字符串中间被截断")
                            is_truncated = True
                            # 找到最后一个完整的 }, 或 }]
                            last_complete = max(
                                fixed_text.rfind('},'),
                                fixed_text.rfind('}]'),
                                fixed_text.rfind('"}')
                            )

                            if last_complete > 0:
                                # 截断到最后一个完整的结构
                                if fixed_text[last_complete] == '}' and last_complete + 1 < len(fixed_text):
                                    if fixed_text[last_complete + 1] == ',':
                                        # 移除逗号，准备闭合数组
                                        fixed_text = fixed_text[:last_complete + 1]
                                    elif fixed_text[last_complete + 1] == ']':
                                        # 已经有闭合的数组
                                        fixed_text = fixed_text[:last_complete + 2]
                                else:
                                    fixed_text = fixed_text[:last_complete + 1]

                                logger.debug(f"截断到位置 {last_complete}，移除不完整的数据")
                        else:
                            # 不在字符串中，可能在键值对中间被截断（如 "key": ）
                            logger.debug("检测到在键值对或值中间被截断")
                            is_truncated = True
                            # 找到最后一个完整的对象或数组元素
                            last_complete = max(
                                fixed_text.rfind('},'),
                                fixed_text.rfind('"}'),
                                fixed_text.rfind('],')
                            )

                            if last_complete > 0:
                                # 截断到最后一个完整的结构
                                fixed_text = fixed_text[:last_complete + 1]
                                logger.debug(f"截断到位置 {last_complete}，移除不完整的键值对")

                        # 统计未闭合的大括号和方括号
                        open_braces = fixed_text.count('{') - fixed_text.count('}')
                        open_brackets = fixed_text.count('[') - fixed_text.count(']')

                        # 闭合数组和对象
                        if open_brackets > 0:
                            logger.debug(f"闭合 {open_brackets} 个未闭合的数组")
                            fixed_text += ']' * open_brackets
                        if open_braces > 0:
                            logger.debug(f"闭合 {open_braces} 个未闭合的对象")
                            fixed_text += '}' * open_braces

                    result_data = json.loads(fixed_text)

                    # 如果数据被截断，添加标记
                    if is_truncated and isinstance(result_data, dict):
                        result_data['_truncated'] = True
                        logger.info(f"JSON修复成功，但数据被截断（部分数据丢失）")

                    return result_data
                except Exception as fix_error:
                    logger.error(f"修复JSON失败: {fix_error}")
                    logger.debug(f"原始响应内容类型: {response.headers.get('Content-Type')}")
                    # 输出部分响应内容用于调试（避免日志过大）
                    if len(text) > 500:
                        logger.debug(f"响应内容片段(前200字符): {text[:200]}")
                        logger.debug(f"响应内容片段(后200字符): {text[-200:]}")
                    else:
                        logger.debug(f"完整响应内容: {text}")
                    return None
        except requests.exceptions.Timeout:
            logger.error(f"请求超时: {endpoint}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"请求失败 {endpoint}: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {endpoint}, 错误: {e}")
            return None
    
    def health_check(self) -> bool:
        result = self._get("/hi")
        return result is not None and result.get("message") == "Hello World!"
    
    def get_mac_addresses(self) -> Optional[Dict]:
        return self._get("/api/system/get_mac")
    
    def set_mac_addresses(self, wifi_mac: Optional[str] = None, starflash_mac: Optional[str] = None) -> Optional[Dict]:
        data = {}
        if wifi_mac:
            data["mac2"] = wifi_mac
        if starflash_mac:
            data["mac3"] = starflash_mac
        return self._post("/api/system/set_mac", data)
    
    def get_version(self) -> Optional[Dict]:
        return self._get("/api/system/version")
    
    def set_factory_mode(self) -> Optional[Dict]:
        return self._get("/api/env/set_factory")
    
    def clear_factory_mode(self) -> Optional[Dict]:
        return self._get("/api/env/clear_factory")
    
    def get_factory_mode(self) -> Optional[Dict]:
        return self._get("/api/env/get_factory")
    
    def stop_audio(self) -> Optional[Dict]:
        return self._get("/api/audio/stop")
    
    def set_ao_volume(self, volume_db: int) -> Optional[Dict]:
        return self._get("/api/audio/ao/volume", {"volume_db": volume_db})
    
    def set_ai_volume(self, volume_db: int) -> Optional[Dict]:
        return self._get("/api/audio/ai/volume", {"volume_db": volume_db})
    
    def mic_record_play(self, duration: int = 5) -> Optional[Dict]:
        # 录音需要duration秒 + 播放duration秒 + 设备处理时间，预留充足超时
        return self._get("/api/audio/mic_record_play", {"duration": duration}, timeout=duration * 2 + 15)

    def play_audio(self) -> Optional[Dict]:
        """播放音频测试喇叭"""
        # 播放音频需要等待音频播放完成，预留充足超时
        return self._get("/api/audio/play", timeout=15)

    def set_factory(self) -> Optional[Dict]:
        """设置为出厂模式"""
        return self._get("/api/env/set_factory")

    def scan_sle(self, duration: int = 8) -> Optional[Dict]:
        return self._get("/api/scan/sle", {"duration": duration}, timeout=duration + 12)
    
    def scan_bluetooth(self, duration: int = 5) -> Optional[Dict]:
        return self._get("/api/scan/bluetooth", {"duration": duration}, timeout=duration + 20)
    
    def scan_wifi(self, duration: int = 8) -> Optional[Dict]:
        return self._get("/api/scan/wifi", {"duration": duration}, timeout=duration + 12)
    
    def get_adc_voltage(self, channel: int = 1) -> Optional[Dict]:
        return self._get("/api/adc/voltage", {"channel": channel})
    
    def send_ir_blaster(self, address: str, command: str, carrier_freq: int = 38000, 
                       duty_cycle: int = 33, repeat: int = 2) -> Optional[Dict]:
        data = {
            "address": address,
            "command": command,
            "carrier_freq": carrier_freq,
            "duty_cycle": duty_cycle,
            "repeat": repeat
        }
        return self._post("/api/ir/blaster", data)
    
    def read_microwave(self, duration: int = 5, interval: int = 200) -> Optional[Dict]:
        return self._get('/api/micro/read', params={'duration': duration, 'interval': interval}, timeout=duration + 12)
    
    def microwave_cmd(self, cmd: str, **kwargs) -> Optional[Dict]:
        params = {"cmd": cmd}
        params.update(kwargs)
        return self._get("/api/micro/cmd", params)

    def get_microwave_version(self) -> Optional[Dict]:
        """获取微波感应器固件版本"""
        return self.microwave_cmd("read_version")
    
    def get_stream_urls(self) -> Optional[Dict]:
        return self._get("/api/stream/urls")
    
    def reboot(self) -> Optional[Dict]:
        return self._get("/api/sys/reboot")

    def ota_upgrade(self, config: str, images: str, timeout: int = 30) -> Optional[Dict]:
        """触发OTA固件在线升级
        config: 配置字符串，格式 wget_server=http://IP:8000,timeout=秒数
        images: 镜像列表，多个镜像用分号分隔
                每个镜像格式: 版本号,镜像类型,MD5,文件名,文件大小(字节)
                示例: v1.0.0.1,rootfs,f3bbb211...,rootfs_hi3516cv610.ubifs,52428800
        """
        return self._post("/api/ota/upgrade", {"config": config, "images": images}, timeout=timeout)

    def version_set(self, version: str) -> Optional[Dict]:
        """设置固件版本号配置
        version: 版本号，如 1.0.0.1
        """
        return self._get("/api/config/version_set", {"firmwareversion": version})
