"""
扫码枪输入监听器 - PyQt5版本
基于PyQt5事件过滤器实现，无需额外依赖
适用于Python 3.8+
"""
import time
from PyQt5.QtCore import QObject, QEvent, pyqtSignal, Qt
from PyQt5.QtWidgets import QApplication
from src.utils.logger import logger


class ScannerInputListener(QObject):
    """
    扫码枪输入监听器
    通过PyQt5事件过滤器实现键盘监听
    """

    # 信号：扫码完成
    scanned = pyqtSignal(str)

    def __init__(self, callback=None, timeout=0.05, parent=None):
        """
        初始化监听器

        Args:
            callback: 扫码完成后的回调函数 callback(qrcode_data: str)
            timeout: 字符间隔阈值（秒）。判定依据是相邻字符的最大间隔，而非整串
                总耗时——总耗时随载荷长度线性增长，长二维码必然超时被误判。
                实测扫码枪约 8ms/字符，人手打字约 175ms/字符，取 0.05s 有 6 倍余量。
            parent: 父对象（QRCodePrintWindow）
        """
        super().__init__(parent)
        self.callback = callback
        self.timeout = timeout
        self.parent_window = parent  # 保存父窗口引用
        self.buffer = []
        self.first_input_time = None
        self.last_input_time = None
        self.max_gap = 0.0  # 本串输入中相邻字符的最大间隔
        self.is_listening = False

        # 如果提供了回调函数，连接信号
        if self.callback:
            self.scanned.connect(self._on_scanned)

    def _reset_buffer(self):
        """清空累积状态"""
        self.buffer.clear()
        self.first_input_time = None
        self.last_input_time = None
        self.max_gap = 0.0

    def start(self):
        """启动监听"""
        if not self.is_listening:
            # 在应用级别安装事件过滤器
            app = QApplication.instance()
            if app:
                app.installEventFilter(self)
                self.is_listening = True
                logger.info("扫码枪监听器已启动（PyQt5模式）")
            else:
                logger.error("无法启动监听器：QApplication未初始化")

    def stop(self):
        """停止监听"""
        if self.is_listening:
            app = QApplication.instance()
            if app:
                app.removeEventFilter(self)
            self.is_listening = False
            logger.info("扫码枪监听器已停止")

    def eventFilter(self, obj, event):
        """
        事件过滤器
        捕获所有键盘事件
        """
        # 安全检查：只处理父窗口（扫码打印窗口）内的事件
        if self.parent_window is not None:
            # 检查事件来源是否属于父窗口
            widget = obj
            while widget is not None:
                if widget == self.parent_window:
                    break
                widget = widget.parent() if hasattr(widget, 'parent') else None

            # 如果事件不来自父窗口，直接放行
            if widget is None:
                return super().eventFilter(obj, event)

        if event.type() == QEvent.KeyPress:
            key = event.key()

            # 记录当前时间
            current_time = time.time()

            # 使用 event.text() 获取实际输入的字符
            # 扫码枪通常输出纯ASCII字符（数字、字母、常见符号）
            # 通过 isascii() 过滤掉输入法产生的表情和中文字符
            text = event.text()

            # 只接受ASCII可打印字符（排除控制字符和非ASCII字符）
            if text and len(text) == 1 and text.isascii() and text.isprintable() and text not in ('\r', '\n'):
                if not self.buffer:
                    self.first_input_time = current_time
                else:
                    # 记录相邻字符间隔，取本串最大值
                    gap = current_time - self.last_input_time
                    if gap > self.max_gap:
                        self.max_gap = gap
                self.last_input_time = current_time
                self.buffer.append(text)
                logger.debug(f"接收字符: {repr(text)} (key={key})")
                # 如果正在累积扫码枪输入，阻止事件传播到输入框（避免输入法转换）
                return True

            # 处理回车键
            elif key in (Qt.Key_Return, Qt.Key_Enter):
                if self.buffer and self.first_input_time is not None:
                    # 整串耗时仅用于日志诊断，不作为判定依据
                    input_duration = current_time - self.first_input_time

                    # 按相邻字符最大间隔判定：扫码枪逐字符间隔极小，
                    # 且该间隔与载荷长度无关
                    if self.max_gap < self.timeout:
                        scanned_data = ''.join(self.buffer)
                        logger.info(
                            f"识别到扫码枪输入: {scanned_data} "
                            f"(最大间隔: {self.max_gap * 1000:.1f}ms, "
                            f"{len(scanned_data)}字符, 总耗时: {input_duration:.3f}s)"
                        )

                        # 清空缓冲区
                        self._reset_buffer()

                        # 发射信号
                        self.scanned.emit(scanned_data)

                        # 阻止回车键传播（避免触发手动输入处理）
                        return True
                    else:
                        logger.debug(
                            f"手动输入（最大间隔{self.max_gap * 1000:.1f}ms，"
                            f"{len(self.buffer)}字符，总耗时{input_duration:.3f}s）：忽略"
                        )

                # 清空缓冲区
                self._reset_buffer()

        # 继续传递事件
        return super().eventFilter(obj, event)

    def _on_scanned(self, qrcode_data):
        """信号处理：调用回调函数"""
        if self.callback:
            try:
                self.callback(qrcode_data)
            except Exception as e:
                logger.error(f"扫码回调执行失败: {e}")

    def is_running(self):
        """检查监听器是否运行中"""
        return self.is_listening
