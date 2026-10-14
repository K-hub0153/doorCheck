import sys
import os
import platform
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QTextEdit, QLabel, QMessageBox,
                             QGroupBox, QFrame, QGridLayout, QTabWidget, QInputDialog,
                             QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
                             QSizePolicy, QComboBox, QProgressBar, QApplication, QDialog, QActionGroup)
from PyQt5.QtCore import Qt, pyqtSignal, QThread, QTimer, QProcess, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QFont, QColor, QPixmap, QImage, QPalette, QLinearGradient, QFontDatabase
import threading
import time
import subprocess
from zeroconf import Zeroconf, ServiceBrowser
import imageio_ffmpeg

from ..network.device_info import DeviceInfo, DEVICE_TYPE_SMART_HORN, DEVICE_TYPE_OUTDOOR_SMART_HORN
from ..network.mdns_discovery import DeviceDiscoveryListener, MasterMdnsService, DebugServiceListener
from ..network.ip_scanner import IPScanner
from ..network.speaker_http_client import SpeakerHTTPClient
from ..core.speaker_test_engine import SpeakerTestEngine
from ..core.speaker_test_result import TestStatus
from ..hardware.speaker_label_printer import LabelPrinter
from ..network.http_server import ConfigServer
from ..network.mqtt_broker import MQTTBrokerManager
from ..network.firmware_server import FirmwareHTTPServer
from ..utils.config import Config
from ..utils.logger import logger
from ..utils.mac_allocator import MACAllocator
from ..data.speaker_test_database import TestRecordDB
from ..utils.test_log_capture import TestLogCapture, PanelLogHandler
from .test_results_window import TestResultsWindow
import re


def _strip_v(version: str) -> str:
    """去掉版本号前缀的 v/V，用于版本比对"""
    if not version:
        return ''
    return re.sub(r'^[vV]', '', str(version).strip())


def _compare_version(version1: str, version2: str) -> int:
    """比较两个版本号
    返回值: 1 表示 version1 > version2, 0 表示相等, -1 表示 version1 < version2
    """
    try:
        v1 = _strip_v(version1)
        v2 = _strip_v(version2)

        parts1 = [int(x) for x in v1.split('.')]
        parts2 = [int(x) for x in v2.split('.')]

        # 补齐长度
        max_len = max(len(parts1), len(parts2))
        parts1.extend([0] * (max_len - len(parts1)))
        parts2.extend([0] * (max_len - len(parts2)))

        for p1, p2 in zip(parts1, parts2):
            if p1 > p2:
                return 1
            elif p1 < p2:
                return -1
        return 0
    except Exception as e:
        logger.error(f"版本号比较失败: {version1} vs {version2}, 错误: {e}")
        return -1


class VideoStreamThread(QThread):
    """视频流读取线程，隔离阻塞IO避免主线程冻结"""
    frame_ready = pyqtSignal(bytes)  # 新帧数据
    stream_timeout = pyqtSignal()    # 超时（10秒无数据）
    stream_error = pyqtSignal(str)   # 读取错误

    def __init__(self, ffmpeg_process, width, height):
        super().__init__()
        self.ffmpeg_process = ffmpeg_process
        self.width = width
        self.height = height
        self.frame_size = width * height * 3
        self.running = False
        self.skip_initial_frames = 5  # 跳过前5帧，等待清晰的关键帧
        self._lock = threading.Lock()  # 保护 ffmpeg_process 访问

    def run(self):
        """工作线程：循环读取ffmpeg输出"""
        self.running = True
        last_frame_time = time.time()
        first_frame = True

        try:
            while self.running:
                # 线程安全地检查进程状态
                with self._lock:
                    if not self.ffmpeg_process:
                        break
                    if self.ffmpeg_process.poll() is not None:
                        break
                    process = self.ffmpeg_process

                # 检查超时（首帧15秒，后续10秒无数据）
                timeout = 15.0 if first_frame else 10.0
                if time.time() - last_frame_time > timeout:
                    logger.warning(f"视频流超时 ({timeout}秒无数据)，设备可能断电或RTSP服务未就绪")
                    self.stream_timeout.emit()
                    break

                try:
                    # 阻塞读取（在工作线程中安全）
                    # 使用短超时避免长时间阻塞
                    raw_frame = process.stdout.read(self.frame_size)

                    if len(raw_frame) == 0:
                        # 管道关闭，读取stderr获取错误信息
                        stderr_output = ""
                        try:
                            with self._lock:
                                if self.ffmpeg_process:
                                    stderr_output = self.ffmpeg_process.stderr.read().decode('utf-8', errors='ignore')
                                    if stderr_output:
                                        logger.error(f"ffmpeg错误输出: {stderr_output[-500:]}")  # 只记录最后500字符
                        except Exception as e:
                            logger.warning(f"无法读取ffmpeg错误输出: {e}")

                        logger.info("ffmpeg管道关闭")
                        error_msg = "视频流中断"
                        if "Connection refused" in stderr_output:
                            error_msg = "RTSP连接被拒绝，设备可能未启动"
                        elif "timed out" in stderr_output or "timeout" in stderr_output:
                            error_msg = "RTSP连接超时"
                        elif "401" in stderr_output or "Unauthorized" in stderr_output:
                            error_msg = "RTSP认证失败"

                        self.stream_error.emit(error_msg)
                        break
                    elif len(raw_frame) == self.frame_size:
                        # 完整帧
                        last_frame_time = time.time()

                        if first_frame:
                            logger.info(f"视频流首帧已接收，跳过前{self.skip_initial_frames}帧以等待清晰画面")
                            first_frame = False

                        if self.skip_initial_frames > 0:
                            self.skip_initial_frames -= 1
                            if self.skip_initial_frames == 0:
                                logger.info("初始帧跳过完成，开始显示清晰画面")
                            continue

                        # 只在线程仍在运行时发送帧
                        if self.running:
                            self.frame_ready.emit(raw_frame)
                    else:
                        # 不完整数据
                        logger.warning(f"读取到不完整帧: {len(raw_frame)}/{self.frame_size}")

                except Exception as e:
                    if self.running:  # 只在非主动停止时报错
                        logger.error(f"读取视频帧异常: {e}")
                        self.stream_error.emit(f"读取失败: {e}")
                    break

        except Exception as e:
            logger.error(f"视频流线程异常: {e}")
            if self.running:
                self.stream_error.emit(str(e))
        finally:
            self.running = False
            # 清空进程引用，避免悬挂指针
            with self._lock:
                self.ffmpeg_process = None

    def stop(self):
        """停止线程"""
        self.running = False
        # 清空进程引用
        with self._lock:
            self.ffmpeg_process = None
        # 等待线程结束，但不要等太久
        self.wait(1000)  # 最多等待1秒


# 现代化UI样式配置
class UIStyles:
    """统一的UI样式配置"""
    
    # 配色方案（统一门控风格）
    PRIMARY = "#2196F3"       # 主题蓝
    PRIMARY_DARK = "#1976D2"  # 主题蓝深
    PRIMARY_LIGHT = "#e3f2fd" # 主题蓝浅
    SUCCESS = "#4CAF50"       # 成功绿
    WARNING = "#FF9800"       # 警告橙
    ERROR = "#F44336"         # 错误红
    INFO = "#2196F3"          # 信息蓝

    BACKGROUND = "#F5F7FA"    # 背景灰
    CARD_BG = "#FFFFFF"       # 卡片白
    SURFACE = "#FAFAFA"       # 表面灰
    BORDER = "#DDDDDD"        # 边框灰（统一为#ddd）

    TEXT_PRIMARY = "#333333"  # 主文本（统一为#333）
    TEXT_SECONDARY = "#666666" # 次要文本（统一为#666）
    TEXT_DISABLED = "#999999"  # 禁用文本（统一为#999）
    
    # 阴影效果
    SHADOW_LIGHT = "0 2px 4px rgba(0,0,0,0.1)"
    SHADOW_MEDIUM = "0 4px 8px rgba(0,0,0,0.15)"
    SHADOW_HEAVY = "0 8px 16px rgba(0,0,0,0.2)"
    
    @staticmethod
    def get_system_font():
        """获取系统适配字体"""
        system = platform.system()
        if system == "Windows":
            return ["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Arial"]
        elif system == "Darwin":  # macOS
            return ["PingFang SC", "Helvetica Neue", "Arial"]
        else:  # Linux
            return ["Noto Sans CJK SC", "DejaVu Sans", "Arial"]
    
    @staticmethod
    def get_dpi_scale():
        """获取DPI缩放比例"""
        try:
            app = QApplication.instance()
            if app:
                screen = app.primaryScreen()
                dpi = screen.logicalDotsPerInch()
                return max(1.0, dpi / 96.0)  # 96 DPI 为基准
        except:
            pass
        return 1.0
    
    @staticmethod
    def scale_size(size):
        """根据DPI缩放尺寸"""
        return int(size * UIStyles.get_dpi_scale())
    
    @staticmethod
    def get_font(size=10, bold=False):
        """获取适配字体"""
        fonts = UIStyles.get_system_font()
        scaled_size = UIStyles.scale_size(size)
        
        # 获取系统可用字体列表
        font_db = QFontDatabase()
        available_families = font_db.families()
        
        for font_name in fonts:
            if font_name in available_families:
                font = QFont(font_name, scaled_size)
                if bold:
                    font.setBold(True)
                return font
        
        # 回退到系统默认字体
        font = QFont()
        font.setPointSize(scaled_size)
        if bold:
            font.setBold(True)
        return font
    
    @staticmethod
    def get_button_style(color=None, hover_color=None):
        if color is None:
            color = UIStyles.PRIMARY
        if hover_color is None:
            hover_color = UIStyles.PRIMARY_DARK
            
        button_height = UIStyles.scale_size(40)
        font_size = UIStyles.scale_size(14)
        padding_v = UIStyles.scale_size(12)
        padding_h = UIStyles.scale_size(24)
        border_radius = UIStyles.scale_size(8)
        
        return f"""
            QPushButton {{
                background-color: {color};
                color: white;
                border: none;
                border-radius: {border_radius}px;
                padding: {padding_v}px {padding_h}px;
                font-size: {font_size}px;
                font-weight: 600;
                min-height: {button_height}px;
            }}
            QPushButton:hover {{
                background-color: {hover_color};
            }}
            QPushButton:pressed {{
                background-color: {color};
                padding-top: {padding_v + 2}px;
                padding-bottom: {padding_v - 2}px;
            }}
            QPushButton:disabled {{
                background-color: #BDBDBD;
                color: #757575;
            }}
        """
    
    @staticmethod
    def get_card_style():
        border_radius = UIStyles.scale_size(12)
        return f"""
            QFrame {{
                background-color: #FFFFFF;
                border: 1px solid #E0E0E0;
                border-radius: {border_radius}px;
            }}
        """


class ClickableVideoLabel(QLabel):
    doubleClicked = pyqtSignal()

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class VideoZoomDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('视频放大')
        self.setModal(False)

        # 使用DPI缩放的尺寸
        zoom_width = UIStyles.scale_size(1280)
        zoom_height = UIStyles.scale_size(720)
        self.resize(zoom_width, zoom_height)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self.video_label = QLabel()
        self.video_label.setScaledContents(True)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet("background-color: black;")
        layout.addWidget(self.video_label)

    def update_frame(self, pixmap):
        self.video_label.setPixmap(pixmap)


class VideoWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        # 重试计数器
        self.retry_count = 0
        self.max_retries = 3

        # 防止短时间内重复启动（修复崩溃问题）
        self.last_start_time = 0
        self.min_start_interval = 1.0  # 最小启动间隔1秒

        # 使用DPI缩放尺寸
        min_width = UIStyles.scale_size(320)
        min_height = UIStyles.scale_size(240)
        max_width = UIStyles.scale_size(400)
        max_height = UIStyles.scale_size(300)
        video_width = UIStyles.scale_size(400)
        video_height = UIStyles.scale_size(225)

        self.setMinimumSize(min_width, min_height)
        self.setMaximumSize(max_width, max_height)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self.video_label = ClickableVideoLabel()
        self.video_label.setMinimumSize(video_width, video_height)
        self.video_label.setMaximumSize(video_width, video_height)
        self.video_label.setScaledContents(True)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.doubleClicked.connect(self.show_fullsize_video)

        border_radius = UIStyles.scale_size(12)
        font_size = UIStyles.scale_size(14)
        self.video_label.setStyleSheet(f"""
            QLabel {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #37474F, stop:1 #263238);
                border: 2px solid {UIStyles.BORDER};
                border-radius: {border_radius}px;
                color: white;
                font-size: {font_size}px;
                font-weight: 500;
            }}
        """)
        self.video_label.setText('📹 暂无视频')
        layout.addWidget(self.video_label)

        button_font_size = UIStyles.scale_size(12)
        button_padding = UIStyles.scale_size(8)
        button_radius = UIStyles.scale_size(6)
        self.retry_button = QPushButton('🔄 重新连接视频')
        self.retry_button.setStyleSheet(f"""
            QPushButton {{
                background-color: #FF9800;
                color: white;
                border: none;
                border-radius: {button_radius}px;
                padding: {button_padding}px {button_padding * 2}px;
                font-size: {button_font_size}px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background-color: #F57C00;
            }}
            QPushButton:pressed {{
                background-color: #E65100;
            }}
        """)
        self.retry_button.hide()
        layout.addWidget(self.retry_button)
        
        self.ffmpeg_process = None
        self.video_stream_thread = None  # 视频流读取线程
        self.device_ip = None
        self.original_pixmap = None
        self.video_width = 1280
        self.video_height = 720
        self.zoom_dialog = None
        
    def show_default_image(self):
        self.stop_stream()
        self.video_label.setText('📹 暂无视频')
        self.retry_button.hide()
        self.device_ip = None
        
    def start_rtsp_stream(self, device_ip, retry_count=0):
        # 防止短时间内重复启动
        current_time = time.time()
        if current_time - self.last_start_time < self.min_start_interval:
            logger.warning(f"[视频流] 启动间隔过短 ({current_time - self.last_start_time:.2f}秒)，跳过本次启动")
            return False
        self.last_start_time = current_time

        self.stop_stream()
        self.device_ip = device_ip
        self.retry_button.hide()

        # 如果是新的设备IP，重置重试计数
        if retry_count == 0:
            self.retry_count = 0
        else:
            self.retry_count = retry_count

        # 检查是否超过最大重试次数
        if self.retry_count >= self.max_retries:
            logger.error(f"[视频流] 已达到最大重试次数 ({self.max_retries})，停止重试")
            self.video_label.setText(f'❌ 视频流连接失败\\n已重试{self.max_retries}次')
            self.retry_button.show()
            return False

        rtsp_url = f'rtsp://admin:weidian_24h@{device_ip}/camera0/main'

        logger.info(f"[视频流] 准备连接RTSP流")
        logger.info(f"[视频流] 设备IP: {device_ip}")
        logger.info(f"[视频流] 完整RTSP URL: {rtsp_url}")
        logger.info(f"[视频流] 说明: 直接从室外音箱内置摄像头拉取主码流")

        try:
            if self.retry_count == 0:
                self.video_label.setText('📹 正在连接视频流...')
            else:
                self.video_label.setText(f'📹 正在重试连接({self.retry_count}/{self.max_retries})...')

            # Windows 下隐藏 ffmpeg 黑框窗口
            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            # 简化参数配置，类似ffplay方式
            ffmpeg_cmd = [
                ffmpeg_exe,
                '-rtsp_transport', 'tcp',       # TCP传输方式
                '-timeout', '5000000',          # 5秒连接超时（微秒）
                '-i', rtsp_url,
                '-an',                          # 禁用音频
                '-f', 'image2pipe',             # 输出格式：图像管道
                '-pix_fmt', 'rgb24',            # 像素格式
                '-vcodec', 'rawvideo',          # 原始视频编码
                '-'
            ]

            # 打印ffmpeg命令（隐藏密码）
            cmd_display = ' '.join(ffmpeg_cmd).replace('weidian_24h', '***')
            logger.info(f"[视频流] ffmpeg命令: {cmd_display}")

            self.ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=65536,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )

            # 启动视频流读取线程
            self.video_stream_thread = VideoStreamThread(self.ffmpeg_process, self.video_width, self.video_height)
            self.video_stream_thread.frame_ready.connect(self.on_frame_ready)
            self.video_stream_thread.stream_timeout.connect(self.on_stream_timeout)
            self.video_stream_thread.stream_error.connect(self.on_stream_error)
            self.video_stream_thread.start()

            logger.info(f"视频流线程已启动: {device_ip} (尝试 {self.retry_count + 1})")
            return True
        except Exception as e:
            logger.error(f"视频流连接失败 (尝试 {self.retry_count + 1}): {e}")

            # 自动重试最多3次
            if self.retry_count < self.max_retries:
                logger.info(f"将在2秒后重试视频流连接...")
                QTimer.singleShot(2000, lambda ip=device_ip: self.start_rtsp_stream(ip, self.retry_count + 1))
                return False
            else:
                self.video_label.setText(f'❌ 视频流连接失败\\n{str(e)}')
                self.retry_button.show()
                return False
    
    def on_frame_ready(self, raw_frame):
        """处理新帧数据（在主线程中）"""
        try:
            image = QImage(raw_frame, self.video_width, self.video_height, self.video_width * 3, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(image)
            self.original_pixmap = pixmap
            self.video_label.setPixmap(pixmap)
            if self.zoom_dialog and self.zoom_dialog.isVisible():
                self.zoom_dialog.update_frame(pixmap)
        except Exception as e:
            logger.error(f"显示视频帧失败: {e}")
    
    def on_stream_timeout(self):
        """处理视频流超时，带重试机制"""
        self.stop_stream()

        logger.warning(f"视频流超时 (已尝试 {self.retry_count + 1} 次)")

        # 自动重试
        if self.retry_count < self.max_retries and self.device_ip:
            logger.info(f"将在2秒后重试视频流连接...")
            QTimer.singleShot(2000, lambda ip=self.device_ip: self.start_rtsp_stream(ip, self.retry_count + 1))
        else:
            # 达到最大重试次数或没有设备IP
            self.video_label.setText('❌ 视频流超时（设备断电？）')
            if self.device_ip:
                self.retry_button.show()
    
    def on_stream_error(self, error_msg):
        """处理视频流错误，带重试机制"""
        self.stop_stream()

        logger.warning(f"视频流错误: {error_msg} (已尝试 {self.retry_count + 1} 次)")

        # 自动重试
        if self.retry_count < self.max_retries and self.device_ip:
            logger.info(f"将在2秒后重试视频流连接...")
            QTimer.singleShot(2000, lambda ip=self.device_ip: self.start_rtsp_stream(ip, self.retry_count + 1))
        else:
            # 达到最大重试次数或没有设备IP
            self.video_label.setText(f'❌ {error_msg}')
            if self.device_ip:
                self.retry_button.show()
    
    def stop_stream(self):
        """停止视频流（关键：先停止进程，再停止线程）"""
        # 1. 先终止 ffmpeg 进程（这会让线程的 read() 操作返回）
        if self.ffmpeg_process:
            try:
                # 先尝试正常终止
                self.ffmpeg_process.terminate()
                try:
                    self.ffmpeg_process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    # 超时则强制杀死
                    self.ffmpeg_process.kill()
                    try:
                        self.ffmpeg_process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        # kill都超时，直接放弃等待
                        logger.warning("ffmpeg进程无法终止，放弃等待")
            except Exception as e:
                logger.debug(f"停止ffmpeg进程异常: {e}")
                try:
                    self.ffmpeg_process.kill()
                except:
                    pass
            finally:
                self.ffmpeg_process = None

        # 2. 再停止视频流线程（此时 ffmpeg 已经停止，线程会安全退出）
        if self.video_stream_thread and self.video_stream_thread.isRunning():
            self.video_stream_thread.stop()
            # 不要立即设为 None，等线程真正结束
            if not self.video_stream_thread.wait(1000):
                logger.warning("视频流线程未能在1秒内结束")
            self.video_stream_thread = None

    def show_fullsize_video(self):
        if not self.zoom_dialog:
            self.zoom_dialog = VideoZoomDialog(self)
        if self.original_pixmap:
            self.zoom_dialog.update_frame(self.original_pixmap)
        self.zoom_dialog.show()
        self.zoom_dialog.raise_()
        self.zoom_dialog.activateWindow()


class DeviceCard(QFrame):
    clicked = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    assign_window_requested = pyqtSignal(object)  # 新增：请求分配窗口信号

    def __init__(self, device, versions=None, parent=None):
        super().__init__(parent)
        self.device = device
        self.versions = versions or {}
        self.selected = False
        self.checked = False  # 新增：是否被勾选（用于批量操作）
        # 保存版本信息显示的标签引用，用于动态更新
        self.kernel_label = None
        self.rootfs_label = None
        self.pending_label = None
        self.app_label = None  # 新增：保存app版本标签引用
        self.info_layout = None
        self.checkbox = None  # 新增：复选框引用
        self.setup_ui()

    def setup_ui(self):
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        # 按内容自适应高度，不被布局压缩
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        layout = QVBoxLayout()
        mh = UIStyles.scale_size(10)
        mv = UIStyles.scale_size(7)
        layout.setContentsMargins(mh, mv, mh, mv)
        layout.setSpacing(UIStyles.scale_size(3))
        self.setLayout(layout)

        # ── 头部：复选框 + SN + 复制/分配/删除按钮 ──────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(UIStyles.scale_size(3))

        # 复选框
        from PyQt5.QtWidgets import QCheckBox
        self.checkbox = QCheckBox()
        self.checkbox.setFixedSize(UIStyles.scale_size(16), UIStyles.scale_size(16))
        self.checkbox.setStyleSheet("""
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
        """)
        self.checkbox.stateChanged.connect(self._on_checkbox_changed)
        header.addWidget(self.checkbox)

        sn_label = QLabel(self.device.sn)
        sn_label.setFont(UIStyles.get_font(8, bold=True))
        sn_label.setWordWrap(True)
        sn_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sn_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        sn_label.setToolTip(self.device.sn)
        sn_label.setStyleSheet(f'color: {UIStyles.TEXT_PRIMARY};')
        header.addWidget(sn_label, 1)

        btn_size = UIStyles.scale_size(24)

        # 分配窗口按钮
        assign_btn = QPushButton('🪟')
        assign_btn.setFixedSize(btn_size, btn_size)
        assign_btn.setCursor(Qt.PointingHandCursor)
        assign_btn.setStyleSheet(
            'QPushButton{border:none;background:transparent;font-size:12px;}'
            'QPushButton:hover{background:#C8E6C9;border-radius:4px;}'
        )
        assign_btn.clicked.connect(lambda: self.assign_window_requested.emit(self.device))
        assign_btn.setToolTip('分配到窗口')
        header.addWidget(assign_btn)

        copy_btn = QPushButton('📋')
        copy_btn.setFixedSize(btn_size, btn_size)
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setStyleSheet(
            'QPushButton{border:none;background:transparent;font-size:12px;}'
            'QPushButton:hover{background:#E8EAF6;border-radius:4px;}'
        )
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.device.sn))
        copy_btn.setToolTip('复制SN')
        header.addWidget(copy_btn)

        del_btn = QPushButton('✕')
        del_btn.setFixedSize(btn_size, btn_size)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setFont(UIStyles.get_font(7, bold=True))
        del_btn.setStyleSheet(
            'QPushButton{border:none;background:transparent;color:#9E9E9E;}'
            'QPushButton:hover{background:#FFCDD2;color:#F44336;border-radius:3px;}'
        )
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self.device))
        del_btn.setToolTip('移除设备')
        header.addWidget(del_btn)

        layout.addLayout(header)

        # ── 分割线 ──────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setFixedHeight(1)
        div.setStyleSheet('background:#EBEBEB;border:none;')
        layout.addWidget(div)

        # ── 信息行 ──────────────────────────────────────
        info_font = UIStyles.get_font(7)
        sc = UIStyles.TEXT_SECONDARY  # secondary color

        # 设备类型（新增）
        type_lbl = QLabel(f'📱  {self.device.get_type_display()}')
        type_lbl.setFont(info_font)
        type_lbl.setStyleSheet(f'color:{UIStyles.PRIMARY}; font-weight: bold;')
        layout.addWidget(type_lbl)

        ip_lbl = QLabel(f'🌐  {self.device.ip}')
        ip_lbl.setFont(info_font)
        ip_lbl.setStyleSheet(f'color:{sc};')
        layout.addWidget(ip_lbl)

        # app版本标签（保存引用，用于动态更新）
        fw = _strip_v(self.device.fw_ver or '')
        self.app_label = QLabel(f'📦  app: {fw}' if fw else '📦  app: —')
        self.app_label.setFont(info_font)
        self.app_label.setStyleSheet(f'color:{sc};')
        layout.addWidget(self.app_label)

        # 保存布局引用，用于动态更新版本信息
        self.info_layout = layout
        self._update_version_labels(info_font, sc)

        self.update_style()

    def _update_version_labels(self, info_font=None, sc=None):
        """更新或创建版本信息标签"""
        if info_font is None:
            info_font = UIStyles.get_font(7)
        if sc is None:
            sc = UIStyles.TEXT_SECONDARY

        # 移除旧的版本标签
        if self.kernel_label:
            self.info_layout.removeWidget(self.kernel_label)
            self.kernel_label.deleteLater()
            self.kernel_label = None
        if self.rootfs_label:
            self.info_layout.removeWidget(self.rootfs_label)
            self.rootfs_label.deleteLater()
            self.rootfs_label = None
        if self.pending_label:
            self.info_layout.removeWidget(self.pending_label)
            self.pending_label.deleteLater()
            self.pending_label = None

        # kernel / rootfs（MQTT 版本查询结果）
        kernel = self.versions.get('kernel', '')
        rootfs_a = self.versions.get('rootfs_a', '')
        rootfs_b = self.versions.get('rootfs_b', '')
        # A/B 不一致时两个都显示，一致时只显示一个
        if rootfs_a and rootfs_b and rootfs_a != rootfs_b:
            rootfs_str = f'{rootfs_a} / {rootfs_b} ⚠'
        else:
            rootfs_str = rootfs_a or rootfs_b

        if kernel:
            self.kernel_label = QLabel(f'🔧  kernel: {kernel}')
            self.kernel_label.setFont(info_font)
            self.kernel_label.setStyleSheet(f'color:{sc};')
            self.info_layout.addWidget(self.kernel_label)

        if rootfs_str:
            self.rootfs_label = QLabel(f'💽  rootfs: {rootfs_str}')
            self.rootfs_label.setFont(info_font)
            self.rootfs_label.setStyleSheet(f'color:{sc};')
            self.info_layout.addWidget(self.rootfs_label)

        if not self.versions:
            self.pending_label = QLabel('⏳ 版本查询中...')
            self.pending_label.setFont(UIStyles.get_font(6))
            self.pending_label.setStyleSheet('color:#BDBDBD;')
            self.info_layout.addWidget(self.pending_label)

    def update_versions(self, versions):
        """更新版本信息（动态刷新，不重建整个卡片）"""
        self.versions = versions or {}
        self._update_version_labels()

    def update_app_version(self):
        """更新app版本显示"""
        if self.app_label:
            fw = _strip_v(self.device.fw_ver or '')
            self.app_label.setText(f'📦  app: {fw}' if fw else '📦  app: —')

    def _on_checkbox_changed(self, state):
        """复选框状态变化"""
        from PyQt5.QtCore import Qt
        self.checked = (state == Qt.Checked)

    def set_checked(self, checked):
        """设置复选框状态"""
        self.checked = checked
        if self.checkbox:
            from PyQt5.QtCore import Qt
            self.checkbox.setChecked(checked)

    def update_style(self):
        r = UIStyles.scale_size(8)
        if self.selected:
            # 选中样式（门控风格）
            self.setStyleSheet(f"""
                DeviceCard {{
                    background-color: {UIStyles.PRIMARY_LIGHT};
                    border: 2px solid {UIStyles.PRIMARY};
                    border-radius: {r}px;
                }}
            """)
        else:
            # 默认样式（门控风格）
            self.setStyleSheet(f"""
                DeviceCard {{
                    background-color: {UIStyles.CARD_BG};
                    border: 1px solid {UIStyles.BORDER};
                    border-radius: {r}px;
                }}
                DeviceCard:hover {{
                    background-color: #f5f5f5;
                    border: 1px solid {UIStyles.PRIMARY};
                }}
            """)

    def set_selected(self, selected):
        self.selected = selected
        self.update_style()

    def mousePressEvent(self, event):
        self.clicked.emit(self.device)
        super().mousePressEvent(event)


class TestStatusIndicator(QWidget):
    """测试状态指示器 - 显示测试项名称和状态"""
    clicked = pyqtSignal(str)
    
    def __init__(self, test_name, parent=None):
        super().__init__(parent)
        self.test_name = test_name
        self.status = 'untested'
        self.setup_ui()
    
    def setup_ui(self):
        layout = QHBoxLayout()
        # 紧凑的边距设置
        margin_v = UIStyles.scale_size(2)
        margin_h = UIStyles.scale_size(4)
        spacing = UIStyles.scale_size(3)
        layout.setContentsMargins(margin_h, margin_v, margin_h, margin_v)
        layout.setSpacing(spacing)
        self.setLayout(layout)

        # 适中的指示器高度
        min_height = UIStyles.scale_size(22)
        self.setMinimumHeight(min_height)

        # 移除状态点，只保留文本标签
        self.name_label = QLabel(self.test_name)
        self.name_label.setFont(UIStyles.get_font(7))  # 使用7px字体
        self.name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.name_label, 1)

        self.setCursor(Qt.PointingHandCursor)
        self.set_status('untested')
    
    def set_status(self, status):
        self.status = status
        border_radius = UIStyles.scale_size(4)

        if status == 'untested':
            self.setStyleSheet(f"""
                TestStatusIndicator {{
                    background-color: {UIStyles.SURFACE};
                    border-radius: {border_radius}px;
                    border: 1px solid {UIStyles.BORDER};
                }}
                TestStatusIndicator:hover {{
                    background-color: #F5F5F5;
                    border: 1px solid {UIStyles.TEXT_DISABLED};
                }}
                QLabel {{
                    color: {UIStyles.TEXT_SECONDARY};
                    background-color: transparent;
                }}
            """)
        elif status == 'testing':
            self.setStyleSheet(f"""
                TestStatusIndicator {{
                    background-color: {UIStyles.PRIMARY_LIGHT};
                    border-radius: {border_radius}px;
                    border: 1px solid {UIStyles.PRIMARY};
                }}
                QLabel {{
                    color: {UIStyles.PRIMARY_DARK};
                    font-weight: bold;
                    background-color: transparent;
                }}
            """)
        elif status == 'passed':
            self.setStyleSheet(f"""
                TestStatusIndicator {{
                    background-color: #E8F5E9;
                    border-radius: {border_radius}px;
                    border: 1px solid {UIStyles.SUCCESS};
                }}
                TestStatusIndicator:hover {{
                    background-color: #C8E6C9;
                }}
                QLabel {{
                    color: {UIStyles.SUCCESS};
                    font-weight: bold;
                    background-color: transparent;
                }}
            """)
        elif status == 'failed':
            self.setStyleSheet(f"""
                TestStatusIndicator {{
                    background-color: #FFEBEE;
                    border-radius: {border_radius}px;
                    border: 1px solid {UIStyles.ERROR};
                }}
                TestStatusIndicator:hover {{
                    background-color: #FFCDD2;
                }}
                QLabel {{
                    color: {UIStyles.ERROR};
                    font-weight: bold;
                    background-color: transparent;
                }}
            """)
    
    def mousePressEvent(self, event):
        self.clicked.emit(self.test_name)
        super().mousePressEvent(event)


