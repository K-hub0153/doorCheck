import os
import shutil
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

from ..utils.logger import logger


class FirmwareHTTPServer:
    """固件HTTP文件服务，供设备通过 wget http://<IP>:8000/<文件名> 拉取固件。

    与 config_server(8081)/tftp(69) 并存，仅托管本次升级用到的固件文件。
    注意: 该服务对局域网开放且无鉴权，仅用于产测内网环境。
    """

    def __init__(self, host: str = '0.0.0.0', port: int = 8000, serve_dir: str = None):
        self.host = host
        self.port = port
        self.httpd = None
        self.server_thread = None

        if serve_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            serve_dir = os.path.join(base_dir, 'data', 'firmware_serve')
        self.serve_dir = serve_dir
        os.makedirs(self.serve_dir, exist_ok=True)

    def get_server_ip(self) -> str:
        """探测本机局域网IP"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return socket.gethostbyname(socket.gethostname())

    def add_firmware(self, firmware_path: str) -> str:
        """将固件拷贝到托管目录，返回可供下载的文件名(basename)"""
        filename = os.path.basename(firmware_path)
        dest = os.path.join(self.serve_dir, filename)
        if os.path.abspath(firmware_path) != os.path.abspath(dest):
            shutil.copy2(firmware_path, dest)
        logger.info(f"固件已托管: {filename} -> {dest}")
        return filename

    def clear_firmware_cache(self):
        """清空固件托管目录中的所有文件"""
        try:
            if os.path.exists(self.serve_dir):
                for filename in os.listdir(self.serve_dir):
                    filepath = os.path.join(self.serve_dir, filename)
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                        logger.debug(f"已删除旧固件: {filename}")
                logger.info(f"固件缓存目录已清空: {self.serve_dir}")
        except Exception as e:
            logger.warning(f"清空固件缓存失败: {e}")

    def start(self):
        try:
            handler = partial(_QuietHandler, directory=self.serve_dir)
            self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
            self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.server_thread.start()
            logger.info(f"固件HTTP服务已启动: {self.host}:{self.port} (目录: {self.serve_dir})")
        except OSError as e:
            logger.error(f"固件HTTP服务启动失败(端口{self.port}可能被占用): {e}")
            self.httpd = None
        except Exception as e:
            logger.error(f"固件HTTP服务启动失败: {e}")
            self.httpd = None

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
                logger.info("固件HTTP服务已停止")
            except Exception as e:
                logger.warning(f"停止固件HTTP服务失败: {e}")
            finally:
                self.httpd = None


class _QuietHandler(SimpleHTTPRequestHandler):
    """静默日志的文件处理器，避免污染控制台"""

    def log_message(self, format, *args):
        logger.debug("固件HTTP: " + (format % args))
