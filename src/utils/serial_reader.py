import serial
import serial.tools.list_ports
import threading
import time
from .logger import logger


class SerialReader:
    def __init__(self, port=None, baudrate=9600, timeout=2):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial = None
        self._listening = False
        self._listen_thread = None
        self.received_data = []

    def find_port(self):
        """自动查找可用串口"""
        ports = serial.tools.list_ports.comports()
        if ports:
            self.port = ports[0].device
            logger.info(f"找到串口: {self.port}")
            return self.port
        return None

    def open(self):
        """打开串口"""
        if not self.port:
            self.find_port()
        if self.port:
            try:
                self.serial = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
                logger.info(f"串口已打开: {self.port}")
                return True
            except serial.SerialException as e:
                if "PermissionError" in str(e) or "拒绝访问" in str(e):
                    logger.warning(f"串口 {self.port} 被占用或权限不足，尝试查找其他串口")
                    # 尝试其他串口
                    ports = serial.tools.list_ports.comports()
                    for port_info in ports:
                        if port_info.device != self.port:
                            try:
                                self.serial = serial.Serial(port_info.device, self.baudrate, timeout=self.timeout)
                                self.port = port_info.device
                                logger.info(f"使用备用串口: {self.port}")
                                return True
                            except:
                                continue
                logger.error(f"打开串口失败: {e}")
            except Exception as e:
                logger.error(f"打开串口失败: {e}")
        return False

    def read_signal(self, timeout=2):
        """读取信号，持续监听直到接收到数据或超时"""
        if not self.serial or not self.serial.is_open:
            if not self.open():
                return None

        try:
            import time
            self.serial.reset_input_buffer()
            logger.info(f"开始监听串口，超时时间: {timeout}秒")

            start_time = time.time()
            while time.time() - start_time < timeout:
                waiting = self.serial.in_waiting
                if waiting > 0:
                    logger.info(f"检测到数据: {waiting} 字节")
                    data = self.serial.read(waiting)
                    if data:
                        signal = data.hex()
                        logger.info(f"接收到红外信号: {signal}")
                        return signal
                time.sleep(0.1)  # 每100ms检查一次

            logger.warning(f"超时 {timeout}秒，未接收到红外信号数据")
        except Exception as e:
            logger.error(f"读取串口数据失败: {e}")
        return None

    def start_listening(self):
        """启动后台监听线程"""
        if not self.serial or not self.serial.is_open:
            if not self.open():
                return False
        self.received_data = []
        self._listening = True
        self.serial.reset_input_buffer()
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listen_thread.start()
        logger.info("串口后台监听已启动")
        return True

    def _listen_loop(self):
        """后台监听循环"""
        while self._listening:
            try:
                if self.serial and self.serial.is_open and self.serial.in_waiting > 0:
                    data = self.serial.read(self.serial.in_waiting)
                    if data:
                        signal = data.hex()
                        logger.info(f"后台接收到红外信号: {signal}")
                        self.received_data.append(signal)
            except Exception as e:
                logger.error(f"后台监听异常: {e}")
                break
            time.sleep(0.1)

    def stop_listening(self):
        """停止后台监听"""
        if not self._listening and not self._listen_thread:
            return self.received_data
        self._listening = False
        if self._listen_thread:
            self._listen_thread.join(timeout=2)
            self._listen_thread = None
        logger.info(f"串口后台监听已停止，接收到 {len(self.received_data)} 条数据")
        return self.received_data

    def close(self):
        """关闭串口"""
        self.stop_listening()
        if self.serial and self.serial.is_open:
            self.serial.close()
            logger.info("串口已关闭")