class TestWindowPanel(QFrame):
    """单个测试窗口面板"""

    # 信号：线程安全地更新打印按钮状态
    update_print_button_signal = pyqtSignal(bool)

    def __init__(self, panel_id, main_window, parent=None):
        super().__init__(parent)
        self.panel_id = panel_id
        self.main_window = main_window
        self.device = None
        self.speaker_type = 'indoor'
        self.video_widget = None
        self.test_buttons = {}
        self.status_indicators = {}
        self.device_label = None
        self.http_client = None
        self.stop_flag = threading.Event()  # 测试停止标志
        self.log_capture = None  # 当前测试的日志捕获器
        self.layout_mode = 4  # 默认4宫格布局

        # 连接信号到槽，确保线程安全
        self.update_print_button_signal.connect(self._set_print_button_enabled)

        self.setup_ui()

    def log_test(self, message: str):
        """记录测试日志：同时输出到全局logger和当前测试日志捕获器"""
        logger.info(message)
        if self.log_capture:
            self.log_capture.append(message)
    
    def setup_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        border_radius = UIStyles.scale_size(8)  # 统一使用8px圆角
        self.setStyleSheet(f"""
            TestWindowPanel {{
                background-color: {UIStyles.CARD_BG};
                border: 1px solid {UIStyles.BORDER};
                border-radius: {border_radius}px;
            }}
        """)
        
        # 移除固定最小高度，改为自适应
        min_width = UIStyles.scale_size(400)  # 降低最小宽度，适配小屏幕
        self.setMinimumWidth(min_width)
        # 不设置最小高度，让内容自适应
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        main_layout = QVBoxLayout()
        # 减小边距和间距，让布局更紧凑
        main_layout.setContentsMargins(8, 4, 8, 4)  # 进一步减小上下边距
        main_layout.setSpacing(3)  # 减小间距到3px
        self.setLayout(main_layout)

        # 设备信息行：标签+复制按钮+关闭按钮
        device_info_layout = QHBoxLayout()
        device_info_layout.setContentsMargins(0, 0, 0, 0)
        device_info_layout.setSpacing(5)

        self.device_label = QLabel(f'窗口 {self.panel_id + 1}: 未分配设备')
        self.device_label.setFont(UIStyles.get_font(7, bold=True))
        self.device_label.setAlignment(Qt.AlignCenter)
        padding_v = UIStyles.scale_size(3)
        padding_h = UIStyles.scale_size(4)
        border_radius = UIStyles.scale_size(3)
        min_height = UIStyles.scale_size(20)
        self.device_label.setStyleSheet(f"""
            QLabel {{
                background-color: {UIStyles.SURFACE};
                color: {UIStyles.TEXT_SECONDARY};
                padding: {padding_v}px {padding_h}px;
                border-radius: {border_radius}px;
                border: 1px solid {UIStyles.BORDER};
                min-height: {min_height}px;
            }}
        """)
        device_info_layout.addWidget(self.device_label)

        btn_size = UIStyles.scale_size(20)
        self.copy_sn_btn = QPushButton('📋')
        self.copy_sn_btn.setFixedSize(btn_size, btn_size)
        self.copy_sn_btn.setStyleSheet("QPushButton { border: none; background: transparent; } QPushButton:hover { background: #E0E0E0; }")
        self.copy_sn_btn.setToolTip('复制SN')
        self.copy_sn_btn.clicked.connect(self.copy_device_sn)
        self.copy_sn_btn.hide()
        device_info_layout.addWidget(self.copy_sn_btn)

        # 关闭窗口按钮（解绑设备）
        self.close_window_btn = QPushButton('✕')
        self.close_window_btn.setFixedSize(btn_size, btn_size)
        self.close_window_btn.setStyleSheet("""
            QPushButton {
                border: none;
                background: transparent;
                color: #9E9E9E;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #FFCDD2;
                color: #F44336;
                border-radius: 3px;
            }
        """)
        self.close_window_btn.setToolTip('关闭窗口（解绑设备）')
        self.close_window_btn.clicked.connect(self.close_window)
        self.close_window_btn.hide()
        device_info_layout.addWidget(self.close_window_btn)

        # 安装事件过滤器以捕获双击事件
        self.device_label.installEventFilter(self)

        main_layout.addLayout(device_info_layout)

        # 主内容区域：左侧视频，右侧测试结果
        content_widget = QWidget()
        self.content_layout = QHBoxLayout()  # 保存引用
        self.content_layout.setSpacing(6)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        content_widget.setLayout(self.content_layout)

        # 左侧：视频区域 - 增大尺寸
        self.video_widget = VideoWidget()
        video_width = UIStyles.scale_size(400)
        video_height = UIStyles.scale_size(225)
        self.video_widget.setMinimumSize(video_width, video_height)
        self.video_widget.setMaximumSize(video_width, video_height)
        self.video_widget.retry_button.clicked.connect(self.retry_video_stream)
        self.content_layout.addWidget(self.video_widget, 0)

        # 右侧：测试结果垂直布局
        self.right_panel = QWidget()  # 保存引用
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(1)
        self.right_panel.setLayout(right_layout)

        # 测试项标题
        self.title_label = QLabel('测试结果')  # 保存引用
        self.title_label.setFont(UIStyles.get_font(7, bold=True))
        self.title_label.setStyleSheet(f'color: {UIStyles.TEXT_PRIMARY};')
        right_layout.addWidget(self.title_label)

        # 测试状态指示器容器
        self.status_container = QWidget()
        self.status_layout = QVBoxLayout()
        self.status_layout.setContentsMargins(0, 0, 0, 0)
        self.status_layout.setSpacing(UIStyles.scale_size(2))
        self.status_container.setLayout(self.status_layout)
        right_layout.addWidget(self.status_container)
        right_layout.addStretch()

        self.content_layout.addWidget(self.right_panel, 1)
        
        # 将内容区域添加到主��局
        main_layout.addWidget(content_widget, 1)
        
        # 功能按钮区域 - 占据整个窗口底部
        button_container = QWidget()
        button_container.setMaximumHeight(UIStyles.scale_size(32))
        self.button_layout = QGridLayout()
        self.button_layout.setSpacing(UIStyles.scale_size(3))
        self.button_layout.setContentsMargins(0, 0, 0, 0)
        button_container.setLayout(self.button_layout)
        main_layout.addWidget(button_container, 0)
        
        self.setup_buttons_and_indicators_for_type(self.speaker_type)

    def adjust_for_layout_mode(self, layout_mode):
        """根据布局模式调整面板内部组件大小
        Args:
            layout_mode: 1, 4, 或 9
        """
        self.layout_mode = layout_mode

        # 根据布局模式设置面板本身的最小尺寸（关键修复）
        if layout_mode == 1:
            # 1宫格：大面板
            self.setMinimumSize(UIStyles.scale_size(600), UIStyles.scale_size(400))
        elif layout_mode == 9:
            # 9宫格：紧凑面板，高度自适应
            self.setMinimumSize(UIStyles.scale_size(350), 0)  # 不限制高度
            self.setMaximumSize(16777215, 16777215)  # 不限制最大尺寸
        else:
            # 4宫格：默认面板
            self.setMinimumSize(UIStyles.scale_size(400), 0)

        # 根据布局模式设置视频区域大小策略和布局拉伸比例
        if layout_mode == 1:
            # 1宫格：视频自适应，占据大部分空间
            if self.video_widget:
                # 移除 VideoWidget 的大小限制
                self.video_widget.setMinimumSize(0, 0)
                self.video_widget.setMaximumSize(16777215, 16777215)
                self.video_widget.setMinimumSize(UIStyles.scale_size(640), UIStyles.scale_size(360))

                # 移除 video_label 的大小限制，让其自适应
                if hasattr(self.video_widget, 'video_label'):
                    self.video_widget.video_label.setMinimumSize(0, 0)
                    self.video_widget.video_label.setMaximumSize(16777215, 16777215)

            # 调整布局拉伸比例：视频占5/6，右侧占1/6
            self.content_layout.setStretch(0, 5)  # 视频
            self.content_layout.setStretch(1, 1)  # 右侧面板

            # 右侧面板设置最大宽度，居中对齐
            if hasattr(self, 'right_panel'):
                self.right_panel.setMinimumWidth(UIStyles.scale_size(120))  # 最小宽度
                self.right_panel.setMaximumWidth(UIStyles.scale_size(180))  # 最大宽度
                # 设置右侧面板在水平布局中居中
                self.content_layout.setAlignment(self.right_panel, Qt.AlignCenter | Qt.AlignTop)

            font_size = 9
            btn_font_size = 8
            btn_height = UIStyles.scale_size(32)

        elif layout_mode == 9:
            # 9宫格：紧凑布局，测试结果列占1/6
            if self.video_widget:
                # 视频设置较小尺寸
                self.video_widget.setMinimumSize(UIStyles.scale_size(200), UIStyles.scale_size(112))
                self.video_widget.setMaximumSize(UIStyles.scale_size(280), UIStyles.scale_size(158))

                # video_label 也设置相同范围
                if hasattr(self.video_widget, 'video_label'):
                    self.video_widget.video_label.setMinimumSize(UIStyles.scale_size(200), UIStyles.scale_size(112))
                    self.video_widget.video_label.setMaximumSize(UIStyles.scale_size(280), UIStyles.scale_size(158))

            # 调整布局拉伸比例：视频占5/6，右侧测试结果占1/6
            self.content_layout.setStretch(0, 5)  # 视频 5/6
            self.content_layout.setStretch(1, 1)  # 右侧面板 1/6

            # 右侧面板不限制宽度，按比例自适应
            if hasattr(self, 'right_panel'):
                self.right_panel.setMinimumWidth(0)
                self.right_panel.setMaximumWidth(16777215)
                self.content_layout.setAlignment(self.right_panel, Qt.AlignLeft | Qt.AlignTop)

            font_size = 5
            btn_font_size = 5
            btn_height = UIStyles.scale_size(22)

        else:
            # 4宫格：默认视频，固定大小
            video_width = UIStyles.scale_size(400)
            video_height = UIStyles.scale_size(225)
            if self.video_widget:
                self.video_widget.setMinimumSize(video_width, video_height)
                self.video_widget.setMaximumSize(video_width, video_height)

                # video_label 也设置为默认尺寸
                if hasattr(self.video_widget, 'video_label'):
                    self.video_widget.video_label.setMinimumSize(video_width, video_height)
                    self.video_widget.video_label.setMaximumSize(video_width, video_height)

            # 调整布局拉伸比例：视频不拉伸，右侧自适应
            self.content_layout.setStretch(0, 0)  # 视频
            self.content_layout.setStretch(1, 1)  # 右侧面板

            # 恢复右侧面板默认设置
            if hasattr(self, 'right_panel'):
                self.right_panel.setMaximumWidth(16777215)
                self.content_layout.setAlignment(self.right_panel, Qt.AlignLeft | Qt.AlignTop)

            font_size = 7
            btn_font_size = 6
            btn_height = UIStyles.scale_size(28)

        # 调整设备标签字体
        if self.device_label:
            self.device_label.setFont(UIStyles.get_font(font_size, bold=True))

        # 调整标题字体
        if hasattr(self, 'title_label') and self.title_label:
            self.title_label.setFont(UIStyles.get_font(font_size, bold=True))

        # 调整测试状态指示器字体
        for indicator in self.status_indicators.values():
            if indicator.name_label:
                indicator.name_label.setFont(UIStyles.get_font(font_size))

        # 调整按钮字体和样式
        for btn in self.test_buttons.values():
            btn.setFont(UIStyles.get_font(btn_font_size))
            # 重新设置按钮样式
            border_radius = UIStyles.scale_size(3)
            padding_v = UIStyles.scale_size(4)
            padding_h = UIStyles.scale_size(6)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {UIStyles.PRIMARY};
                    color: white;
                    border: none;
                    border-radius: {border_radius}px;
                    padding: {padding_v}px {padding_h}px;
                    font-weight: 600;
                    height: {btn_height}px;
                }}
                QPushButton:hover {{
                    background-color: {UIStyles.PRIMARY_DARK};
                }}
                QPushButton:pressed {{
                    background-color: {UIStyles.PRIMARY};
                }}
                QPushButton:disabled {{
                    background-color: #BDBDBD;
                    color: {UIStyles.TEXT_SECONDARY};
                }}
            """)

    def setup_buttons_and_indicators_for_type(self, speaker_type):
        """根据音箱类型设置按钮和测试状态指示器"""
        # 清空现有按钮
        for i in reversed(range(self.button_layout.count())):
            widget = self.button_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        
        # 清空现有指示器
        for i in reversed(range(self.status_layout.count())):
            widget = self.status_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        self.test_buttons.clear()
        self.status_indicators.clear()
        
        # 根据类型创建按钮和测试项
        if speaker_type == 'indoor':
            buttons = ['一键检测', '烧录', '音麦', 'wifi', '蓝牙', '星闪', '红外', '正式', '打印']
            test_items = ['烧录', '音麦', 'WiFi', '蓝牙', '星闪', '红外', '正式']
        else:  # outdoor
            buttons = ['一键检测', '烧录', '音麦', 'wifi', '蓝牙', '星闪', '微波', '正式', '打印']
            test_items = ['烧录', '音麦', 'WiFi', '蓝牙', '星闪', '微波', '正式']
        
        # 创建按钮（单行布局）
        for idx, btn_text in enumerate(buttons):
            btn = QPushButton(btn_text)
            # 使用更小的字体，让按钮自适应宽度
            btn.setFont(UIStyles.get_font(6))  # 使用6px字体，更小
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)  # 水平扩展

            border_radius = UIStyles.scale_size(3)
            padding_v = UIStyles.scale_size(4)
            padding_h = UIStyles.scale_size(6)
            btn_height = UIStyles.scale_size(28)

            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {UIStyles.PRIMARY};
                    color: white;
                    border: none;
                    border-radius: {border_radius}px;
                    padding: {padding_v}px {padding_h}px;
                    font-weight: 600;
                    height: {btn_height}px;
                }}
                QPushButton:hover {{
                    background-color: {UIStyles.PRIMARY_DARK};
                }}
                QPushButton:pressed {{
                    background-color: {UIStyles.PRIMARY};
                }}
                QPushButton:disabled {{
                    background-color: #BDBDBD;
                    color: {UIStyles.TEXT_SECONDARY};
                }}
            """)
            btn.setEnabled(False)
            btn.clicked.connect(lambda checked, text=btn_text: self.on_button_clicked(text))
            # 单行布局：所有按钮放在第0行
            self.button_layout.addWidget(btn, 0, idx)
            self.test_buttons[btn_text] = btn
        
        # 创建状态指示器
        for test_name in test_items:
            indicator = TestStatusIndicator(test_name)
            self.status_layout.addWidget(indicator)
            self.status_indicators[test_name] = indicator
    
    def update_test_status(self, test_name, status):
        """更新测试状态"""
        if test_name in self.status_indicators:
            self.status_indicators[test_name].set_status(status)

        # 记录测试项状态到日志捕获器
        if self.log_capture and status in ['testing', 'passed', 'failed']:
            status_text = {'testing': '开始', 'passed': '通过 ✅', 'failed': '失败 ❌'}.get(status, status)
            self.log_capture.append(f"【{test_name}】{status_text}")

        # 保存测试记录到数据库
        if status in ['passed', 'failed'] and self.device:
            result = 'PASS' if status == 'passed' else 'FAIL'
            try:
                self.main_window.test_db.save_record(
                    sn=self.device.sn,
                    sub_type=test_name,
                    results=result
                )
            except Exception as e:
                logger.error(f"保存测试记录失败: {e}")

        # 更新打印按钮状态
        self._update_print_button_state()

    def _update_print_button_state(self):
        """根据测试状态更新打印按钮：只有全部测试通过才能打印。
        线程安全：通过信号机制确保在主线程中执行 UI 更新。"""
        if '打印' not in self.test_buttons:
            return

        # 检查所有测试项状态
        all_passed = all(
            indicator.status == "passed"
            for indicator in self.status_indicators.values()
        )

        # 发射信号，由主线程的槽函数处理
        self.update_print_button_signal.emit(all_passed)

    def _set_print_button_enabled(self, enabled):
        """槽函数：在主线程中更新打印按钮状态"""
        if '打印' in self.test_buttons:
            self.test_buttons['打印'].setEnabled(enabled)

    def copy_device_sn(self):
        """复制设备SN到剪贴板"""
        if self.device:
            QApplication.clipboard().setText(self.device.sn)
            logger.info(f"已复制SN: {self.device.sn}")

    def close_window(self):
        """关闭窗口（解绑设备但不删除窗口）"""
        if self.device:
            logger.info(f"窗口 {self.panel_id + 1} 关闭，解绑设备: {self.device.sn}")
            self.bind_device(None)

    def eventFilter(self, obj, event):
        """事件过滤器，捕获设备标签的双击事件"""
        if obj == self.device_label and event.type() == event.MouseButtonDblClick:
            if self.device:
                self.on_device_label_double_click()
            return True
        return super().eventFilter(obj, event)

    def on_device_label_double_click(self):
        """双击设备标签时的处理"""
        reply = QMessageBox.question(
            self, '确认操作',
            f'确定要将设备 {self.device.sn} 设置为出厂模式吗？',
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            try:
                result = self.http_client.set_factory()
                if result and result.get('code') == 0:
                    QMessageBox.information(self, '成功', '设备已设置为出厂模式')
                    logger.info(f"设备 {self.device.sn} 已设置为出厂模式")
                else:
                    QMessageBox.warning(self, '失败', '设置出厂模式失败')
                    logger.error(f"设备 {self.device.sn} 设置出厂模式失败")
            except Exception as e:
                QMessageBox.critical(self, '错误', f'操作失败: {e}')
                logger.error(f"设置出厂模式异常: {e}")

    def bind_device(self, device):
        """绑定设备到此窗口"""
        # 如果解绑设备，先通知所有测试线程停止
        if device is None and self.device is not None:
            logger.info(f"窗口{self.panel_id + 1}设备断开，停止所有测试")
            self.stop_flag.set()  # 设置停止标志

            # 停止视频流（修复设备断电后视频画面卡住的问题）
            if self.video_widget:
                self.video_widget.stop_stream()
                logger.info(f"窗口{self.panel_id + 1}已停止视频流")
        elif device is not None:
            self.stop_flag.clear()  # 清除停止标志

        self.device = device
        if device:
            # 优先使用 mDNS 上报的设备类型，原有逻辑作为兜底
            if device.is_indoor_speaker():
                detected_type = 'indoor'
                logger.info(f"窗口{self.panel_id + 1}通过 mDNS 检测到室内音箱 (type={device.type})")
            elif device.is_outdoor_speaker():
                detected_type = 'outdoor'
                logger.info(f"窗口{self.panel_id + 1}通过 mDNS 检测到室外音箱 (type={device.type})")
            else:
                detected_type = self.speaker_type  # 兜底方案
                logger.info(f"窗口{self.panel_id + 1}无法从 mDNS 确定设备类型 (type={device.type})，使用当前设置: {detected_type}")

            # 如果检测到的类型与当前类型不同，更新
            if detected_type != self.speaker_type:
                logger.info(f"窗口{self.panel_id + 1}设备类型从 {self.speaker_type} 更新为 {detected_type}")
                self.set_speaker_type(detected_type)

            self.device_label.setText(f'🔌 窗口 {self.panel_id + 1}: {device.get_display_name()} ({device.ip}:{device.port})')
            padding_v = UIStyles.scale_size(3)
            padding_h = UIStyles.scale_size(4)
            border_radius = UIStyles.scale_size(3)
            min_height = UIStyles.scale_size(20)
            self.device_label.setStyleSheet(f"""
                QLabel {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 {UIStyles.SUCCESS}, stop:1 {UIStyles.INFO});
                    color: white;
                    padding: {padding_v}px {padding_h}px;
                    border-radius: {border_radius}px;
                    min-height: {min_height}px;
                    font-weight: bold;
                }}
            """)
            self.copy_sn_btn.show()
            self.close_window_btn.show()
            self.http_client = SpeakerHTTPClient(device.ip, port=8080)
            logger.info(f"窗口{self.panel_id + 1}绑定设备: {device.get_display_name()} ({device.ip}:8080 HTTP测试端口)")
            
            for btn in self.test_buttons.values():
                btn.setEnabled(True)
            # 设备连接后立即更新打印按钮状态（根据测试项结果）
            self._update_print_button_state()

            if self.speaker_type == 'outdoor':
                # 延迟启动视频流，给设备RTSP服务一些启动时间
                logger.info(f"窗口{self.panel_id + 1}将在1秒后启动视频流")
                device_ip = device.ip  # 立即捕获IP值，避免闭包问题
                QTimer.singleShot(1000, lambda ip=device_ip: self.video_widget.start_rtsp_stream(ip))
        else:
            self.device_label.setText(f'窗口 {self.panel_id + 1}: 未分配设备')
            padding_v = UIStyles.scale_size(3)
            padding_h = UIStyles.scale_size(4)
            border_radius = UIStyles.scale_size(3)
            min_height = UIStyles.scale_size(20)
            self.device_label.setStyleSheet(f"""
                QLabel {{
                    background-color: #e9ecef;
                    padding: {padding_v}px {padding_h}px;
                    border-radius: {border_radius}px;
                    min-height: {min_height}px;
                }}
            """)
            self.copy_sn_btn.hide()
            self.close_window_btn.hide()
            for btn in self.test_buttons.values():
                btn.setEnabled(False)
            self.video_widget.show_default_image()
            
            # 清空所有测试状态
            for indicator in self.status_indicators.values():
                indicator.set_status('untested')
    
    def retry_video_stream(self):
        """重试视频流连接"""
        if self.device and self.speaker_type == 'outdoor':
            logger.info(f"窗口{self.panel_id + 1}重试视频流连接: {self.device.ip}")
            self.video_widget.start_rtsp_stream(self.device.ip)
    
    def set_speaker_type(self, speaker_type):
        """设置音箱类型"""
        # 如果类型相同，无需重新设置
        if self.speaker_type == speaker_type:
            logger.debug(f"窗口{self.panel_id + 1}设备类型已经是 {speaker_type}，跳过重复设置")
            return

        self.speaker_type = speaker_type
        self.setup_buttons_and_indicators_for_type(speaker_type)

        # 先停止视频流，避免冲突
        if speaker_type == 'indoor':
            self.video_widget.show_default_image()

        # 重新绑定设备会根据speaker_type自动处理视频流
        if self.device:
            current_device = self.device
            self.bind_device(None)
            self.bind_device(current_device)
    
    def _run_single_test(self, test_type, impl_func):
        """单个测试项包装：捕获本次测试完整日志并保存为一条记录"""
        import logging as _logging
        panel_handler = None
        try:
            if self.device:
                self.log_capture = TestLogCapture(self.device.sn, test_type=test_type)
                self.log_capture.append(f"=== 开始测试[{test_type}]: {self.device.get_display_name()} ===")
                panel_handler = PanelLogHandler(self.log_capture, f"窗口{self.panel_id + 1}")
                _logging.getLogger('HornCheck').addHandler(panel_handler)

            impl_func()

            # 单项测试结果取该测试项指示器状态
            if self.log_capture:
                indicator = self.status_indicators.get(test_type)
                if indicator and indicator.status == 'passed':
                    result = "PASS"
                elif indicator and indicator.status == 'failed':
                    result = "FAIL"
                else:
                    result = "UNKNOWN"
                self.log_capture.set_result(result)
                self.log_capture.append(f"=== 测试[{test_type}]完成，结果: {result} ===")
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}{test_type}测试异常: {e}")
            if self.log_capture:
                self.log_capture.append(f"测试异常: {e}")
                self.log_capture.set_result("FAIL")
        finally:
            if panel_handler:
                _logging.getLogger('HornCheck').removeHandler(panel_handler)
            if self.log_capture:
                try:
                    log_path = self.log_capture.save_to_file()
                    logger.info(f"测试日志已保存: {log_path}")
                except Exception as e:
                    logger.error(f"保存测试日志失败: {e}")
                self.log_capture = None

    def on_button_clicked(self, button_text):
        """按钮点击事件处理"""
        if not self.device:
            return

        if button_text == '一键检测':
            self.start_auto_test()
        elif button_text == '烧录':
            self.test_burn()
        elif button_text == '音麦':
            self.test_audio_mic()
        elif button_text == 'wifi':
            self.test_wifi()
        elif button_text == '蓝牙':
            self.test_bluetooth()
        elif button_text == '星闪':
            self.test_starflash()
        elif button_text == '红外':
            self.test_infrared()
        elif button_text == '微波':
            self.test_microwave()
        elif button_text == '正式':
            self.test_production()
        elif button_text == '打印':
            self.print_label()
    
    def start_auto_test(self):
        """一键检测"""
        threading.Thread(target=self._run_auto_test, daemon=True).start()
    
    def _run_auto_test(self):
        """执行自动测试 - 并行执行"""
        import logging as _logging
        panel_handler = None
        try:
            # 创建本次测试的日志捕获器，注册 handler 捕获本窗口所有 logger 输出
            if self.device:
                self.log_capture = TestLogCapture(self.device.sn)
                self.log_capture.append(f"=== 开始自动测试: {self.device.get_display_name()} ===")
                panel_prefix = f"窗口{self.panel_id + 1}"
                panel_handler = PanelLogHandler(self.log_capture, panel_prefix)
                _logging.getLogger('HornCheck').addHandler(panel_handler)

            logger.info(f"窗口{self.panel_id + 1}开始自动测试: {self.device.get_display_name()}")

            # 清空所有测试结果状态
            logger.info(f"窗口{self.panel_id + 1}清空所有测试结果")
            for test_name, indicator in self.status_indicators.items():
                indicator.set_status('untested')

            # 线程1：烧录、音麦、红外/微波、正式
            thread1 = threading.Thread(target=self._run_test_group1, daemon=True)
            # 线程2：WiFi、蓝牙、星闪（串行）
            thread2 = threading.Thread(target=self._run_test_group2, daemon=True)

            thread1.start()
            thread2.start()

            thread1.join()
            thread2.join()

            logger.info(f"窗口{self.panel_id + 1}自动测试完成")

            # 判断整体结果并保存日志
            if self.log_capture:
                all_passed = all(
                    ind.status == 'passed'
                    for ind in self.status_indicators.values()
                )
                overall_result = "PASS" if all_passed else "FAIL"
                self.log_capture.set_result(overall_result)
                self.log_capture.append(f"=== 测试完成，整体结果: {overall_result} ===")

        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}自动测试异常: {e}")
            if self.log_capture:
                self.log_capture.append(f"测试异常: {e}")
                self.log_capture.set_result("FAIL")

        finally:
            # 移除 handler，无论正常结束还是异常都执行
            if panel_handler:
                _logging.getLogger('HornCheck').removeHandler(panel_handler)

            # 保存日志文件
            if self.log_capture:
                try:
                    log_path = self.log_capture.save_to_file()
                    logger.info(f"测试日志已保存: {log_path}")
                except Exception as e:
                    logger.error(f"保存测试日志失败: {e}")
                self.log_capture = None
    
    def _run_test_group1(self):
        """测试组1：烧录、音麦、红外/微波、正式"""
        try:
            self._test_burn_impl()
            self._test_audio_mic_impl()
            
            if self.speaker_type == 'indoor':
                self._test_infrared_impl()
            else:
                self._test_microwave_impl()
            
            self._test_production_impl()
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}测试组1异常: {e}")
    
    def _run_test_group2(self):
        """测试组2：WiFi、蓝牙、星闪（串行）"""
        try:
            self._test_wifi_impl()
            self._test_bluetooth_impl()
            self._test_starflash_impl()
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}测试组2异常: {e}")
    
    def test_burn(self):
        """烧录测试 - MAC地址烧录"""
        threading.Thread(target=lambda: self._run_single_test('烧录', self._test_burn_impl), daemon=True).start()
    
    def _test_burn_impl(self):
        try:
            # 检查设备是否断开
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止烧录测试")
                return
            
            self.update_test_status('烧录', 'testing')
            
            logger.info(f"窗口{self.panel_id + 1}开始MAC地址烧录流程")
            
            # 步骤1: 分配MAC地址
            mac_data = self.main_window.mac_allocator.allocate_mac(self.device.sn, "WS73")
            if not mac_data:
                logger.error(f"窗口{self.panel_id + 1}MAC地址分配失败")
                print(f"窗口{self.panel_id + 1}MAC地址分配失败")
                self.update_test_status('烧录', 'failed')
                return
            
            # 再次检查设备状态
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止烧录测试")
                return
            
            allocated_wifi_mac = mac_data.get('wifi_mac')
            allocated_starflash_mac = mac_data.get('starflash_mac')
            
            print(f"分配的MAC - WiFi: {allocated_wifi_mac}, 星闪: {allocated_starflash_mac}")
            logger.info(f"窗口{self.panel_id + 1}分配的MAC - WiFi: {allocated_wifi_mac}, 星闪: {allocated_starflash_mac}")
            
            # 步骤2: 查询设备当前MAC地址
            logger.info(f"窗口{self.panel_id + 1}查询设备当前MAC地址")
            query_result = self.http_client.get_mac_addresses()
            print(f"设备当前MAC查询结果: {query_result}")
            
            if not query_result or query_result.get('code') != 0:
                logger.error(f"窗口{self.panel_id + 1}MAC地址查询失败")
                print(f"窗口{self.panel_id + 1}MAC地址查询失败")
                self.update_test_status('烧录', 'failed')
                return
            
            # 步骤3: 检查设备当前MAC是否已经烧录成功
            data = query_result.get('data', {})
            current_wifi_mac = data.get('wifi_mac', '')
            current_bluetooth_mac = data.get('bluetooth_mac', '')
            current_starflash_mac = data.get('starflash_mac', '')
            
            print(f"设备当前MAC - WiFi: {current_wifi_mac}, 蓝牙: {current_bluetooth_mac}, 星闪: {current_starflash_mac}")
            
            # 检查是否所有MAC都不以00开头（已烧录成功）
            mac_already_burned = (
                not (current_wifi_mac.startswith('00:00') or current_wifi_mac.startswith('00-00')) and
                not (current_bluetooth_mac.startswith('00:00') or current_bluetooth_mac.startswith('00-00')) and
                not (current_starflash_mac.startswith('00:00') or current_starflash_mac.startswith('00-00'))
            )
            
            if mac_already_burned:
                logger.info(f"窗口{self.panel_id + 1}设备MAC已烧录，跳过烧录步骤")
                print(f"窗口{self.panel_id + 1}设备MAC已烧录，无需重复烧录")
                print(f"  WiFi MAC: {current_wifi_mac}")
                print(f"  蓝牙 MAC: {current_bluetooth_mac}")
                print(f"  星闪 MAC: {current_starflash_mac}")
                self.update_test_status('烧录', 'passed')
                return
            
            # 步骤4: MAC未烧录或烧录不完整，需要调用烧录接口
            logger.info(f"窗口{self.panel_id + 1}设备MAC未烧录，开始烧录")
            print(f"窗口{self.panel_id + 1}准备烧录MAC - WiFi: {allocated_wifi_mac}, 星闪: {allocated_starflash_mac}")
            
            result = self.http_client.set_mac_addresses(allocated_wifi_mac, allocated_starflash_mac)
            print(f"MAC烧录接口返回: {result}")
            
            if not result or result.get('code') != 0:
                error_msg = result.get('message', '未知错误') if result else '请求失败'
                logger.error(f"窗口{self.panel_id + 1}MAC地址烧录接口调用失败: {error_msg}")
                print(f"窗口{self.panel_id + 1}MAC地址烧录接口调用失败: {error_msg}")
                self.update_test_status('烧录', 'failed')
                return
            
            # 等待设备写入MAC
            time.sleep(1)
            
            # 步骤5: 再次查询MAC地址验证烧录结果
            logger.info(f"窗口{self.panel_id + 1}再次查询MAC地址验证烧录结果")
            verify_result = self.http_client.get_mac_addresses()
            print(f"烧录后MAC查询结果: {verify_result}")
            
            if not verify_result or verify_result.get('code') != 0:
                logger.error(f"窗口{self.panel_id + 1}烧录后MAC地址查询失败")
                print(f"窗口{self.panel_id + 1}烧录后MAC地址查询失败")
                self.update_test_status('烧录', 'failed')
                return
            
            # 验证烧录后的MAC地址
            verify_data = verify_result.get('data', {})
            final_wifi_mac = verify_data.get('wifi_mac', '')
            final_bluetooth_mac = verify_data.get('bluetooth_mac', '')
            final_starflash_mac = verify_data.get('starflash_mac', '')
            
            print(f"烧录后的MAC - WiFi: {final_wifi_mac}, 蓝牙: {final_bluetooth_mac}, 星闪: {final_starflash_mac}")
            
            # 检查烧录后的MAC地址是否以00开头
            if final_wifi_mac.startswith('00:') or final_wifi_mac.startswith('00-'):
                logger.error(f"窗口{self.panel_id + 1}WiFi MAC地址烧录失败，仍为00开头: {final_wifi_mac}")
                print(f"窗口{self.panel_id + 1}WiFi MAC地址烧录失败: {final_wifi_mac}")
                self.update_test_status('烧录', 'failed')
                return
            
            if final_bluetooth_mac.startswith('00:') or final_bluetooth_mac.startswith('00-'):
                logger.error(f"窗口{self.panel_id + 1}蓝牙MAC地址烧录失败，仍为00开头: {final_bluetooth_mac}")
                print(f"窗口{self.panel_id + 1}蓝牙MAC地址烧录失败: {final_bluetooth_mac}")
                self.update_test_status('烧录', 'failed')
                return
            
            if final_starflash_mac.startswith('00:') or final_starflash_mac.startswith('00-'):
                logger.error(f"窗口{self.panel_id + 1}星闪MAC地址烧录失败，仍为00开头: {final_starflash_mac}")
                print(f"窗口{self.panel_id + 1}星闪MAC地址烧录失败: {final_starflash_mac}")
                self.update_test_status('烧录', 'failed')
                return
            
            # 所有MAC地址都不以00开头，烧录成功
            logger.info(f"窗口{self.panel_id + 1}MAC地址烧录验证成功")
            print(f"窗口{self.panel_id + 1}MAC地址烧录验证成功")
            print(f"  WiFi MAC: {final_wifi_mac}")
            print(f"  蓝牙 MAC: {final_bluetooth_mac}")
            print(f"  星闪 MAC: {final_starflash_mac}")
            self.update_test_status('烧录', 'passed')
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}MAC烧录异常: {e}")
            print(f"窗口{self.panel_id + 1}MAC烧录异常: {e}")
            self.update_test_status('烧录', 'failed')
    
    def test_audio_mic(self):
        """音麦测试"""
        threading.Thread(target=lambda: self._run_single_test('音麦', self._test_audio_mic_impl), daemon=True).start()
    
    def _test_audio_mic_impl(self):
        try:
            # 检查设备是否断开
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止音麦测试")
                return
            
            self.update_test_status('音麦', 'testing')
            logger.info(f"窗口{self.panel_id + 1}开始音麦测试")
            logger.info(f"窗口{self.panel_id + 1}检查设备连接: {self.device.ip}:8080")

            # 先播放音频测试喇叭
            logger.info(f"窗口{self.panel_id + 1}播放音频测试喇叭")
            play_result = self.http_client.play_audio()
            if not play_result or play_result.get('code') != 0:
                logger.warning(f"窗口{self.panel_id + 1}喇叭播放测试失败")

            # 录音回放测试麦克风
            result = self.http_client.mic_record_play(duration=3)
            if result and result.get('code') == 0:
                logger.info(f"窗口{self.panel_id + 1}音麦测试通过 ✅")
                self.update_test_status('音麦', 'passed')
            else:
                error_msg = result.get('message', '未知错误') if result else '请求失败'
                logger.error(f"窗口{self.panel_id + 1}音麦测试失败: {error_msg}")
                self.update_test_status('音麦', 'failed')
                logger.error(f"窗口{self.panel_id + 1}请检查设备 {self.device.ip}:8080 是否在线")

        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}音麦测试异常: {e}")
            self.update_test_status('音麦', 'failed')
            logger.error(f"窗口{self.panel_id + 1}建议检查网络连接和设备状态")
    
    def test_wifi(self):
        """WiFi测试"""
        threading.Thread(target=lambda: self._run_single_test('WiFi', self._test_wifi_impl), daemon=True).start()
    
    def _test_wifi_impl(self):
        try:
            # 检查设备是否断开
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止WiFi测试")
                return

            self.update_test_status('WiFi', 'testing')
            logger.info(f"窗口{self.panel_id + 1}开始WiFi测试")
            result = self.http_client.scan_wifi(duration=8)
            # print(f"WiFi测试结果: {result}")
            if result and result.get('code') == 0:
                data = result.get('data')
                if data is not None and data:
                    # 检查是否有信号强度大于-40dB的WiFi
                    strong_signal = False
                    for wifi in data:
                        signal_str = wifi.get('signal', '')
                        try:
                            signal_value = int(signal_str.split()[0])
                            if signal_value > -40:
                                strong_signal = True
                                break
                        except:
                            pass

                    if strong_signal:
                        logger.info(f"窗口{self.panel_id + 1}WiFi测试通过")
                        self.update_test_status('WiFi', 'passed')
                        print(f"窗口{self.panel_id + 1}WiFi测试通过，数据: {data}")
                    else:
                        logger.error(f"窗口{self.panel_id + 1}WiFi测试失败: 无信号强度>-40dB的WiFi")
                        self.update_test_status('WiFi', 'failed')
                        print(f"窗口{self.panel_id + 1}WiFi测试失败: 无信号强度>-40dB的WiFi")
                else:
                    logger.error(f"窗口{self.panel_id + 1}WiFi测试失败: data为空")
                    self.update_test_status('WiFi', 'failed')
                    print(f"窗口{self.panel_id + 1}WiFi测试失败: data为空")
            else:
                logger.error(f"窗口{self.panel_id + 1}WiFi测试失败")
                self.update_test_status('WiFi', 'failed')
                print(f"窗口{self.panel_id + 1}WiFi测试失败: {result}")
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}WiFi测试异常: {e}")
            self.update_test_status('WiFi', 'failed')
            print(f"窗口{self.panel_id + 1}WiFi测试异常: {e}")
    
    def test_bluetooth(self):
        """蓝牙测试"""
        threading.Thread(target=lambda: self._run_single_test('蓝牙', self._test_bluetooth_impl), daemon=True).start()
    
    def _test_bluetooth_impl(self):
        try:
            # 检查设备是否断开
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止蓝牙测试")
                return

            self.update_test_status('蓝牙', 'testing')
            logger.info(f"窗口{self.panel_id + 1}开始蓝牙测试")
            result = self.http_client.scan_bluetooth(duration=8)

            if result and result.get('code') == 0:
                data = result.get('data')
                if data is not None and data:
                    device_count = len(data) if isinstance(data, list) else 0
                    # 检查是否有截断标记（由 speaker_http_client 添加）
                    is_truncated = result.get('_truncated', False)
                    if is_truncated:
                        logger.warning(f"窗口{self.panel_id + 1}蓝牙扫描响应被截断，实际设备数量可能更多（已解析 {device_count} 个完整设备）")
                    else:
                        logger.info(f"窗口{self.panel_id + 1}蓝牙测试通过，扫描到 {device_count} 个蓝牙设备")
                    self.update_test_status('蓝牙', 'passed')
                else:
                    logger.error(f"窗口{self.panel_id + 1}蓝牙测试失败: data为空")
                    self.update_test_status('蓝牙', 'failed')
            else:
                logger.error(f"窗口{self.panel_id + 1}蓝牙测试失败")
                self.update_test_status('蓝牙', 'failed')
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}蓝牙测试异常: {e}")
            self.update_test_status('蓝牙', 'failed')
    
    def test_starflash(self):
        """星闪测试"""
        threading.Thread(target=lambda: self._run_single_test('星闪', self._test_starflash_impl), daemon=True).start()
    
    def _test_starflash_impl(self):
        try:
            # 检查设备是否断开
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止星闪测试")
                return

            self.update_test_status('星闪', 'testing')
            logger.info(f"窗口{self.panel_id + 1}开始星闪测试")
            result = self.http_client.scan_sle(duration=8)
            print(f"星闪测试结果: {result}")
            if result and result.get('code') == 0:
                data = result.get('data')
                if data is not None and data:
                    logger.info(f"窗口{self.panel_id + 1}星闪测试通过")
                    self.update_test_status('星闪', 'passed')
                    print(f"窗口{self.panel_id + 1}星闪测试通过，数据: {data}")
                else:
                    logger.error(f"窗口{self.panel_id + 1}星闪测试失败: data为空")
                    self.update_test_status('星闪', 'failed')
                    print(f"窗口{self.panel_id + 1}星闪测试失败: data为空")
            else:
                logger.error(f"窗口{self.panel_id + 1}星闪测试失败")
                self.update_test_status('星闪', 'failed')
                print(f"窗口{self.panel_id + 1}星闪测试失败: {result}")
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}星闪测试异常: {e}")
            self.update_test_status('星闪', 'failed')
            print(f"窗口{self.panel_id + 1}星闪测试异常: {e}")
    
    def test_infrared(self):
        """红外测试"""
        threading.Thread(target=lambda: self._run_single_test('红外', self._test_infrared_impl), daemon=True).start()
    
    def _test_infrared_impl(self):
        EXPECTED_IR_SIGNAL = '4bb445'
        try:
            # 检查设备是否断开
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止红外测试")
                return

            self.update_test_status('红外', 'testing')
            logger.info(f"窗口{self.panel_id + 1}开始红外测试")

            ir_strict = Config().ir_strict_verify
            ir_signal_matched = False

            # 尝试读取串口信号（使用全局串口管理器）
            try:
                from ..utils.serial_manager import SerialManager
                import time

                serial_manager = SerialManager()

                # 获取串口读取器（阻塞等待，最多30秒）
                logger.info(f"窗口{self.panel_id + 1}等待获取串口资源...")
                serial_reader = serial_manager.acquire_serial_reader(timeout=30)

                if serial_reader is None:
                    logger.error(f"窗口{self.panel_id + 1}获取串口超时，其他窗口可能正在使用")
                    # 超时后按非强校验模式处理
                    result = self.http_client.send_ir_blaster("0x4B", "0x45")
                    if not ir_strict and result and result.get('code') == 0:
                        logger.warning(f"窗口{self.panel_id + 1}串口不可用，但HTTP发送成功，非强校验模式通过")
                        self.update_test_status('红外', 'passed')
                    else:
                        logger.error(f"窗口{self.panel_id + 1}串口不可用，红外测试失败")
                        self.update_test_status('红外', 'failed')
                    return

                try:
                    logger.info(f"窗口{self.panel_id + 1}已获取串口资源，开始测试")

                    # 启动后台监听线程
                    if serial_reader.start_listening():
                        logger.info(f"窗口{self.panel_id + 1}后台监听已启动，发送红外信号")

                        # 发送红外信号
                        result = self.http_client.send_ir_blaster("0x4B", "0x45")
                        logger.info(f"窗口{self.panel_id + 1}红外信号已发送，等待接收器响应")

                        # 等待5秒让后台线程接收数据
                        time.sleep(5)

                        # 停止监听并获取数据
                        received = serial_reader.stop_listening()
                        if received:
                            logger.info(f"窗口{self.panel_id + 1}红外接收到信号值: {received}")
                            ir_signal_matched = any(sig == EXPECTED_IR_SIGNAL for sig in received)
                            if ir_signal_matched:
                                logger.info(f"窗口{self.panel_id + 1}红外信号校验通过，匹配预期值: {EXPECTED_IR_SIGNAL}")
                            else:
                                logger.warning(f"窗口{self.panel_id + 1}红外信号校验失败，预期: {EXPECTED_IR_SIGNAL}，实际: {received}")
                        else:
                            logger.warning(f"窗口{self.panel_id + 1}红外测试期间未接收到串口信号")
                    else:
                        logger.warning(f"窗口{self.panel_id + 1}后台监听启动失败，跳过信号读取")
                        result = self.http_client.send_ir_blaster("0x4B", "0x45")
                finally:
                    # 释放串口资源，让其他窗口可以使用
                    serial_manager.release_serial_reader()
                    logger.info(f"窗口{self.panel_id + 1}已释放串口资源")

            except ImportError:
                logger.warning("未安装pyserial库，跳过串口信号读取")
                result = self.http_client.send_ir_blaster("0x4B", "0x45")
            except Exception as e:
                logger.warning(f"窗口{self.panel_id + 1}串口读取失败: {e}")
                result = self.http_client.send_ir_blaster("0x4B", "0x45")

            # 判断红外测试结果
            if ir_strict:
                # 强校验模式：必须收到正确的红外信号
                if ir_signal_matched:
                    logger.info(f"窗口{self.panel_id + 1}红外测试通过（强校验）")
                    self.update_test_status('红外', 'passed')
                else:
                    logger.error(f"窗口{self.panel_id + 1}红外测试失败（强校验：信号不匹配）")
                    self.update_test_status('红外', 'failed')
            else:
                # 非强校验模式：HTTP发送成功即通过
                if result and result.get('code') == 0:
                    logger.info(f"窗口{self.panel_id + 1}红外测试通过")
                    self.update_test_status('红外', 'passed')
                else:
                    logger.error(f"窗口{self.panel_id + 1}红外测试失败")
                    self.update_test_status('红外', 'failed')
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}红外测试异常: {e}")
            self.update_test_status('红外', 'failed')
    
    def test_microwave(self):
        """微波测试"""
        threading.Thread(target=lambda: self._run_single_test('微波', self._test_microwave_impl), daemon=True).start()
    
    def _test_microwave_impl(self):
        try:
            # 检查设备是否断开
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止微波测试")
                return

            self.update_test_status('微波', 'testing')

            # 1. 检查微波感应器固件版本
            logger.info(f"窗口{self.panel_id + 1}开始获取微波感应器固件版本")
            version_result = self.http_client.get_microwave_version()

            if not version_result or version_result.get("code") != 0:
                error_msg = version_result.get('message', '请求失败') if version_result else '请求失败'
                logger.error(f"窗口{self.panel_id + 1}获取微波感应器固件版本失败: {error_msg}")
                self.update_test_status('微波', 'failed')
                return

            version_data = version_result.get("data", {})
            firmware_version = version_data.get("value", "")

            if not firmware_version:
                logger.error(f"窗口{self.panel_id + 1}微波感应器固件版本为空")
                self.update_test_status('微波', 'failed')
                return

            logger.info(f"窗口{self.panel_id + 1}微波感应器固件版本: {firmware_version}")

            # 比较版本号，要求 >= 1.2.1
            stripped_version = _strip_v(firmware_version)
            compare_result = _compare_version(firmware_version, "1.2.1")
            if compare_result < 0:
                logger.error(f"窗口{self.panel_id + 1}微波感应器固件版本校验失败: {firmware_version} (实际比较: {stripped_version} < 1.2.1)")
                self.update_test_status('微波', 'failed')
                return

            logger.info(f"窗口{self.panel_id + 1}微波感应器固件版本校验通过: {firmware_version} (实际比较: {stripped_version} >= 1.2.1)")

            # 2. 进行微波感应测试
            logger.info(f"窗口{self.panel_id + 1}开始微波测试，调用接口: /api/micro/read (duration=5秒, interval=200ms)")

            result = self.http_client.read_microwave()

            logger.info(f"窗口{self.panel_id + 1}微波测试接口返回完整数据: {result}")

            if result and result.get('code') == 0:
                data = result.get('data', {})
                micro_data = data.get('data')

                logger.info(f"窗口{self.panel_id + 1}微波测试data字段: {data}")
                logger.info(f"窗口{self.panel_id + 1}微波感应数据(data['data']): {micro_data}")

                if micro_data is not None and micro_data:
                    # 数据有效性检查
                    is_valid = True
                    reason = ""

                    # 检查1：至少要有数据
                    if len(micro_data) == 0:
                        is_valid = False
                        reason = "数据为空"
                    else:
                        # 检查2：数据是否全部相同（可能是异常固定值）
                        first_item = micro_data[0]
                        all_same = all(item == first_item for item in micro_data)

                        if all_same:
                            # 全部相同，检查距离是否过小（<50cm 认为异常）
                            distance = first_item.get('distance', 0)
                            if distance < 50:
                                is_valid = False
                                reason = f"所有数据完全相同且距离异常({distance}cm < 50cm)，可能感应器未连接或故障"
                                logger.warning(f"窗口{self.panel_id + 1}微波数据异常: {reason}")
                            else:
                                # 距离>=50但全部相同，也可能异常，给出警告但仍通过
                                logger.warning(f"窗口{self.panel_id + 1}微波数据警告: 所有{len(micro_data)}条数据完全相同(distance={distance}cm)，可能存在问题")
                        else:
                            # 检查3：距离范围是否合理（大部分应该>50cm）
                            distances = [item.get('distance', 0) for item in micro_data]
                            valid_distances = [d for d in distances if d >= 50]

                            if len(valid_distances) < len(distances) * 0.5:  # 少于50%的数据距离>=50
                                is_valid = False
                                reason = f"有效距离数据不足({len(valid_distances)}/{len(distances)})，大部分距离<50cm"
                                logger.warning(f"窗口{self.panel_id + 1}微波数据异常: {reason}")

                    if is_valid:
                        logger.info(f"窗口{self.panel_id + 1}微波测试通过，检测到{len(micro_data)}条有效数据: {micro_data}")
                        self.update_test_status('微波', 'passed')
                    else:
                        logger.error(f"窗口{self.panel_id + 1}微波测试失败: {reason}")
                        self.update_test_status('微波', 'failed')
                else:
                    logger.error(f"窗口{self.panel_id + 1}微波测试失败: data['data']为空或None")
                    self.update_test_status('微波', 'failed')
            else:
                error_msg = result.get('message', '未知错误') if result else '请求失败'
                logger.error(f"窗口{self.panel_id + 1}微波测试失败: code={result.get('code') if result else 'None'}, message={error_msg}")
                self.update_test_status('微波', 'failed')
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}微波测试异常: {e}")
            self.update_test_status('微波', 'failed')
    
    def test_production(self):
        """正式环境测试"""
        threading.Thread(target=lambda: self._run_single_test('正式', self._test_production_impl), daemon=True).start()
    
    def _test_production_impl(self):
        try:
            # 检查设备是否断开
            if self.stop_flag.is_set():
                logger.info(f"窗口{self.panel_id + 1}设备已断开，停止正式测试")
                return
            
            self.update_test_status('正式', 'testing')
            logger.info(f"窗口{self.panel_id + 1}开始切换到正式环境")
            result = self.http_client.clear_factory_mode()
            print(f"正式环境切换结果: {result}")
            if result and result.get('code') == 0:
                logger.info(f"窗口{self.panel_id + 1}正式环境切换成功")
                print(f"窗口{self.panel_id + 1}正式环境切换成功")
                self.update_test_status('正式', 'passed')
            else:
                error_msg = result.get('message', '未知错误') if result else '请求失败'
                logger.error(f"窗口{self.panel_id + 1}正式环境切换失败: {error_msg}")
                print(f"窗口{self.panel_id + 1}正式环境切换失败: {error_msg}")
                self.update_test_status('正式', 'failed')
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}正式环境切换异常: {e}")
            print(f"窗口{self.panel_id + 1}正式环境切换异常: {e}")
            self.update_test_status('正式', 'failed')

    def print_label(self):
        """打印标签（检查所有测试项通过后才允许打印）"""
        # 检查所有测试项是否全部通过
        all_passed = all(
            indicator.status == "passed"
            for indicator in self.status_indicators.values()
        )
        if not all_passed:
            QMessageBox.warning(
                self, '无法打印',
                '只有全部测试项通过后才能打印标签。\n请先完成所有测试项。'
            )
            return

        threading.Thread(target=self._print_label_impl, daemon=True).start()
    
    def _print_label_impl(self):
        try:
            logger.info(f"窗口{self.panel_id + 1}开始打印标签")

            # 先上传测试结果
            if self.device:
                from ..utils.upload_service import UploadService
                upload_service = UploadService()
                records = self.main_window.test_db.query_by_sn(self.device.sn)
                if records:
                    logger.info(f"窗口{self.panel_id + 1}上传测试结果: {len(records)}条")
                    # 传入质检类型
                    upload_service.upload_records(records, self.main_window.check_type)

            success = self.main_window.label_printer.print_label(self.device.sn, "PASSED")
            if success:
                logger.info(f"窗口{self.panel_id + 1}标签打印成功")
            else:
                logger.error(f"窗口{self.panel_id + 1}标签打印失败")
        except Exception as e:
            logger.error(f"窗口{self.panel_id + 1}打印标签异常: {e}")


