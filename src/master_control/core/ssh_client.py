import paramiko
import socket
import threading
from typing import Tuple, Optional
from src.master_control.utils.logger import logger
from src.master_control.utils.config import config


class SSHClient:
    
    def __init__(self, host: str, username: str = None, password: str = None, port: int = 22, timeout: int = None):
        self.host = host
        self.port = port
        self.username = username or config.ssh_username
        self.password = password or config.ssh_password
        self.timeout = timeout or config.ssh_timeout
        self.client = None
        self.connected = False
        self._lock = threading.RLock()  # 线程安全锁，防止并发执行命令时channel冲突
    
    def connect(self) -> bool:
        """连接SSH服务器，增加重试和详细错误信息"""
        # 如果已有连接，先清理
        if self.client:
            try:
                self.client.close()
            except:
                pass
            self.client = None
        
        self.connected = False
        
        try:
            logger.info(f"开始SSH连接: {self.host}:{self.port} (用户: {self.username}, 超时: {self.timeout}s)")
            
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # 增加banner_timeout和auth_timeout，避免慢速设备连接超时
            self.client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                banner_timeout=self.timeout,  # banner读取超时
                auth_timeout=self.timeout,    # 认证超时
                look_for_keys=False,
                allow_agent=False
            )
            
            # 验证连接是否真正建立
            transport = self.client.get_transport()
            if transport is None or not transport.is_active():
                logger.error(f"SSH连接建立但transport未激活: {self.host}")
                self.client.close()
                self.client = None
                return False
            
            self.connected = True
            logger.info(f"SSH 连接成功: {self.host}")
            return True
            
        except socket.timeout:
            logger.error(f"SSH 连接超时: {self.host} (超过{self.timeout}秒)")
            return False
        except socket.error as e:
            logger.error(f"SSH 网络错误: {self.host} - {e}")
            return False
        except paramiko.AuthenticationException:
            logger.error(f"SSH 认证失败: {self.host} (用户名或密码错误)")
            return False
        except paramiko.SSHException as e:
            logger.error(f"SSH 连接异常: {self.host} - {e}")
            return False
        except Exception as e:
            logger.error(f"SSH 连接失败: {self.host} - {type(e).__name__}: {e}", exc_info=True)
            return False
        finally:
            # 连接失败时清理资源
            if not self.connected and self.client:
                try:
                    self.client.close()
                except:
                    pass
                self.client = None
    
    def execute_command(self, command: str, timeout: int = None) -> Tuple[bool, str, str]:
        if not self.connected:
            logger.error("SSH 未连接，无法执行命令")
            return False, "", "SSH not connected"
        
        # 使用锁保护，防止并发执行命令时channel冲突
        with self._lock:
            try:
                exec_timeout = timeout or self.timeout
                logger.debug(f"执行命令 (timeout={exec_timeout}s): {command}")
                stdin, stdout, stderr = self.client.exec_command(command, timeout=exec_timeout)

                # 设置 channel 读取超时，确保长时间运行的命令不会无限阻塞
                stdout.channel.settimeout(exec_timeout)

                stdout_data = stdout.read().decode('utf-8', errors='ignore').strip()
                stderr_data = stderr.read().decode('utf-8', errors='ignore').strip()
                exit_status = stdout.channel.recv_exit_status()

                success = exit_status == 0
                logger.debug(f"命令执行完成 (退出码: {exit_status})")

                return success, stdout_data, stderr_data
            except socket.timeout:
                logger.error(f"命令执行超时 (>{exec_timeout}s): {command[:50]}...")
                self.connected = False  # 超时后标记连接可能不可用
                return False, "", f"Timeout after {exec_timeout}s"
            except Exception as e:
                logger.error(f"命令执行异常: {e}")
                # 检查是否是严重错误（会话断开）
                if "session not active" in str(e).lower() or "channel" in str(e).lower():
                    self.connected = False
                return False, "", str(e)
    
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """上传文件到远程主机"""
        if not self.connected:
            logger.error("SSH 未连接，无法上传文件")
            return False
        
        # 使用锁保护，防止并发上传时SFTP冲突
        with self._lock:
            try:
                sftp = self.client.open_sftp()
                sftp.put(local_path, remote_path)
                sftp.close()
                logger.info(f"文件上传成功: {local_path} -> {remote_path}")
                return True
            except Exception as e:
                logger.error(f"文件上传失败: {e}")
                return False

    def is_alive(self) -> bool:
        """检查SSH连接是否存活"""
        if not self.client:
            self.connected = False
            return False
        if not self.connected:
            return False
        try:
            transport = self.client.get_transport()
            if transport is None:
                logger.debug(f"SSH连接检测失败: {self.host} - transport is None")
                self.connected = False
                return False
            
            if not transport.is_active():
                logger.debug(f"SSH连接检测失败: {self.host} - transport not active")
                self.connected = False
                return False
            
            # 发送keepalive测试连接
            transport.send_ignore()
            return True
        except EOFError as e:
            logger.debug(f"SSH连接已断开: {self.host} - EOF")
            self.connected = False
            return False
        except Exception as e:
            logger.debug(f"SSH连接检测失败: {self.host} - {type(e).__name__}: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        if self.client:
            self.client.close()
            self.connected = False
            logger.info(f"SSH 连接已关闭: {self.host}")
    
    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