class SpeakerTestWindow(QMainWindow):
    device_found_signal = pyqtSignal(object)
    device_removed_signal = pyqtSignal(str)
    batch_print_completed_signal = pyqtSignal(int, int)  # 批量打印完成信号(成功数, 失败数)

    def __init__(self, speaker_type='indoor'):
        super().__init__()
        self.config = Config()
        self.devices = []
        self.device_cards = []
        self.selected_device = None
        # 从启动参数获取音箱类型，不再从配置读取，且不可切换
        self.current_speaker_type = speaker_type
        self.speaker_device_type = {
            'indoor': DEVICE_TYPE_SMART_HORN,
            'outdoor': DEVICE_TYPE_OUTDOOR_SMART_HORN,
        }[speaker_type]
        self.mac_allocator = MACAllocator()
        self.label_printer = LabelPrinter(self.config)
        self.test_db = TestRecordDB()

        # 测试窗口面板（数量根据布局决定）
        self.test_panels = []
        # 当前布局模式：根据配置决定是否记忆
        if self.config.get_remember_layout():
            self.current_layout = self.config.get_layout_mode()
            logger.info(f"启用布局记忆，加载上次布局：{self.current_layout}宫格")
        else:
            self.current_layout = 4  # 不记忆则默认4宫格
            logger.info("未启用布局记忆，使用默认4宫格布局")
        self.grid_layout = None  # 保存grid_layout引用，用于动态调整

        
        self.zeroconf = None
        self.browser = None
        self.listener = None
        self.master_mdns = None
        self.config_server = None
        self.http_thread = None
        self.mqtt_broker = None
        self.broker_thread = None
        self.ip_scanner = None
        self.firmware_server = None
        
        self.device_refresh_timer = None
        self.device_heartbeat_timer = None
        self.scan_timeout_timer = None
        self._scanning = False

        # 缓存每台设备的 kernel/rootfs 版本（后台 MQTT 查询结果）
        self.device_versions: dict = {}
        # 记录版本查询失败的设备，避免无限重试
        self.version_query_failed: set = set()

        # HTTP 心跳检测机制（修复设备断电后不自动消失的问题）
        self.device_last_seen: dict = {}  # {sn: timestamp} 记录设备最后在线时间
        self.heartbeat_timeout = 3  # 心跳超时时间（秒）- 快速检测离线
        self.heartbeat_check_interval = 2  # 心跳检查间隔（秒）- 高频检测

        # 版本查询防重机制（修复线程泄漏问题）
        self.version_querying: set = set()  # 正在查询版本的设备SN集合

        # P0修复：校时防抖 - 记录已校时的设备及时间戳，避免重复校时
        self.datetime_synced_devices: dict = {}  # {sn: timestamp}
        self.datetime_sync_interval = 300  # 5分钟内不重复校时同一设备

        self.device_found_signal.connect(self._on_device_found_main_thread)
        self.device_removed_signal.connect(self._on_device_removed_main_thread)
        self.batch_print_completed_signal.connect(self._show_batch_print_result)

        self.init_ui()

        # 根据broker模式决定是否启动本地broker
        if self.broker_mode == 'local':
            self.start_mqtt_broker()
        else:
            logger.info(f"远程Broker模式，跳过本地Broker启动，将连接到: {self.config.mqtt_broker}:{self.config.mqtt_port}")

        self.start_http_server()
        self.start_firmware_server()
        self.start_device_discovery()
        self.start_device_refresh_timer()
        self.start_device_heartbeat_timer()

        # 工具启动10秒后，主动查询所有设备版本（解决重启后版本丢失问题）
        QTimer.singleShot(10000, self._query_all_device_versions)

    def _get_icon_path(self):
        """获取图标路径，兼容开发环境和打包环境"""
        if getattr(sys, 'frozen', False):
            # 打包后的环境
            return os.path.join(sys._MEIPASS, 'vdian.ico')
        else:
            # 开发环境
            return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'src', 'ui', 'icon', 'vdian.ico')

    def init_ui(self):
        # 根据音箱类型设置窗口标题
        speaker_type_display = '室内' if self.current_speaker_type == 'indoor' else '室外'
        self.setWindowTitle(f'智能设备产测工具 - {speaker_type_display}音箱模式 v{self.config.app_version}')

        # 设置窗口图标
        icon_path = self._get_icon_path()
        if os.path.exists(icon_path):
            from PyQt5.QtGui import QIcon
            self.setWindowIcon(QIcon(icon_path))
            logger.info(f"SpeakerTestWindow 窗口图标已设置: {icon_path}")

        # 创建菜单栏（门控风格：深蓝色主题）
        menubar = self.menuBar()
        menubar.setNativeMenuBar(False)  # 在窗口内显示菜单栏
        menu_font_size = UIStyles.scale_size(13)
        menu_padding = UIStyles.scale_size(2)
        menu_item_padding_v = UIStyles.scale_size(6)
        menu_item_padding_h = UIStyles.scale_size(12)
        menu_radius = UIStyles.scale_size(4)
        menubar.setStyleSheet(f"""
            QMenuBar {{
                background-color: #4A5F7A;
                color: white;
                font-size: {menu_font_size}px;
                padding: {menu_padding}px;
            }}
            QMenuBar::item {{
                background-color: transparent;
                padding: {menu_item_padding_v}px {menu_item_padding_h}px;
            }}
            QMenuBar::item:selected {{
                background-color: #5a6f8a;
                border-radius: {menu_radius}px;
            }}
            QMenu {{
                background-color: #4A5F7A;
                color: white;
                border: 1px solid #5a6f8a;
            }}
            QMenu::item {{
                padding: {menu_item_padding_v}px {menu_item_padding_h * 2}px;
            }}
            QMenu::item:selected {{
                background-color: {UIStyles.PRIMARY};
            }}
        """)
        
        # 设置菜单
        settings_menu = menubar.addMenu('设置')
        printer_action = settings_menu.addAction('🖨️ 打印机配置')
        printer_action.triggered.connect(self.open_printer_config)

        broker_config_action = settings_menu.addAction('🌐 远程Broker配置')
        broker_config_action.triggered.connect(self.open_broker_config)

        # 移除音箱类型切换菜单（音箱类型在启动时已固定）

        # 测试结果菜单
        view_menu = menubar.addMenu('查看')
        results_action = view_menu.addAction('📊 测试结果')
        results_action.triggered.connect(self.show_test_results)

        logs_action = view_menu.addAction('📋 查看日志')
        logs_action.triggered.connect(self.show_test_logs)

        # 固件升级菜单（位于"查看"右侧）
        firmware_menu = menubar.addMenu('固件升级')
        firmware_action = firmware_menu.addAction('🚀 固件升级')
        firmware_action.triggered.connect(self.open_firmware_upgrade)

        # 版本写入入口
        version_action = firmware_menu.addAction('📝 App版本写入')
        version_action.triggered.connect(self.open_app_version_write)

        # 其它菜单
        other_menu = menubar.addMenu('其它')

        # 质检方式子菜单
        check_type_menu = other_menu.addMenu('🔧 质检方式')

        # 创建质检方式的动作组（单选）
        self.check_type_group = QActionGroup(self)
        self.check_type_group.setExclusive(True)

        # 生产质检选项（默认选中）
        self.production_check_action = check_type_menu.addAction('生产质检')
        self.production_check_action.setCheckable(True)
        self.production_check_action.setChecked(True)
        self.production_check_action.triggered.connect(lambda: self.set_check_type(1))
        self.check_type_group.addAction(self.production_check_action)

        # 仓库质检选项
        self.warehouse_check_action = check_type_menu.addAction('仓库质检')
        self.warehouse_check_action.setCheckable(True)
        self.warehouse_check_action.triggered.connect(lambda: self.set_check_type(2))
        self.check_type_group.addAction(self.warehouse_check_action)

        # 初始化质检类型（默认为生产质检）
        self.check_type = 1

        # 布局切换子菜单
        layout_menu = other_menu.addMenu('📐 布局切换')

        # 创建布局切换的动作组（单选）
        self.layout_group = QActionGroup(self)
        self.layout_group.setExclusive(True)

        # 1宫格选项
        self.layout_1_action = layout_menu.addAction('1宫格')
        self.layout_1_action.setCheckable(True)
        self.layout_1_action.setChecked(self.current_layout == 1)
        self.layout_1_action.triggered.connect(lambda: self.switch_layout(1))
        self.layout_group.addAction(self.layout_1_action)

        # 4宫格选项（默认）
        self.layout_4_action = layout_menu.addAction('4宫格')
        self.layout_4_action.setCheckable(True)
        self.layout_4_action.setChecked(self.current_layout == 4)
        self.layout_4_action.triggered.connect(lambda: self.switch_layout(4))
        self.layout_group.addAction(self.layout_4_action)

        # 9宫格选项
        self.layout_9_action = layout_menu.addAction('9宫格')
        self.layout_9_action.setCheckable(True)
        self.layout_9_action.setChecked(self.current_layout == 9)
        self.layout_9_action.triggered.connect(lambda: self.switch_layout(9))
        self.layout_group.addAction(self.layout_9_action)

        # MQTT Broker模式子菜单
        broker_mode_menu = other_menu.addMenu('🌐 MQTT模式')

        # 创建broker模式的动作组（单选）
        self.broker_mode_group = QActionGroup(self)
        self.broker_mode_group.setExclusive(True)

        # 本地Broker选项
        self.local_broker_action = broker_mode_menu.addAction('本地Broker')
        self.local_broker_action.setCheckable(True)
        self.local_broker_action.triggered.connect(lambda: self.set_broker_mode('local'))
        self.broker_mode_group.addAction(self.local_broker_action)

        # 远程Broker选项
        self.remote_broker_action = broker_mode_menu.addAction('远程Broker')
        self.remote_broker_action.setCheckable(True)
        self.remote_broker_action.triggered.connect(lambda: self.set_broker_mode('remote'))
        self.broker_mode_group.addAction(self.remote_broker_action)

        # 初始化broker模式（从配置读取）
        self.broker_mode = self.config.broker_mode
        if self.broker_mode == 'local':
            self.local_broker_action.setChecked(True)
        else:
            self.remote_broker_action.setChecked(True)
        
        # 根据DPI缩放窗口大小
        window_width = UIStyles.scale_size(1800)
        window_height = UIStyles.scale_size(1000)
        self.setGeometry(50, 50, window_width, window_height)
        
        # Windows系统默认最大化窗口
        if platform.system() == "Windows":
            self.showMaximized()
        
        # 设置主窗口背景色
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {UIStyles.BACKGROUND};
            }}
        """)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout()
        margin = UIStyles.scale_size(10)
        spacing = UIStyles.scale_size(10)
        main_layout.setContentsMargins(margin, margin, margin, margin)
        main_layout.setSpacing(spacing)
        central_widget.setLayout(main_layout)

        # 批量测试按钮区域 - 放在窗口最顶部
        batch_test_container = QWidget()
        batch_test_container.setStyleSheet(f"""
            QWidget {{
                background-color: {UIStyles.CARD_BG};
                border: 1px solid {UIStyles.BORDER};
                border-radius: 8px;
            }}
        """)
        batch_buttons_layout = QHBoxLayout()
        batch_buttons_layout.setContentsMargins(6, 4, 6, 4)
        batch_buttons_layout.setSpacing(UIStyles.scale_size(3))
        batch_test_container.setLayout(batch_buttons_layout)

        # 左侧留空，将按钮推到右边
        batch_buttons_layout.addStretch()

        # 定义按钮样式参数
        border_radius = UIStyles.scale_size(3)
        padding_v = UIStyles.scale_size(2)
        padding_h = UIStyles.scale_size(12)
        btn_height = UIStyles.scale_size(22)

        # 清空设备列表按钮（红色）
        self.clear_devices_btn = QPushButton('清空设备列表')
        self.clear_devices_btn.setFont(UIStyles.get_font(6))
        self.clear_devices_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {UIStyles.ERROR};
                color: white;
                border: none;
                border-radius: {border_radius}px;
                padding: {padding_v}px {padding_h}px;
                font-weight: 600;
                min-height: {btn_height}px;
                max-height: {btn_height}px;
            }}
            QPushButton:hover {{
                background-color: #D32F2F;
            }}
            QPushButton:pressed {{
                background-color: #B71C1C;
            }}
            QPushButton:disabled {{
                background-color: #BDBDBD;
                color: {UIStyles.TEXT_SECONDARY};
            }}
        """)
        self.clear_devices_btn.clicked.connect(self.clear_device_list)
        batch_buttons_layout.addWidget(self.clear_devices_btn)

        # 批量打印按钮（绿色）
        self.batch_print_btn = QPushButton('批量打印')
        self.batch_print_btn.setFont(UIStyles.get_font(6))
        self.batch_print_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {UIStyles.SUCCESS};
                color: white;
                border: none;
                border-radius: {border_radius}px;
                padding: {padding_v}px {padding_h}px;
                font-weight: 600;
                min-height: {btn_height}px;
                max-height: {btn_height}px;
            }}
            QPushButton:hover {{
                background-color: #388E3C;
            }}
            QPushButton:pressed {{
                background-color: #2E7D32;
            }}
            QPushButton:disabled {{
                background-color: #BDBDBD;
                color: {UIStyles.TEXT_SECONDARY};
            }}
        """)
        self.batch_print_btn.clicked.connect(self.batch_print_labels)
        self.batch_print_btn.setEnabled(False)  # 默认置灰，只有选择设备后才亮
        batch_buttons_layout.addWidget(self.batch_print_btn)

        # 一键老化按钮（橙色突出显示）
        self.batch_aging_btn = QPushButton('一键老化')
        self.batch_aging_btn.setFont(UIStyles.get_font(6))
        self.batch_aging_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {UIStyles.WARNING};
                color: white;
                border: none;
                border-radius: {border_radius}px;
                padding: {padding_v}px {padding_h}px;
                font-weight: 600;
                min-height: {btn_height}px;
                max-height: {btn_height}px;
            }}
            QPushButton:hover {{
                background-color: #F57C00;
            }}
            QPushButton:pressed {{
                background-color: {UIStyles.WARNING};
            }}
            QPushButton:disabled {{
                background-color: #BDBDBD;
                color: {UIStyles.TEXT_SECONDARY};
            }}
        """)
        self.batch_aging_btn.clicked.connect(lambda: self.batch_test('aging'))
        self.batch_aging_btn.setEnabled(False)
        batch_buttons_layout.addWidget(self.batch_aging_btn)

        # 其他批量测试按钮（根据音箱类型动态生成）
        batch_buttons_config = [
            ('烧录', 'burn'),
            ('音麦', 'audio_mic'),
            ('WiFi', 'wifi'),
            ('蓝牙', 'bluetooth'),
            ('星闪', 'starflash'),
        ]

        # 根据音箱类型添加红外或微波
        if self.current_speaker_type == 'indoor':
            batch_buttons_config.append(('红外', 'infrared'))
        else:
            batch_buttons_config.append(('微波', 'microwave'))

        for btn_text, test_type in batch_buttons_config:
            btn = QPushButton(btn_text)
            btn.setFont(UIStyles.get_font(6))
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {UIStyles.PRIMARY};
                    color: white;
                    border: none;
                    border-radius: {border_radius}px;
                    padding: {padding_v}px {padding_h}px;
                    font-weight: 600;
                    min-height: {btn_height}px;
                    max-height: {btn_height}px;
                }}
                QPushButton:hover {{
                    background-color: {UIStyles.PRIMARY_DARK};
                }}
                QPushButton:pressed {{
                    background-color: {UIStyles.PRIMARY};
                }}
                QPushButton:disabled {{
                    background-color: #BDBDBD;
                    color: {UIStyles.TEXT_SECONDARY};
                }}
            """)
            btn.clicked.connect(lambda checked, t=test_type: self.batch_test(t))
            btn.setEnabled(False)
            batch_buttons_layout.addWidget(btn)
            setattr(self, f'batch_{test_type}_btn', btn)

        main_layout.addWidget(batch_test_container)

        # 主内容区域：左侧设备列表 + 右侧4窗口
        content_layout = QHBoxLayout()
        content_layout.setSpacing(UIStyles.scale_size(10))
        
        # 左侧设备列表
        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(UIStyles.scale_size(10))
        left_widget.setLayout(left_layout)
        
        # 设备列表标题行：全选复选框 + 标签 + 扫描按钮 + 停止扫描按钮
        device_header = QHBoxLayout()
        device_header.setContentsMargins(0, 0, 0, 0)
        device_header.setSpacing(UIStyles.scale_size(8))

        # 全选复选框
        from PyQt5.QtWidgets import QCheckBox
        self.select_all_checkbox = QCheckBox()
        self.select_all_checkbox.setFixedSize(UIStyles.scale_size(18), UIStyles.scale_size(18))
        self.select_all_checkbox.setToolTip('全选/取消全选')
        self.select_all_checkbox.stateChanged.connect(self.on_select_all_changed)
        device_header.addWidget(self.select_all_checkbox)

        device_label = QLabel('设备列表')
        device_label.setFont(UIStyles.get_font(6, bold=True))  # 和按钮字体一样大
        device_header.addWidget(device_label)

        # 分配按钮（新增）
        border_radius = UIStyles.scale_size(3)
        padding_v = UIStyles.scale_size(1)  # 进一步缩小
        padding_h = UIStyles.scale_size(8)  # 进一步缩小
        btn_height = UIStyles.scale_size(18)  # 按钮高度进一步减小

        self.assign_button = QPushButton('分配')  # 去掉图标
        self.assign_button.setFont(UIStyles.get_font(6))
        self.assign_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {UIStyles.SUCCESS};
                color: white;
                border: none;
                border-radius: {border_radius}px;
                padding: {padding_v}px {padding_h}px;
                font-weight: 600;
                min-height: {btn_height}px;
                max-height: {btn_height}px;
            }}
            QPushButton:hover {{
                background-color: #388E3C;
            }}
            QPushButton:pressed {{
                background-color: #2E7D32;
            }}
        """)
        self.assign_button.clicked.connect(self.assign_selected_devices)
        device_header.addWidget(self.assign_button)

        # 手动扫描按钮（与一键老化样式一致）
        self.scan_button = QPushButton('扫描')  # 去掉图标
        self.scan_button.setFont(UIStyles.get_font(6))
        self.scan_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {UIStyles.PRIMARY};
                color: white;
                border: none;
                border-radius: {border_radius}px;
                padding: {padding_v}px {padding_h}px;
                font-weight: 600;
                min-height: {btn_height}px;
                max-height: {btn_height}px;
            }}
            QPushButton:hover {{
                background-color: {UIStyles.PRIMARY_DARK};
            }}
            QPushButton:pressed {{
                background-color: {UIStyles.PRIMARY};
            }}
        """)
        self.scan_button.clicked.connect(self.manual_scan_devices)
        device_header.addWidget(self.scan_button)

        # 停止扫描按钮（与一键老化样式一致）
        self.stop_scan_button = QPushButton('停止')  # 去掉图标
        self.stop_scan_button.setFont(UIStyles.get_font(6))
        self.stop_scan_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {UIStyles.ERROR};
                color: white;
                border: none;
                border-radius: {border_radius}px;
                padding: {padding_v}px {padding_h}px;
                font-weight: 600;
                min-height: {btn_height}px;
                max-height: {btn_height}px;
            }}
            QPushButton:hover {{
                background-color: #D32F2F;
            }}
            QPushButton:pressed {{
                background-color: #B71C1C;
            }}
        """)
        self.stop_scan_button.clicked.connect(self.stop_scan_devices)
        device_header.addWidget(self.stop_scan_button)

        device_header.addStretch()

        left_layout.addLayout(device_header)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setMinimumWidth(UIStyles.scale_size(280))
        scroll_area.setMaximumWidth(UIStyles.scale_size(340))
        scroll_area.setFrameShape(QFrame.NoFrame)
        
        self.device_container = QWidget()
        self.device_layout = QVBoxLayout()
        self.device_layout.setContentsMargins(0, 0, 0, 0)
        spacing = UIStyles.scale_size(8)
        self.device_layout.setSpacing(spacing)
        self.device_layout.addStretch()
        self.device_container.setLayout(self.device_layout)
        
        scroll_area.setWidget(self.device_container)
        left_layout.addWidget(scroll_area)

        # 设备数量显示标签
        self.device_count_label = QLabel('设备数量: 0')
        self.device_count_label.setFont(UIStyles.get_font(5, bold=True))
        self.device_count_label.setStyleSheet(f"""
            QLabel {{
                color: {UIStyles.TEXT_SECONDARY};
                padding: 5px;
                background-color: {UIStyles.SURFACE};
                border-radius: 4px;
            }}
        """)
        self.device_count_label.setAlignment(Qt.AlignCenter)
        left_layout.addWidget(self.device_count_label)

        content_layout.addWidget(left_widget)

        # 右侧区域：包含测试窗口网格和翻页按钮
        right_widget = QWidget()
        right_main_layout = QVBoxLayout()
        right_main_layout.setContentsMargins(0, 0, 0, 0)
        right_main_layout.setSpacing(UIStyles.scale_size(10))
        right_widget.setLayout(right_main_layout)

        # 右侧测试窗口网格布局（容器）
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout()
        self.grid_layout.setSpacing(UIStyles.scale_size(15))
        self.grid_layout.setContentsMargins(
            UIStyles.scale_size(5),
            UIStyles.scale_size(5),
            UIStyles.scale_size(5),
            UIStyles.scale_size(5)
        )
        self.grid_container.setLayout(self.grid_layout)

        # 根据当前布局模式创建测试面板
        self._create_test_panels(self.current_layout)

        right_main_layout.addWidget(self.grid_container, 1)

        content_layout.addWidget(right_widget, 1)

        main_layout.addLayout(content_layout)

        logger.info(f"{self.current_layout}宫格布局初始化完成")

    def _create_test_panels(self, layout_mode):
        """根据布局模式创建测试面板
        Args:
            layout_mode: 1, 4, 或 9
        """
        # 清空现有面板
        for panel in self.test_panels:
            self.grid_layout.removeWidget(panel)
            panel.setParent(None)
            panel.deleteLater()
        self.test_panels.clear()

        # 清除所有行列的拉伸因子（重置为0）
        for i in range(self.grid_layout.rowCount()):
            self.grid_layout.setRowStretch(i, 0)
        for i in range(self.grid_layout.columnCount()):
            self.grid_layout.setColumnStretch(i, 0)

        # 根据布局模式确定行列数
        if layout_mode == 1:
            rows, cols = 1, 1
        elif layout_mode == 4:
            rows, cols = 2, 2
        elif layout_mode == 9:
            rows, cols = 3, 3
        else:
            rows, cols = 2, 2  # 默认4宫格

        # 创建面板
        panel_count = rows * cols
        for i in range(panel_count):
            panel = TestWindowPanel(i, self)
            panel.set_speaker_type(self.current_speaker_type)

            # 调整面板内部组件以适应布局模式
            panel.adjust_for_layout_mode(layout_mode)

            self.test_panels.append(panel)
            row = i // cols
            col = i % cols
            self.grid_layout.addWidget(panel, row, col)

        # 设置行列拉伸因子，使所有窗口均匀分布
        for row in range(rows):
            self.grid_layout.setRowStretch(row, 1)
        for col in range(cols):
            self.grid_layout.setColumnStretch(col, 1)

        logger.info(f"已创建 {panel_count} 个测试面板 ({rows}x{cols} 布局)")

    def switch_layout(self, layout_mode):
        """切换布局模式
        Args:
            layout_mode: 1, 4, 或 9
        """
        if layout_mode == self.current_layout:
            return

        # 9宫格切换前需要用户确认
        if layout_mode == 9:
            reply = QMessageBox.question(
                self,
                '切换到9宫格布局',
                '9宫格布局会同时显示9个测试窗口，内容较为紧凑。\n\n'
                '为了获得最佳的使用体验，建议在以下环境中使用：\n'
                '• 28寸及以上显示器\n'
                '• 1920×1080或更高分辨率\n\n'
                '是否切换到9宫格布局？',
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Cancel
            )

            if reply == QMessageBox.Cancel:
                logger.info("用户取消切换到9宫格布局")
                return

        # 保存当前窗口状态和大小
        was_maximized = self.isMaximized()
        if not was_maximized:
            saved_geometry = self.geometry()

        # 保存当前所有设备绑定（包括面板ID，以便恢复到相同位置）
        bound_devices_map = {}  # {panel_id: device}
        for panel in self.test_panels:
            if panel.device:
                bound_devices_map[panel.panel_id] = panel.device
                panel.bind_device(None)  # 解绑设备

        # 切换布局
        old_layout = self.current_layout
        self.current_layout = layout_mode

        # 更新每页设备数
        if layout_mode == 1:
            self.devices_per_page = 1
        elif layout_mode == 4:
            self.devices_per_page = 4
        elif layout_mode == 9:
            self.devices_per_page = 9

        # 重置到第一页
        self.current_page = 0

        self._create_test_panels(layout_mode)

        # 恢复设备绑定到相同的窗口位置（如果该���置仍然存在）
        for panel_id, device in bound_devices_map.items():
            if panel_id < len(self.test_panels):
                self.test_panels[panel_id].bind_device(device)
                logger.info(f"恢复设备 {device.sn} 到窗口 {panel_id + 1}")

        # 恢复窗口状态和大小
        if was_maximized:
            # 保持最大化状态
            if not self.isMaximized():
                self.showMaximized()
        else:
            # 恢复原来的窗口大小和位置
            if self.isMaximized():
                self.showNormal()
            self.setGeometry(saved_geometry)

        # 更新菜单选中状态
        self.layout_1_action.setChecked(layout_mode == 1)
        self.layout_4_action.setChecked(layout_mode == 4)
        self.layout_9_action.setChecked(layout_mode == 9)

        # 如果启用了布局记忆，保存到配置文件
        if self.config.get_remember_layout():
            self.config.save_layout_mode(layout_mode)
            logger.info(f"布局记忆已启用，保存布局模式：{layout_mode}宫格")

        logger.info(f"布局已从 {old_layout}宫格切换到 {layout_mode}宫格，每页显示 {self.devices_per_page} 个设备")

    def _get_local_broker_ip(self):
        """动态获取本机IP地址（用于本地Broker模式）"""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            broker_ip = s.getsockname()[0]
            s.close()
            return broker_ip
        except Exception as e:
            logger.warning(f"获取本机IP失败: {e}，使用配置的broker地址")
            return self.config.mqtt_broker

    def start_mqtt_broker(self):
        try:
            self.mqtt_broker = MQTTBrokerManager(
                host='0.0.0.0',
                port=self.config.mqtt_port,
                ssl_enabled=True
            )
            self.broker_thread = threading.Thread(
                target=self.mqtt_broker.start,
                daemon=True
            )
            self.broker_thread.start()
            logger.info(f"MQTT Broker启动中: 0.0.0.0:{self.config.mqtt_port}")
        except Exception as e:
            logger.error(f"MQTT Broker启动失败: {e}")
    
    def start_http_server(self):
        try:
            self.config_server = ConfigServer(
                host='0.0.0.0',
                port=self.config.http_port,
                mqtt_broker=self.config.mqtt_broker,
                mqtt_port=self.config.mqtt_port,
                secret_key=self.config.device_psk,
                on_device_config_callback=self._on_device_config_request,
                broker_mode=self.broker_mode  # 传入broker模式
            )
            self.http_thread = threading.Thread(
                target=self.config_server.start,
                daemon=True
            )
            self.http_thread.start()
            logger.info(f"HTTP配置服务已启动: 0.0.0.0:{self.config.http_port}")
        except Exception as e:
            logger.error(f"HTTP配置服务启动失败: {e}")

    def _on_device_config_request(self, sn: str, product_id: str):
        """设备请求配置时的回调，更新设备的 product_id"""
        try:
            # 查找设备并更新 product_id
            for device in self.devices:
                if device.sn == sn:
                    device.product_id = product_id
                    logger.info(f"更新设备 {sn} 的 product_id: {product_id}")
                    break
        except Exception as e:
            logger.error(f"更新设备 product_id 失败: {e}")

    def start_firmware_server(self):
        try:
            self.firmware_server = FirmwareHTTPServer(host='0.0.0.0', port=8000)
            self.firmware_server.start()
        except Exception as e:
            logger.error(f"固件HTTP服务启动失败: {e}")

    def open_firmware_upgrade(self):
        """打开固件升级对话框"""
        from .firmware_upgrade_dialog import FirmwareUpgradeDialog
        if not self.devices:
            QMessageBox.information(self, '提示', '当前没有已发现的设备，请先扫描设备')
            return
        dialog = FirmwareUpgradeDialog(self.devices, self.firmware_server, self.config, self)
        dialog.upgrade_finished_signal.connect(self._on_device_upgrade_finished)
        dialog.exec_()

    def set_check_type(self, check_type: int):
        """设置质检类型"""
        self.check_type = check_type
        check_type_name = "生产质检" if check_type == 1 else "仓库质检"
        logger.info(f"质检方式已切换为: {check_type_name} (check_type={check_type})")
        QMessageBox.information(self, '质检方式', f'已切换为: {check_type_name}')

    def set_broker_mode(self, mode: str):
        """切换MQTT Broker模式"""
        if mode == self.broker_mode:
            return

        # 确认切换
        mode_name = "本地Broker" if mode == 'local' else "远程Broker"
        reply = QMessageBox.question(
            self, 'MQTT模式切换',
            f'切换到{mode_name}模式需要重启软件才能完全生效。\n\n'
            f'切换后的变化：\n'
            f'• 本地Broker模式：工具启动本地MQTT服务\n'
            f'• 远程Broker模式：工具连接到主控的MQTT服务\n\n'
            f'确认切换吗？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.No:
            # 恢复原来的选中状态
            if self.broker_mode == 'local':
                self.local_broker_action.setChecked(True)
            else:
                self.remote_broker_action.setChecked(True)
            return

        logger.info(f"切换MQTT Broker模式: {self.broker_mode} -> {mode}")

        # 保存配置
        self.config.save_broker_mode(mode)
        old_mode = self.broker_mode
        self.broker_mode = mode

        # 提示需要重启
        if mode == 'local':
            message = (
                f'MQTT模式已切换为：本地Broker\n\n'
                f'请重启软件以使配置生效。\n'
                f'重启后工具将启动本地MQTT Broker服务，\n'
                f'设备将连接到产测工具的Broker。'
            )
        else:
            message = (
                f'MQTT模式已切换为：远程Broker\n\n'
                f'请重启软件以使配置生效。\n'
                f'重启后设备将连接到主控的Broker：\n'
                f'{self.config.mqtt_broker}:{self.config.mqtt_port}\n\n'
                f'提示：\n'
                f'• 如需修改远程Broker地址，请在"设置"菜单中打开"远程Broker配置"\n'
                f'• 如需切回本地模式，请点击"其它 → MQTT模式 → 本地Broker"'
            )

        QMessageBox.information(self, 'MQTT模式切换', message)

        logger.info(f"MQTT Broker模式已切换: {old_mode} -> {mode}，配置已保存，需要重启软件生效")

    def _on_device_upgrade_finished(self, sn: str, success: bool):
        """某台设备升级结束后，若成功则清版本缓存并重新查询"""
        if not success:
            return
        # 清掉旧版本缓存，让卡片先显示"版本查询中..."
        self.device_versions.pop(sn, None)
        self.update_device_list()
        # 找到对应设备对象，后台重新查询 kernel/rootfs 版本
        device = next((d for d in self.devices if d.sn == sn), None)
        if device:
            self._query_device_versions(device)

    def _on_device_version_write_finished(self, sn: str, success: bool, new_version: str):
        """App版本写入完成后，更新设备信息中的app版本"""
        if not success or not new_version:
            return

        # 找到对应的设备对象并更新fw_ver
        device = next((d for d in self.devices if d.sn == sn), None)
        if device:
            device.fw_ver = new_version
            logger.info(f"设备 {sn} App版本已更新: {new_version}")
            # 同时更新版本缓存中的app_version
            if sn in self.device_versions:
                self.device_versions[sn]['app_version'] = _strip_v(new_version)
            # 刷新设备列表显示
            self.update_device_list()

    def open_app_version_write(self):
        """打开 App 版本写入对话框"""
        from .app_version_dialog import AppVersionDialog
        if not self.devices:
            QMessageBox.information(self, '提示', '当前没有已发现的设备，请先扫描设备')
            return
        dialog = AppVersionDialog(self.devices, self.config, self)
        dialog.version_write_finished_signal.connect(self._on_device_version_write_finished)
        dialog.exec_()

    def start_device_discovery(self):
        logger.info("启动设备发现...")
        try:
            logger.info("初始化 Zeroconf...")
            self.zeroconf = Zeroconf()
            
            # 尝试获取网络接口信息（兼容不同版本的zeroconf）
            try:
                if hasattr(self.zeroconf, 'interfaces'):
                    logger.info(f"Zeroconf 初始化成功，接口: {self.zeroconf.interfaces}")
                else:
                    logger.info("Zeroconf 初始化成功")
            except Exception as e:
                logger.debug(f"获取Zeroconf接口信息失败: {e}")
            
            self.master_mdns = MasterMdnsService(self.zeroconf, port=self.config.http_port)
            self.master_mdns.register()
            
            logger.info(f"开始监听 mDNS 服务类型: {self.config.mdns_service_type}")
            # 只接受当前产测模式对应的音箱类型
            self.listener = DeviceDiscoveryListener(
                on_device_found=self.on_device_found,
                on_device_removed=self.on_device_removed,
                device_types=[self.speaker_device_type]
            )
            self.browser = ServiceBrowser(self.zeroconf, self.config.mdns_service_type, self.listener)
            logger.info(f"mDNS设备发现服务已启动 (监听类型: {self.config.mdns_service_type}, 设备类型: {self.speaker_device_type})")
            
            debug_listener = DebugServiceListener()
            common_service_types = [
                "_http._tcp.local.",
                "_https._tcp.local.",
                "_mqtt._tcp.local.",
                "_ssh._tcp.local.",
                "_device-info._tcp.local.",
                "_workstation._tcp.local.",
            ]
            
            logger.info("启动mDNS调试监听器，扫描常见服务类型...")
            for service_type in common_service_types:
                try:
                    ServiceBrowser(self.zeroconf, service_type, debug_listener)
                    logger.debug(f"  监听服务类型: {service_type}")
                except Exception as e:
                    logger.warning(f"  监听服务类型失败 {service_type}: {e}")
            
            # IP扫描已禁用，只使用mDNS发现（参考旧项目设计）
            # self.ip_scanner = IPScanner(self.on_device_found)
            # logger.info("IP扫描器已初始化")
            logger.info("音箱产测只使用mDNS发现，不使用IP扫描")

            # threading.Thread(target=self._start_ip_scan, daemon=True).start()
            
        except Exception as e:
            logger.error(f"设备发现启动失败: {e}")
    
    def _start_ip_scan(self):
        import time
        time.sleep(2)
        logger.info("启动IP扫描（自动检测网段）...")
        self.ip_scanner.scan_local_subnet(1, 254)
    
    def start_device_refresh_timer(self):
        self.device_refresh_timer = QTimer()
        self.device_refresh_timer.timeout.connect(self.refresh_devices)
        self.device_refresh_timer.start(5000)  # 改为5秒，提高发现频率
        logger.info("设备刷新定时器已启动 (间隔: 5秒)")

    def start_device_heartbeat_timer(self):
        """启动HTTP心跳检测定时器"""
        self.device_heartbeat_timer = QTimer()
        self.device_heartbeat_timer.timeout.connect(self._check_device_heartbeat)
        self.device_heartbeat_timer.start(self.heartbeat_check_interval * 1000)
        logger.info(f"设备心跳检测定时器已启动 (间隔: {self.heartbeat_check_interval}秒, 超时: {self.heartbeat_timeout}秒)")
    
    def on_select_all_changed(self, state):
        """全选/取消全选"""
        from PyQt5.QtCore import Qt
        checked = (state == Qt.Checked)
        for card in self.device_cards:
            card.set_checked(checked)
        self.update_batch_buttons_state()

    def update_batch_buttons_state(self):
        """更新批量测试按钮的启用状态"""
        checked_count = sum(1 for card in self.device_cards if card.checked)
        has_selection = checked_count > 0

        # 更新所有批量测试按钮的状态（包括批量打印按钮）
        self.batch_aging_btn.setEnabled(has_selection)
        self.batch_print_btn.setEnabled(has_selection)  # 批量打印按钮也根据选择状态启用

        batch_test_types = ['burn', 'audio_mic', 'wifi', 'bluetooth', 'starflash']
        # 根据音箱类型添加红外或微波
        if self.current_speaker_type == 'indoor':
            batch_test_types.append('infrared')
        else:
            batch_test_types.append('microwave')

        for test_type in batch_test_types:
            btn = getattr(self, f'batch_{test_type}_btn', None)
            if btn:
                btn.setEnabled(has_selection)

    def batch_test(self, test_type):
        """批量测试选中的设备"""
        from PyQt5.QtWidgets import QMessageBox

        # 获取所有选中的设备
        checked_devices = [card.device for card in self.device_cards if card.checked]

        if not checked_devices:
            QMessageBox.warning(self, '提示', '请先选择要测试的设备')
            return

        logger.info(f"批量测试 {test_type}，选中设备数量: {len(checked_devices)}")

        # 检查哪些设备没有分配到窗口
        unassigned_devices = []
        assigned_panels = []

        for device in checked_devices:
            # 查找该设备是否已绑定到窗口
            bound_panel = None
            for panel in self.test_panels:
                if panel.device and panel.device.sn == device.sn:
                    bound_panel = panel
                    break

            if bound_panel:
                assigned_panels.append((device, bound_panel))
            else:
                unassigned_devices.append(device)

        # 如果有未分配窗口的设备，提示用户手动分配
        if unassigned_devices:
            device_names = '\n'.join([f"• {dev.get_display_name()}" for dev in unassigned_devices])
            QMessageBox.warning(
                self,
                '设备未分配到窗口',
                f'以下设备尚未分配到测试窗口，请先使用设备卡片上的 🪟 按钮手动分配窗口：\n\n'
                f'{device_names}\n\n'
                f'分配窗口后，可重新执行批量测试。'
            )
            logger.warning(f"批量测试中止：{len(unassigned_devices)} 个设备未分配窗口")
            return

        # 所有设备都已分配，执行测试
        test_count = 0
        for device, panel in assigned_panels:
            if test_type == 'aging':
                panel.start_auto_test()
            elif test_type == 'burn':
                panel.test_burn()
            elif test_type == 'audio_mic':
                panel.test_audio_mic()
            elif test_type == 'wifi':
                panel.test_wifi()
            elif test_type == 'bluetooth':
                panel.test_bluetooth()
            elif test_type == 'starflash':
                panel.test_starflash()
            elif test_type == 'infrared':
                panel.test_infrared()
            elif test_type == 'microwave':
                panel.test_microwave()
            test_count += 1

        logger.info(f"批量测试已启动，成功启动 {test_count}/{len(checked_devices)} 个设备")

    def assign_selected_devices(self):
        """将选中的设备按顺序分配到窗口"""
        from PyQt5.QtWidgets import QMessageBox

        # 获取所有选中的设备
        checked_devices = [card.device for card in self.device_cards if card.checked]

        if not checked_devices:
            QMessageBox.warning(self, '提示', '请先选择要分配的设备')
            return

        # 获取可用窗口数量
        available_panels = len(self.test_panels)
        selected_count = len(checked_devices)

        logger.info(f"开始分配设备：选中 {selected_count} 个设备，可用窗口 {available_panels} 个")

        # 检查数量是否匹配
        if selected_count > available_panels:
            # 选中的设备数量超过窗口数量，提示用户
            QMessageBox.warning(
                self,
                '设备数量超出',
                f'您选择了 {selected_count} 个设备，但当前只有 {available_panels} 个测��窗口。\n\n'
                f'只会将前 {available_panels} 个设备分配到窗口，其余设备将被忽略。'
            )
            logger.warning(f"设备数量 ({selected_count}) 超过窗口数量 ({available_panels})，只分配前 {available_panels} 个")

        # 按顺序分配设备到窗口
        assigned_count = 0
        for i in range(min(selected_count, available_panels)):
            device = checked_devices[i]
            panel = self.test_panels[i]

            # 如果窗口已有设备，先解绑
            if panel.device:
                logger.info(f"窗口 {panel.panel_id + 1} 已有设备 {panel.device.sn}，将被替换")

            # 分配设备到窗口
            panel.bind_device(device)
            assigned_count += 1
            logger.info(f"设备 {device.get_display_name()} 已分配到窗口 {panel.panel_id + 1}")

        logger.info(f"设备分配完成：共分配 {assigned_count} 个设备")

    def clear_device_list(self):
        """清空设备列表和窗口分配"""
        if not self.devices:
            return

        logger.info(f"开始清空设备列表，当前有 {len(self.devices)} 个设备")

        # 解绑所有窗口的设备
        for panel in self.test_panels:
            if panel.device:
                panel.bind_device(None)

        # 清空设备列表
        self.devices.clear()

        # 清空版本缓存
        self.device_versions.clear()

        # 清空版本查询失败记录
        self.version_query_failed.clear()

        # 清空校时记录
        self.datetime_synced_devices.clear()

        # 清空选中设备
        self.selected_device = None

        # 更新设备列表显示
        self.update_device_list()

        logger.info("设备列表已清空")

    def batch_print_labels(self):
        """批量打印选中设备的标签"""
        # 获取所有选中的设备
        checked_devices = [card.device for card in self.device_cards if card.checked]

        # 按钮已置灰，不会出现未选择设备的情况，移除警告弹窗
        if not checked_devices:
            return

        logger.info(f"开始批量打印标签，选中 {len(checked_devices)} 个设备")

        # 在后台线程中执行批量打印
        threading.Thread(target=self._batch_print_impl, args=(checked_devices,), daemon=True).start()

    def _batch_print_impl(self, devices):
        """批量打印实现（后台线程执行）"""
        from ..utils.upload_service import UploadService

        total = len(devices)
        success_count = 0
        fail_count = 0
        skipped_count = 0  # 跳过的设备数（测试未全部通过）

        for i, device in enumerate(devices, 1):
            try:
                # 检查该设备的测试记录是否全部通过
                records = self.test_db.query_by_sn(device.sn)
                if not records:
                    logger.warning(f"设备 {device.sn} 没有测试记录，跳过打印 ({i}/{total})")
                    skipped_count += 1
                    fail_count += 1
                    continue

                # 检查是否所有测试项都通过
                all_passed = all(record.get('results') == 'PASS' for record in records)
                if not all_passed:
                    logger.warning(f"设备 {device.sn} 测试未全部通过，跳过打印 ({i}/{total})")
                    skipped_count += 1
                    fail_count += 1
                    continue

                logger.info(f"正在打印第 {i}/{total} 个设备: {device.sn}")

                # 先上传测试结果
                upload_service = UploadService()
                logger.info(f"上传设备 {device.sn} 的测试结果: {len(records)}条")
                upload_service.upload_records(records, self.check_type)

                # 打印标签
                success = self.label_printer.print_label(device.sn, "PASSED")
                if success:
                    success_count += 1
                    logger.info(f"设备 {device.sn} 标签打印成功 ({i}/{total})")
                else:
                    fail_count += 1
                    logger.error(f"设备 {device.sn} 标签打印失败 ({i}/{total})")

                # 打印间隔，避免打印机过载
                if i < total:
                    time.sleep(0.5)

            except Exception as e:
                fail_count += 1
                logger.error(f"打印设备 {device.sn} 标签时发生异常: {e}")

        logger.info(f"批量打印完成: 成功 {success_count} 个，失败/跳过 {fail_count} 个（其中 {skipped_count} 个因测试未通过被跳过）")

        # 发送信号到主线程显示完成弹窗
        self.batch_print_completed_signal.emit(success_count, fail_count)

    def _show_batch_print_result(self, success_count, fail_count):
        """显示批量打印完成结果弹窗（主线程执行）"""
        total = success_count + fail_count
        if fail_count == 0:
            # 全部成功
            QMessageBox.information(
                self,
                '批量打印完成',
                f'批量打印完成\n\n成功：{success_count} 个\n失败：{fail_count} 个'
            )
        else:
            # 有失败的
            QMessageBox.warning(
                self,
                '批量打印完成',
                f'批量打印完成\n\n成功：{success_count} 个\n失败：{fail_count} 个'
            )

    def manual_scan_devices(self):
        """手动触发设备扫描（15秒超时）"""
        # 防止重复点击
        if getattr(self, '_scanning', False):
            return

        logger.info("手动扫描设备...")
        self._scanning = True
        self._scan_start_count = len(self.devices)
        self.scan_button.setEnabled(False)
        self.scan_button.setText("扫描中...")
        self.stop_scan_button.show()  # 显示停止按钮

        # 清除版本查询失败记录，给设备重新查询的机会
        self.version_query_failed.clear()
        logger.info("已清除版本查询失败记录，重新扫描时将尝试获取版本")

        # 标记为手动扫描，强制重新查询所有设备版本（包括已有缓存的）
        self._is_manual_scan = True

        # 后台触发扫描（仅mDNS刷新，不使用IP扫描）
        def scan_thread():
            try:
                self.refresh_devices()
                # IP扫描已禁用，只使用mDNS
                # if self.ip_scanner:
                #     self.ip_scanner.scan_local_subnet(1, 254)
            except Exception as e:
                logger.error(f"手动扫描失败: {e}")

        threading.Thread(target=scan_thread, daemon=True).start()

        # 主线程15秒超时，到点恢复按钮并给出提示
        self.scan_timeout_timer = QTimer(self)
        self.scan_timeout_timer.setSingleShot(True)
        self.scan_timeout_timer.timeout.connect(self._on_scan_finished)
        self.scan_timeout_timer.start(15000)

    def stop_scan_devices(self):
        """停止扫描（包括手动扫描和自动扫描）"""
        # 如果正在手动扫描，停止手动扫描
        if getattr(self, '_scanning', False):
            logger.info("用户手动停止扫描")

            # 立即停止扫描定时器
            if self.scan_timeout_timer and self.scan_timeout_timer.isActive():
                self.scan_timeout_timer.stop()

            # 调用扫描结束处理
            self._on_scan_finished()
            return

        # 否则停止自动扫描（定时刷新）
        if self.device_refresh_timer and self.device_refresh_timer.isActive():
            self.device_refresh_timer.stop()
            logger.info("自动扫描已停止")
            self.scan_button.setText("扫描")  # 统一为"扫描"
            QMessageBox.information(self, '停止扫描', '自动扫描已停止\n\n点击"扫描"按钮可重新启动自动扫描')
        else:
            # 如果定时器已停止，则重新启动
            if self.device_refresh_timer:
                self.device_refresh_timer.start(5000)
                logger.info("自动扫描已启动")
                self.scan_button.setText("扫描")  # 统一为"扫描"
                QMessageBox.information(self, '启动扫描', '自动扫描已启动\n\n每5秒自动刷新设备列表')

    def _on_scan_finished(self):
        """扫描超时（15秒）到达，恢复按钮状态并提示扫描结果"""
        self._scanning = False
        self._is_manual_scan = False  # 清除手动扫描标记
        self.scan_button.setEnabled(True)
        self.scan_button.setText("扫描")  # 去掉图标

        device_count = len(self.devices)
        logger.info(f"扫描结束，当前共发现 {device_count} 个设备")

        if device_count > 0:
            QMessageBox.information(
                self, '扫描完成',
                f'扫描完成，共发现 {device_count} 个设备'
            )
        else:
            QMessageBox.warning(
                self, '扫描超时',
                '15秒内未扫描到任何设备\n\n请检查：\n'
                '• 设备是否已上电并连接到同一网络\n'
                '• 网络连接是否正常'
            )
    
    def refresh_devices(self):
        if self.listener and self.zeroconf:
            threading.Thread(target=self._refresh_devices_background, daemon=True).start()
    
    def _refresh_devices_background(self):
        try:
            # logger.debug("后台刷新设备列表...")
            self.listener.refresh_all_devices(self.zeroconf, self.config.mdns_service_type)
        except Exception as e:
            logger.error(f"刷新设备列表失败: {e}")

    def _check_device_heartbeat(self):
        """HTTP心跳检测：定期检查设备是否在线"""
        import requests
        current_time = time.time()
        offline_devices = []

        for device in list(self.devices):
            sn = device.sn

            # 检查设备是否分配到窗口
            bound_panel = next((panel for panel in self.test_panels if panel.device and panel.device.sn == sn), None)

            # 检查设备是否正在执行测试（log_capture不为None表示正在测试）
            is_testing = bound_panel and bound_panel.log_capture is not None

            if is_testing:
                # 正在测试的设备，更新心跳时间（避免误判离线）
                self.device_last_seen[sn] = current_time
                logger.debug(f"设备 {sn} 正在测试，跳过心跳检测")
                continue

            last_seen = self.device_last_seen.get(sn, 0)

            # 新发现的设备，记录首次在线时间
            if last_seen == 0:
                self.device_last_seen[sn] = current_time
                continue

            # 检查是否超时
            if current_time - last_seen > self.heartbeat_timeout:
                # TCP连接探活（1秒超时，不解析响应内容）
                try:
                    url = f"http://{device.ip}:8080/hi"
                    response = requests.get(url, timeout=1)

                    # 只要HTTP请求成功（状态码200-399），就认为在线
                    if response.status_code < 400:
                        self.device_last_seen[sn] = current_time
                        logger.debug(f"设备 {sn} ({device.ip}) HTTP探活成功 (status={response.status_code})")
                    else:
                        # HTTP状态码错误，判定离线
                        offline_devices.append(sn)
                        logger.warning(f"设备 {sn} ({device.ip}) HTTP状态码错误 (status={response.status_code})，判定离线")
                except requests.exceptions.Timeout:
                    # 请求超时，判定离线
                    offline_devices.append(sn)
                    logger.warning(f"设备 {sn} ({device.ip}) HTTP探活超时，判定离线")
                except requests.exceptions.RequestException as e:
                    # 连接失败，判定离线
                    offline_devices.append(sn)
                    logger.warning(f"设备 {sn} ({device.ip}) HTTP探活异常: {type(e).__name__}，判定离线")

        # 移除离线设备
        for device_sn in offline_devices:
            self.device_removed_signal.emit(device_sn)

    def on_device_found(self, device: DeviceInfo):
        self.device_found_signal.emit(device)
    
    def on_device_removed(self, device_sn: str):
        self.device_removed_signal.emit(device_sn)
    
    def _on_device_found_main_thread(self, device: DeviceInfo):
        """设备发现后更新列表（手动分配窗口）"""
        if device.type != self.speaker_device_type:
            logger.debug(f"忽略非当前音箱模式设备: sn={device.sn}, type={device.type}")
            return

        # 优先根据IP地址去重（同一IP视为同一设备）
        existing_by_ip = next((d for d in self.devices if d.ip == device.ip), None)

        if existing_by_ip:
            # 同一IP的设备已存在，更新信息
            idx = self.devices.index(existing_by_ip)
            self.devices[idx] = device
            # logger.info(f"更新设备: {device.get_display_name()} ({device.ip}) [IP去重]")

            # 如果没有版本信息，查询版本（解决工具重启后版本丢失问题）
            # 但如果之前查询失败过，则不再重试
            # 手动扫描时强制重新查询所有设备的版本
            should_query = (device.sn not in self.device_versions and device.sn not in self.version_query_failed) or \
                           getattr(self, '_is_manual_scan', False)
            if should_query:
                logger.info(f"设备 {device.sn} 版本缺失，触发查询 [IP去重]")
                self._query_device_versions(device)
        else:
            # 检查SN是否重复（防止同一设备换IP）
            existing_by_sn = next((d for d in self.devices if d.sn == device.sn), None)
            if existing_by_sn:
                # 同一SN但不同IP，更新设备信息
                idx = self.devices.index(existing_by_sn)
                self.devices[idx] = device
                # logger.info(f"更新设备: {device.get_display_name()} ({device.ip}) [SN去重，IP已变更]")

                # 如果没有版本信息，查询版本（解决工具重启后版本丢失问题）
                # 但如果之前查询失败过，则不再重试
                # 手动扫描时强制重新查询所有设备的版本
                should_query = (device.sn not in self.device_versions and device.sn not in self.version_query_failed) or \
                               getattr(self, '_is_manual_scan', False)
                if should_query:
                    logger.info(f"设备 {device.sn} 版本缺失，触发查询 [SN去重]")
                    self._query_device_versions(device)
            else:
                # 新设备，添加到列表
                self.devices.append(device)
                logger.info(f"发现设备: {device.get_display_name()} ({device.ip})")

                # 不再自动分配设备到窗口，改为手动选择
                # self._auto_assign_device(device)

                # 后台查询 kernel/rootfs 版本（只查一次，结果缓存到 device_versions）
                # 但如果之前查询失败过，则不再重试
                # 手动扫描时强制重新查询所有设备的版本
                should_query = (device.sn not in self.device_versions and device.sn not in self.version_query_failed) or \
                               getattr(self, '_is_manual_scan', False)
                if should_query:
                    logger.info(f"发现新设备 {device.sn}，触发版本查询")
                    self._query_device_versions(device)

        # 更新设备最后在线时间（心跳检测）
        self.device_last_seen[device.sn] = time.time()

        # 更新设备列表显示
        self.update_device_list()

    def _on_device_removed_main_thread(self, device_sn: str):
        """设备离线后从窗口移除"""
        self.devices = [d for d in self.devices if d.sn != device_sn]
        # 清理版本缓存
        self.device_versions.pop(device_sn, None)
        # 清理心跳记录
        self.device_last_seen.pop(device_sn, None)
        # 清理校时记录（修复设备重新上下电后不会重新校时的问题）
        self.datetime_synced_devices.pop(device_sn, None)

        # 从mDNS listener缓存中移除，防止refresh时重新添加
        if self.listener:
            with self.listener._lock:
                # 查找并移除该设备的service name
                names_to_remove = []
                for name, cached_device in list(self.listener.discovered_devices.items()):
                    if cached_device.sn == device_sn:
                        names_to_remove.append(name)

                for name in names_to_remove:
                    self.listener.discovered_devices.pop(name, None)
                    logger.info(f"已从mDNS缓存移除设备: {name}")

        # 从窗口中移除设备
        for panel in self.test_panels:
            if panel.device and panel.device.sn == device_sn:
                panel.bind_device(None)
                logger.info(f"窗口 {panel.panel_id + 1} 设备已移除: {device_sn}")

        # 更新设备列表显示
        self.update_device_list()

        logger.info(f"设备离线: {device_sn}")

    def _query_all_device_versions(self):
        """工具启动后，主动查询所有设备版本"""
        logger.info(f"主动查询所有设备版本，共 {len(self.devices)} 台设备")
        for device in self.devices:
            # 跳过已有版本信息或查询失败过的设备
            if device.sn not in self.device_versions and device.sn not in self.version_query_failed:
                logger.debug(f"查询设备版本: {device.sn}")
                self._query_device_versions(device)

    def _query_device_versions(self, device: DeviceInfo):
        """后台 HTTP 查询设备 kernel/rootfs 版本，完成后刷新卡片列表"""
        # 防重：如果该设备正在查询版本，跳过
        if device.sn in self.version_querying:
            logger.debug(f"[版本查询] 设备 {device.sn} 已在查询队列中，跳过重复查询")
            return

        # 标记为正在查询
        self.version_querying.add(device.sn)
        logger.info(f"[版本查询] 启动查询线程: {device.sn}")

        def _run():
            try:
                # 跳过临时IP标识的设备
                if device.sn.startswith('IP-'):
                    logger.debug(f"设备 {device.sn} 使用临时标识，跳过版本查询")
                    return

                logger.info(f"[版本查询] 等待2秒后开始查询: {device.sn}")
                time.sleep(2)  # 等待设备HTTP服务就绪

                logger.info(f"[版本查询] 创建HTTP客户端: {device.sn} ({device.ip})")
                http_client = SpeakerHTTPClient(device.ip, port=8080)

                # 重试2次
                max_retries = 2
                versions = None
                for retry in range(max_retries):
                    logger.info(f"[版本查询] 尝试 {retry + 1}/{max_retries}: {device.sn}")
                    result = http_client.get_version()

                    if result and result.get('code') == 0:
                        # 解析HTTP响应，提取版本信息
                        data = result.get('data', {})
                        app_version = data.get('app_version', '')
                        ab_system = data.get('ab_system', {})
                        partitions = ab_system.get('partitions', {})

                        # 构建版本字典（格式与MQTT方式一致，去掉v前缀）
                        versions = {
                            'app_version': _strip_v(app_version),
                            'kernel': _strip_v(partitions.get('kernel', '')),
                            'rootfs_a': _strip_v(partitions.get('rootfs_a', '')),
                            'rootfs_b': _strip_v(partitions.get('rootfs_b', '')),
                        }
                        logger.info(f"[版本查询] 成功获取版本: {device.sn} -> {versions}")
                        break
                    else:
                        error_msg = result.get('message', '未知错误') if result else '请求失败'
                        logger.warning(f"[版本查询] 失败: {device.sn} - {error_msg}")

                    if retry < max_retries - 1:
                        logger.warning(f"[版本查询] 重试 {retry + 1}/{max_retries}: {device.sn}")
                        time.sleep(2)

                if versions:
                    self.device_versions[device.sn] = versions
                    logger.info(f"[版本查询] 版本已缓存: {device.sn} -> {versions}")
                    # 查询成功，从失败记录中移除（如果存在）
                    self.version_query_failed.discard(device.sn)
                    QTimer.singleShot(0, self.update_device_list)

                    # 室外音箱自动校时
                    if device.type == DEVICE_TYPE_OUTDOOR_SMART_HORN:
                        logger.info(f"[自动校时] 检测到室外音箱，开始自动校时: {device.sn}")
                        self._auto_sync_datetime(device)
                else:
                    logger.error(f"[版本查询] 所有重试失败: {device.sn}")
                    # 记录失败，避免后续重复查询
                    self.version_query_failed.add(device.sn)

            except Exception as e:
                logger.error(f"[版本查询] 异常: {device.sn} -> {e}")
                # 异常情况也记录到失败集合
                self.version_query_failed.add(device.sn)
            finally:
                # 无论成功还是失败，都要移除查询标记
                self.version_querying.discard(device.sn)
                logger.debug(f"[版本查询] 完成，移除查询标记: {device.sn}")

        threading.Thread(target=_run, daemon=True).start()

    def _auto_sync_datetime(self, device: DeviceInfo):
        """自动校时（仅室外音箱）- P0修复：添加防抖逻辑，避免重复校时"""
        def _run():
            try:
                # P0修复：检查是否最近已经校时过
                current_time = time.time()
                last_sync_time = self.datetime_synced_devices.get(device.sn, 0)
                time_since_last_sync = current_time - last_sync_time

                if time_since_last_sync < self.datetime_sync_interval:
                    remaining_time = int(self.datetime_sync_interval - time_since_last_sync)
                    logger.info(f"[自动校时] 跳过校时: {device.sn} (距离上次校时仅{int(time_since_last_sync)}秒，需等待{remaining_time}秒)")
                    return

                logger.info(f"[自动校时] 开始校时: {device.sn}")

                # 检查设备版本，只有 >= 1.0.0.9 的固件才支持校时
                versions = self.device_versions.get(device.sn, {})
                kernel_ver = versions.get('kernel', '')
                rootfs_ver = versions.get('rootfs', '')

                # 检查版本号
                def version_check(ver_str: str) -> bool:
                    """检查版本是否 >= 1.0.0.9"""
                    try:
                        # 去掉前缀 v/V
                        ver_str = ver_str.lstrip('vV')
                        parts = ver_str.split('.')
                        if len(parts) != 4:
                            return False
                        ver_tuple = tuple(int(p) for p in parts)
                        return ver_tuple >= (1, 0, 0, 9)
                    except:
                        return False

                if not version_check(kernel_ver) and not version_check(rootfs_ver):
                    logger.info(f"[自动校时] 跳过校时: {device.sn} (固件版本 < 1.0.0.9，不支持校时功能)")
                    logger.info(f"[自动校时] 当前版本: kernel={kernel_ver}, rootfs={rootfs_ver}")
                    return

                # 导入校时模块
                from ..network.speaker_mqtt_datetime_sync import sync_speaker_datetime

                # 根据broker模式决定使用的地址
                if self.broker_mode == 'local':
                    # 本地模式：动态获取本机IP
                    broker_ip = self._get_local_broker_ip()
                    logger.info(f"[自动校时] 使用本地Broker模式，连接到: {broker_ip}:{self.config.mqtt_port}")
                else:
                    # 远程模式：使用配置文件中的地址
                    broker_ip = self.config.mqtt_broker
                    logger.info(f"[自动校时] 使用远程Broker模式，连接到: {broker_ip}:{self.config.mqtt_port}")

                # 执行校时
                result = sync_speaker_datetime(
                    broker=broker_ip,
                    port=self.config.mqtt_port,
                    product_id=device.get_product_id(),
                    device_sn=device.sn,
                    device_model=device.model or ""
                )

                if result:
                    logger.info(f"[自动校时] 校时成功: {device.sn}")
                else:
                    logger.warning(f"[自动校时] 校时失败: {device.sn}")

                # P0修复：无论成功失败都记录时间戳，避免频繁重试造成MQTT连接风暴
                self.datetime_synced_devices[device.sn] = time.time()

            except Exception as e:
                logger.error(f"[自动校时] 校时异常: {device.sn} -> {e}")

        threading.Thread(target=_run, daemon=True).start()

    def update_device_list(self):
        """更新设备列表显示"""
        # 构建当前设备的 SN 集合
        current_device_sns = {device.sn for device in self.devices}
        existing_card_sns = {card.device.sn for card in self.device_cards}

        # 找出需要删除的卡片（设备已移除）
        cards_to_remove = [card for card in self.device_cards if card.device.sn not in current_device_sns]
        for card in cards_to_remove:
            self.device_layout.removeWidget(card)
            card.deleteLater()
            self.device_cards.remove(card)

        # 更新或添加设备卡片
        for i, device in enumerate(self.devices):
            # 查找是否已存在该设备的卡片
            existing_card = next((card for card in self.device_cards if card.device.sn == device.sn), None)

            if existing_card:
                # 卡片已存在，检查是否需要更新
                need_update = False

                # 检查设备对象引用是否改变（IP、fw_ver等）
                if existing_card.device.fw_ver != device.fw_ver:
                    existing_card.device = device
                    existing_card.update_app_version()
                    need_update = True
                elif existing_card.device.ip != device.ip:
                    existing_card.device = device
                    need_update = True

                # 检查版本信息是否改变
                current_versions = self.device_versions.get(device.sn)
                if current_versions and existing_card.versions != current_versions:
                    existing_card.update_versions(current_versions)
                    need_update = True

                # 更新选中状态
                if self.selected_device and self.selected_device.sn == device.sn:
                    if not existing_card.selected:
                        existing_card.set_selected(True)
            else:
                # 新设备，创建新卡片
                card = DeviceCard(device, versions=self.device_versions.get(device.sn))
                card.clicked.connect(self.on_device_card_clicked)
                card.delete_requested.connect(self.on_device_delete_requested)
                card.assign_window_requested.connect(self.on_device_assign_window_requested)
                # 连接复选框变化信号以更新批量测试按钮状态
                if card.checkbox:
                    card.checkbox.stateChanged.connect(lambda state, c=card: self.update_batch_buttons_state())
                if self.selected_device and self.selected_device.sn == device.sn:
                    card.set_selected(True)
                # 插入到正确的位置（保持设备顺序）
                self.device_layout.insertWidget(i, card)
                self.device_cards.insert(i, card)

        # 更新设备数量显示
        device_count = len(self.devices)
        if self.device_count_label.text() != f'设备数量: {device_count}':
            self.device_count_label.setText(f'设备数量: {device_count}')

        # 更新批量测试按钮状态
        self.update_batch_buttons_state()

    def on_device_card_clicked(self, device):
        """设备卡片点击事件"""
        self.selected_device = device
        for card in self.device_cards:
            card.set_selected(card.device.sn == device.sn)
        logger.info(f"已选择设备: {device.get_display_name()}")

    def on_device_assign_window_requested(self, device):
        """处理设备分配窗口请求"""
        from PyQt5.QtWidgets import QMenu

        # 创建窗口选择菜单
        menu = QMenu(self)

        # 添加所有窗口选项
        for i, panel in enumerate(self.test_panels):
            if panel.device:
                # 窗口已被占用
                action = menu.addAction(f"窗口 {i + 1} (已占用: {panel.device.get_display_name()})")
                action.triggered.connect(lambda checked, p=panel, d=device: self._assign_device_to_panel(p, d))
            else:
                # 窗口空闲
                action = menu.addAction(f"窗口 {i + 1} (空闲)")
                action.triggered.connect(lambda checked, p=panel, d=device: self._assign_device_to_panel(p, d))

        # 在鼠标位置显示菜单
        menu.exec_(QApplication.instance().desktop().cursor().pos())

    def _assign_device_to_panel(self, panel, device):
        """将设备分配到指定窗口"""
        # 如果窗口已有设备，询问是否替换
        if panel.device:
            from PyQt5.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self,
                '确认替换',
                f'窗口 {panel.panel_id + 1} 已被设备 {panel.device.get_display_name()} 占用，是否替换？',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                return

        # 分配设备到窗口
        panel.bind_device(device)
        logger.info(f"设备 {device.get_display_name()} 已手动分配到窗口 {panel.panel_id + 1}")

    def on_device_delete_requested(self, device):
        """处理设备删除请求"""
        # 检查设备是否正在被某个窗口使用
        in_use_panels = []
        for panel in self.test_panels:
            if panel.device and panel.device.sn == device.sn:
                in_use_panels.append(panel.panel_id + 1)
        
        # 如果设备正在使用，提示用户
        if in_use_panels:
            reply = QMessageBox.question(
                self,
                '确认删除',
                f'设备 {device.get_display_name()} 正在窗口 {", ".join(map(str, in_use_panels))} 使用中。\n是否要移除该设备？',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
            
            # 从使用中的窗口解绑
            for panel in self.test_panels:
                if panel.device and panel.device.sn == device.sn:
                    panel.bind_device(None)
                    logger.info(f"窗口 {panel.panel_id + 1} 已解绑设备: {device.sn}")
        
        # 从mDNS缓存中移除设备（防止定时刷新重新添加）
        if self.listener and hasattr(self.listener, 'discovered_devices'):
            # 查找并移除对应的设备
            device_name_to_remove = None
            for name, cached_device in list(self.listener.discovered_devices.items()):
                if cached_device.sn == device.sn:
                    device_name_to_remove = name
                    break
            
            if device_name_to_remove:
                self.listener.discovered_devices.pop(device_name_to_remove, None)
                logger.info(f"已从mDNS缓存移除设备: {device_name_to_remove}")
        
        # 从设备列表中移除
        self.devices = [d for d in self.devices if d.sn != device.sn]
        
        # 如果是选中的设备，清除选中状态
        if self.selected_device and self.selected_device.sn == device.sn:
            self.selected_device = None
        
        # 更新显示
        self.update_device_list()
        logger.info(f"已移除设备: {device.get_display_name()}")

    def show_test_results(self):
        """显示测试结果窗口"""
        dialog = TestResultsWindow(self)
        dialog.exec_()

    def show_test_logs(self):
        """显示测试日志查看窗口"""
        from .test_logs_window import TestLogsWindow
        dialog = TestLogsWindow(self)
        dialog.exec_()

    def open_printer_config(self):
        """打开打印机配置对话框"""
        from .printer_config_dialog import PrinterConfigDialog
        from ..utils.config import get_resource_path

        # 传入配置文件路径，而不是配置对象
        config_path = get_resource_path('config/config.yaml')

        dialog = PrinterConfigDialog(config_path, self)

        if dialog.exec_() == QDialog.Accepted:
            # 配置已保存，重新加载配置和打印机实例。
            # UniversalPrinter 在 __init__ 里把 printer_name/protocol/dpi 拷进实例属性，
            # 不重建实例的话改了配置也不生效。
            self.config.load_config()
            self.label_printer = LabelPrinter(self.config)
            logger.info(
                f"打印机配置已更新: {self.label_printer.printer_name}, "
                f"{self.label_printer.protocol}, {self.label_printer.dpi} DPI"
            )
            self.statusBar().showMessage('打印机配置已更新', 3000)

    def open_broker_config(self):
        """打开远程Broker配置对话框"""
        from .broker_config_dialog import BrokerConfigDialog
        from ..utils.config import get_resource_path

        config_path = get_resource_path('config/config.yaml')

        dialog = BrokerConfigDialog(config_path, self)
        if dialog.exec_() == QDialog.Accepted:
            # 配置保存成功后，重新加载配置
            self.config.load_config()
            logger.info(f"远程Broker配置已更新: {self.config.mqtt_broker}:{self.config.mqtt_port}")

    def closeEvent(self, event):
        """关闭窗口时清理资源"""
        logger.info("音箱测试窗口退出，开始清理资源...")

        # 1. 停止所有窗口的视频流
        try:
            for panel in self.test_panels:
                if panel.video_widget:
                    panel.video_widget.stop_stream()
            logger.info("所有视频流已停止")
        except Exception as e:
            logger.error(f"停止视频流失败: {e}")

        # 2. 停止定时器
        try:
            if self.device_refresh_timer:
                self.device_refresh_timer.stop()
                logger.info("设备刷新定时器已停止")
            if self.device_heartbeat_timer:
                self.device_heartbeat_timer.stop()
                logger.info("设备心跳定时器已停止")
            if self.scan_timeout_timer:
                self.scan_timeout_timer.stop()
                logger.info("扫描超时定时器已停止")
        except Exception as e:
            logger.error(f"停止定时器失败: {e}")

        # 3. 断开所有MQTT连接
        try:
            if hasattr(self, 'mqtt_clients') and self.mqtt_clients:
                for mqtt_client in self.mqtt_clients.values():
                    mqtt_client.disconnect()
                self.mqtt_clients.clear()
                logger.info("所有MQTT客户端已断开")
        except Exception as e:
            logger.error(f"断开MQTT连接失败: {e}")

        # 4. 停止mDNS服务发现（关键：按正确顺序清理）
        try:
            # 先停止 ServiceBrowser
            if hasattr(self, 'browser') and self.browser:
                self.browser.cancel()
                logger.info("mDNS ServiceBrowser 已停止")
                time.sleep(0.2)  # 给 ServiceBrowser 时间完成清理

            # 再注销 mDNS 服务
            if hasattr(self, 'master_mdns') and self.master_mdns:
                self.master_mdns.unregister()
                logger.info("mDNS 服务已注销")
                time.sleep(0.2)  # 给注销操作时间完成

            # 最后关闭 Zeroconf
            if hasattr(self, 'zeroconf') and self.zeroconf:
                self.zeroconf.close()
                logger.info("Zeroconf 已关闭")
                time.sleep(0.3)  # 确保UDP端口完全释放
        except Exception as e:
            logger.error(f"清理Zeroconf资源失败: {e}")

        # 5. 停止HTTP配置服务
        if hasattr(self, 'config_server') and self.config_server:
            try:
                self.config_server.stop()
                logger.info("HTTP配置服务已停止")
            except Exception as e:
                logger.error(f"停止HTTP配置服务失败: {e}")

        # 6. 停止固件HTTP服务
        if hasattr(self, 'firmware_server') and self.firmware_server:
            try:
                self.firmware_server.stop()
                logger.info("固件HTTP服务已停止")
            except Exception as e:
                logger.error(f"停止固件HTTP服务失败: {e}")

        # 7. 停止MQTT Broker
        if hasattr(self, 'mqtt_broker') and self.mqtt_broker:
            try:
                self.mqtt_broker.stop()
                logger.info("MQTT Broker已停止")
            except Exception as e:
                logger.error(f"停止MQTT Broker失败: {e}")

        # 8. 关闭串口资源
        try:
            from ..utils.serial_manager import SerialManager
            serial_manager = SerialManager()
            serial_manager.close_all()
            logger.info("串口资源已关闭")
        except Exception as e:
            logger.error(f"关闭串口资源失败: {e}")

        logger.info("音箱测试窗口所有资源清理完成")
        event.accept()
