import threading
import time

from PyQt5.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                             QMessageBox, QFileDialog, QSplitter, QDialog,
                             QLabel, QAction)
from PyQt5.QtCore import Qt, pyqtSignal, QThread, QTimer
from PyQt5.QtGui import QFont, QIcon
from zeroconf import Zeroconf, ServiceBrowser
import uuid

from .device_list_panel import DeviceListPanel
from .device_detail_panel import DeviceDetailPanel
from .test_record_panel import TestRecordPanel
from ..network.mdns_discovery import DeviceInfo, DeviceDiscoveryListener, MasterMdnsService
from ..network.device_info import DEVICE_TYPE_SMART_DOOR, DEVICE_TYPE_MAP
from ..network.mqtt_client import MQTTClient
from ..core.test_engine import TestEngine
from ..core.test_result import TestStatus
from src.hardware.universal_printer import UniversalPrinter
from ..network.http_server import ConfigServer
from ..network.mqtt_broker import MQTTBrokerManager
from ..core.protocol import DiscoverMessage, QueryStatusMessage
from ..core.crypto import encrypt_mqtt_credential, mask_credential
from ..network.tftp_server import TFTPServer
from ..data.test_record_storage import TestRecordStorage
from ..utils.config import Config
from ..utils.logger import logger


def show_message(parent, title, text):
    """显示无图标的消息框"""
    msg = QMessageBox(parent)
    msg.setWindowTitle(title)
    msg.setText(text)
    msg.setIcon(QMessageBox.NoIcon)
    msg.exec_()


class CountdownDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('操作提示')
        self.setModal(True)
        self.setFixedSize(380, 160)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("QDialog { background-color: #fff; } QLabel { background: transparent; }")

        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        self.message_label = QLabel()
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setFont(QFont('Microsoft YaHei', 13, QFont.Bold))
        self.message_label.setStyleSheet('color: #1a1a1a;')
        layout.addWidget(self.message_label)

        self.countdown_label = QLabel()
        self.countdown_label.setAlignment(Qt.AlignCenter)
        self.countdown_label.setFont(QFont('Microsoft YaHei', 22, QFont.Bold))
        self.countdown_label.setStyleSheet('color: #1976D2;')
        layout.addWidget(self.countdown_label)

        self.setLayout(layout)

    def update_message(self, message: str, countdown: int = None):
        self.message_label.setText(message)
        self.countdown_label.setText(f'{countdown} 秒' if countdown is not None else '')


class TestThread(QThread):
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(object)
    countdown_signal = pyqtSignal(str, int)
    test_item_signal = pyqtSignal(str, str, str)

    def __init__(self, test_engine):
        super().__init__()
        self.test_engine = test_engine

    def _report_callback(self, event_type: str, countdown: int):
        if event_type == "emergency_countdown":
            self.countdown_signal.emit("请按应急开关", countdown)
        elif event_type == "pairing_prepare":
            # GPIO8 准备期，此时按键设备采集不到，别让操作员白按
            self.countdown_signal.emit("配对准备中  请勿按键", countdown)
        elif event_type == "pairing_countdown":
            self.countdown_signal.emit("请连续按遥控器配对键 3 次", countdown)
        elif event_type == "open_countdown":
            self.countdown_signal.emit("配对完成  请按遥控器开门", countdown)
        elif event_type == "hide_dialog":
            self.countdown_signal.emit("__hide__", 0)

    def run(self):
        self.test_engine.set_progress_callback(lambda msg: self.progress_signal.emit(msg))
        self.test_engine.set_test_item_callback(
            lambda name, status, msg: self.test_item_signal.emit(name, status, msg))
        result = self.test_engine.run_full_test(report_callback=self._report_callback)
        self.finished_signal.emit(result)


class OTAThread(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool)

    def __init__(self, test_engine, tftp_server_ip, tftp_port, firmware_name, file_size):
        super().__init__()
        self.test_engine = test_engine
        self.tftp_server_ip = tftp_server_ip
        self.tftp_port = tftp_port
        self.firmware_name = firmware_name
        self.file_size = file_size

    def run(self):
        try:
            success = self.test_engine.test_ota_upgrade(
                self.tftp_server_ip,
                self.tftp_port,
                self.firmware_name,
                self.file_size
            )
            if success:
                self.log_signal.emit("✅ OTA升级指令已接受，设备正在下载固件")
            else:
                self.log_signal.emit("❌ OTA升级失败")
            self.finished_signal.emit(success)
        except Exception as e:
            self.log_signal.emit(f"❌ OTA升级异常: {str(e)}")
            self.finished_signal.emit(False)


class SingleTestThread(QThread):
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool)
    countdown_signal = pyqtSignal(str, int)

    def __init__(self, test_engine, test_func):
        super().__init__()
        self.test_engine = test_engine
        self.test_func = test_func

    def _report_callback(self, event_type: str, countdown: int):
        if event_type == "emergency_countdown":
            self.countdown_signal.emit("请按应急开关", countdown)
        elif event_type == "pairing_prepare":
            # GPIO8 准备期，此时按键设备采集不到，别让操作员白按
            self.countdown_signal.emit("配对准备中  请勿按键", countdown)
        elif event_type == "pairing_countdown":
            self.countdown_signal.emit("请连续按遥控器配对键 3 次", countdown)
        elif event_type == "open_countdown":
            self.countdown_signal.emit("配对完成  请按遥控器开门", countdown)
        elif event_type == "hide_dialog":
            self.countdown_signal.emit("__hide__", 0)

    def run(self):
        self.test_engine.set_progress_callback(lambda msg: self.progress_signal.emit(msg))
        try:
            result = self.test_func()
            self.finished_signal.emit(result)
        except Exception as e:
            self.progress_signal.emit(f"❌ 测试异常: {str(e)}")
            self.finished_signal.emit(False)


class MainWindow(QMainWindow):
    device_found_signal = pyqtSignal(object)
    device_validated_signal = pyqtSignal(object)
    device_removed_signal = pyqtSignal(str)
    ota_progress_signal = pyqtSignal(str, int, int, int)
    ota_log_signal = pyqtSignal(str, str)
    network_scan_done_signal = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.devices = {}                       # sn -> DeviceInfo
        self.selected_device_sn = None
        self.mqtt_client = None
        self.test_engine = None
        self.label_printer = UniversalPrinter(self.config)
        self.test_record_storage = TestRecordStorage()
        self.zeroconf = None
        self.browser = None
        self.master_mdns = None
        self.config_server = None
        self.http_thread = None
        self.mqtt_broker = None
        self.broker_thread = None
        self.device_test_status = {}
        self.device_mqtt_clients = {}
        self.device_test_threads = {}
        self.broadcast_mqtt_client = None
        self.device_last_heartbeat = {}
        self.device_heartbeat_miss_count = {}
        self.heartbeat_timeout = 30
        self.heartbeat_max_miss = 3
        self.tftp_server = None
        self.device_ota_progress = {}
        self.device_ota_in_progress = set()
        self.device_test_in_progress = set()
        self.current_firmware_path = None
        self.current_firmware_name = None
        self.device_ip_to_sn = {}
        self.countdown_dialog = None
        self.listener = None
        self.pending_device_validations = set()
        self.pending_discovered_devices = {}
        self.pending_device_validation_lock = threading.Lock()
        self.discovery_generation = 0

        self.device_found_signal.connect(self._on_device_found_main_thread)
        self.device_validated_signal.connect(self._add_or_update_device_main_thread)
        self.device_removed_signal.connect(self._on_device_removed_main_thread)
        self.ota_progress_signal.connect(self._on_ota_progress_update)
        self.ota_log_signal.connect(self._emit_ota_log)
        self.network_scan_done_signal.connect(self._apply_network_scan_result)

        self.init_ui()
        self.init_menu()
        self.connect_signals()

        # 优化：并行启动网络服务，减少初始化时间
        self.start_mqtt_broker()
        self.start_http_server()
        self.start_tftp_server()

        # 等待MQTT Broker和HTTP服务就绪后再启动设备发现
        time.sleep(0.3)  # 短暂等待服务启动

        self.start_device_discovery()
        self.init_broadcast_mqtt()
        self.start_heartbeat_monitor()

    def init_ui(self):
        self.setWindowTitle(f'智能设备产测工具 - 门控模式 v{self.config.app_version}')
        self.setGeometry(100, 100, 1400, 800)

        # 设置窗口图标
        import os
        import sys

        # 获取资源文件路径（兼容打包后的环境）
        if getattr(sys, 'frozen', False):
            # 打包后的环境
            base_path = sys._MEIPASS
            icon_path = os.path.join(base_path, 'vdian.ico')
        else:
            # 开发环境
            icon_path = os.path.join(os.path.dirname(__file__), 'icon', 'vdian.ico')

        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout()

        splitter = QSplitter(Qt.Horizontal)

        self.device_list_panel = DeviceListPanel()
        self.device_list_panel.setMinimumWidth(300)
        splitter.addWidget(self.device_list_panel)

        self.device_detail_panel = DeviceDetailPanel()
        splitter.addWidget(self.device_detail_panel)

        splitter.setStretchFactor(0, 35)
        splitter.setStretchFactor(1, 65)
        splitter.setSizes([490, 910])

        main_layout.addWidget(splitter)
        central_widget.setLayout(main_layout)

        self.statusBar().showMessage("就绪")

    def init_menu(self):
        menubar = self.menuBar()
        menubar.setNativeMenuBar(False)  # 在窗口内显示菜单栏

        # 添加菜单栏样式（避免深色背景下文字不可见）
        menubar.setStyleSheet("""
            QMenuBar {
                background-color: #4A5F7A;
                color: white;
                font-size: 13px;
                padding: 2px;
            }
            QMenuBar::item {
                background-color: transparent;
                padding: 6px 12px;
            }
            QMenuBar::item:selected {
                background-color: #5a6f8a;
                border-radius: 4px;
            }
            QMenu {
                background-color: #4A5F7A;
                color: white;
                border: 1px solid #5a6f8a;
            }
            QMenu::item {
                padding: 6px 20px;
            }
            QMenu::item:selected {
                background-color: #3498db;
            }
        """)

        # 工具菜单
        tools_menu = menubar.addMenu('工具')

        view_records_action = QAction('查看测试记录', self)
        view_records_action.triggered.connect(self.open_test_records)
        tools_menu.addAction(view_records_action)

        # 设置菜单
        settings_menu = menubar.addMenu('设置')

        printer_config_action = QAction('🖨️ 打印机配置', self)
        printer_config_action.triggered.connect(self.open_printer_config)
        settings_menu.addAction(printer_config_action)

    def open_test_records(self, sn: str = ''):
        """打开测试记录窗口"""
        dialog = QDialog(self)
        dialog.setWindowTitle('测试记录')
        dialog.setMinimumSize(1200, 700)

        layout = QVBoxLayout()
        record_panel = TestRecordPanel()
        if sn:
            record_panel.sn_input.setText(sn)
            record_panel.on_search()
        layout.addWidget(record_panel)
        dialog.setLayout(layout)

        dialog.exec_()

    def open_printer_config(self):
        """打开打印机配置对话框"""
        from .printer_config_dialog import PrinterConfigDialog
        from ..utils.paths import get_app_dir
        import os

        config_path = os.path.join(get_app_dir(), 'config', 'config.yaml')
        dialog = PrinterConfigDialog(config_path, self)

        if dialog.exec_() == QDialog.Accepted:
            # 配置已保存，重新加载配置和打印机实例
            self.config.load_config()
            self.label_printer = UniversalPrinter(self.config)
            self.statusBar().showMessage('打印机配置已更新', 3000)

    def connect_signals(self):
        # Device list signals
        self.device_list_panel.device_selected.connect(self._on_device_selected)
        self.device_list_panel.device_deleted.connect(self._on_device_deleted)
        self.device_list_panel.refresh_btn.clicked.connect(self.refresh_devices)
        self.device_list_panel.scan_btn.clicked.connect(self.start_network_scan)

        # Device detail signals
        self.device_detail_panel.auto_test_clicked.connect(self._on_auto_test)
        self.device_detail_panel.test_clicked.connect(self._on_test_item)
        self.device_detail_panel.upload_firmware_clicked.connect(self.upload_firmware)
        self.device_detail_panel.ota_clicked.connect(self.start_ota_upgrade)
        self.device_detail_panel.print_label_clicked.connect(self.print_label)
        self.device_detail_panel.reset_config_clicked.connect(self.start_reset_config)
        self.device_detail_panel.view_records_clicked.connect(self.open_test_records)

    # ---------------------------------------------------------------
    # Device selection
    # ---------------------------------------------------------------
    def _on_device_selected(self, sn: str):
        device = self.devices.get(sn)
        if not device:
            return

        self.selected_device_sn = sn
        self.device_detail_panel.set_device(device.sn, device.ip, device.model)

        # Restore previous test status
        status = self.device_test_status.get(sn, '')
        if status:
            if '通过' in status:
                self.device_detail_panel.update_auto_test_status("passed")
            elif '失败' in status:
                self.device_detail_panel.update_auto_test_status("failed")

        self.statusBar().showMessage(f"已选择设备: {sn} ({device.ip})")
        logger.info(f"选中设备: {sn} ({device.ip})")

    def _on_device_deleted(self, sn: str):
        reply = QMessageBox.question(
            self, '确认删除',
            f'确定要删除设备 {sn} 吗？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.device_removed_signal.emit(sn)

    def _begin_device_test(self, device, label: str) -> bool:
        if self.device_test_in_progress:
            active = ", ".join(sorted(self.device_test_in_progress))
            QMessageBox.warning(self, '测试进行中', f'已有设备正在测试: {active}\n请等待当前测试完成后再启动{label}。')
            return False
        self.device_test_in_progress.add(device.sn)
        self.device_detail_panel.set_testing(True)
        return True

    def _finish_device_test(self, device_sn: str):
        self.device_test_in_progress.discard(device_sn)
        self.device_detail_panel.set_testing(False)

    # ---------------------------------------------------------------
    # Auto test (full test)
    # ---------------------------------------------------------------
    def _on_auto_test(self, sn: str):
        device = self.devices.get(sn)
        if not device:
            return
        if not self._begin_device_test(device, "一键测试"):
            return

        self.device_detail_panel.update_auto_test_status("testing")
        self.device_detail_panel.clear_results()
        self.device_detail_panel.append_log(f"开始测试设备: {device.sn}")

        try:
            mqtt_client = self._ensure_mqtt_client(device)
            if not mqtt_client:
                self._finish_device_test(device.sn)
                return

            test_engine = TestEngine(mqtt_client, self.config, device.hw_ver)
            self.countdown_dialog = CountdownDialog(self)

            test_thread = TestThread(test_engine)
            test_thread.progress_signal.connect(self.device_detail_panel.append_log)
            test_thread.countdown_signal.connect(self._on_countdown_update)
            test_thread.test_item_signal.connect(self._on_test_item_update)
            test_thread.finished_signal.connect(lambda result: self._on_test_finished(result, device))
            test_thread.start()

            self.device_test_threads[device.sn] = test_thread

        except Exception as e:
            self._finish_device_test(device.sn)
            QMessageBox.critical(self, '错误', f'测试启动失败: {str(e)}')

    def _on_countdown_update(self, message: str, countdown: int):
        if message == "__hide__":
            if self.countdown_dialog:
                self.countdown_dialog.hide()
            return
        if self.countdown_dialog:
            self.countdown_dialog.update_message(message, countdown)
            if not self.countdown_dialog.isVisible():
                self.countdown_dialog.show()

    def _on_test_item_update(self, test_name: str, status: str, message: str):
        self.device_detail_panel.update_test_result(test_name, status, message)

    def _on_test_finished(self, result, device):
        if self.countdown_dialog:
            self.countdown_dialog.close()
            self.countdown_dialog = None

        self.device_detail_panel.set_testing(False)
        self._finish_device_test(device.sn)
        result_engine = getattr(self.device_test_threads.get(device.sn), 'test_engine', None)
        if result_engine:
            result_engine.close()
        self.device_test_threads.pop(device.sn, None)

        # 保存测试记录：拆分为各子测试类型分别保存
        import uuid
        from datetime import datetime
        for sub in result.sub_results:
            record = {
                'id': str(uuid.uuid4()),
                'device_sn': device.sn,
                'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'test_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'test_type': sub['test_type'],
                'status': sub['status'],
                'duration': sub['duration'],
                'steps': [{'name': s['name'], 'success': s['success'], 'message': s['message']} for s in sub['steps']]
            }
            self.test_record_storage.upsert_record(record)

        if result.status == TestStatus.PASSED:
            self.device_detail_panel.append_log("✅ 测试通过！")
            self.device_detail_panel.update_auto_test_status("passed")
            self.device_test_status[device.sn] = '✅ 通过'
            self.device_list_panel.update_device_status(device.sn, '✅ 通过')
        else:
            self.device_detail_panel.append_log(f"❌ 测试失败: {result.error_message}")
            self.device_detail_panel.update_auto_test_status("failed")
            status_text = f'❌ 失败'
            self.device_test_status[device.sn] = status_text
            self.device_list_panel.update_device_status(device.sn, status_text)

    # ---------------------------------------------------------------
    # Individual test items
    # ---------------------------------------------------------------
    def _on_test_item(self, test_name: str, sn: str):
        device = self.devices.get(sn)
        if not device:
            return

        if test_name == "burn_mac":
            self.start_burn_mac(device)
        elif test_name == "remote_pairing":
            self.start_remote_pairing(device)
        elif test_name == "emergency_switch":
            self.start_emergency_switch_test(device)
        elif test_name in ("wifi_discover", "ble_discover", "sle_discover"):
            self.start_wireless_discover(device, test_name)

    def start_wireless_discover(self, device, test_name: str):
        if not self._begin_device_test(device, "无线检测"):
            return
        label_map = {
            "wifi_discover": "WiFi检测",
            "ble_discover": "BLE检测",
            "sle_discover": "SLE检测",
        }
        label = label_map[test_name]
        self.device_detail_panel.update_test_result(test_name, "testing")
        self.device_detail_panel.append_log(f"开始{label}: {device.sn}")

        try:
            mqtt_client = self._ensure_mqtt_client(device)
            if not mqtt_client:
                self._finish_device_test(device.sn)
                return

            test_engine = TestEngine(mqtt_client, self.config, device.hw_ver)
            func_map = {
                "wifi_discover": test_engine.test_wifi_discover,
                "ble_discover": test_engine.test_ble_discover,
                "sle_discover": test_engine.test_sle_discover,
            }
            start_time = time.time()
            thread = SingleTestThread(test_engine, func_map[test_name])
            thread.progress_signal.connect(self.device_detail_panel.append_log)
            thread.finished_signal.connect(
                lambda ok, n=test_name, l=label, sn=device.sn, st=start_time, te=test_engine:
                self._on_wireless_discover_finished(ok, n, l, sn, time.time() - st, te))
            thread.start()
            self._single_test_thread = thread

        except Exception as e:
            self._finish_device_test(device.sn)
            self.device_detail_panel.update_test_result(test_name, "failed")
            QMessageBox.critical(self, '错误', f'{label}失败: {str(e)}')

    def _on_wireless_discover_finished(self, success: bool, test_name: str, label: str, sn: str, duration: float, test_engine):
        status = "passed" if success else "failed"
        self.device_detail_panel.update_test_result(test_name, status)
        label_map = {
            "wifi_discover": "WiFi检测",
            "ble_discover": "BLE检测",
            "sle_discover": "SLE检测",
        }
        self.test_record_storage.upsert_record({
            'device_sn': sn,
            'test_type': label_map.get(test_name, test_name),
            'status': status,
            'duration': round(duration, 2),
            'steps': [{'name': s['name'], 'success': s['success'], 'message': s['message']} for s in test_engine.result.steps] if test_engine.result else [],
        })
        if success:
            self.device_detail_panel.append_log(f"✅ {label}通过")
        else:
            self.device_detail_panel.append_log(f"❌ {label}失败")
        test_engine.close()
        self._finish_device_test(sn)

    def start_burn_mac(self, device):
        if not self._begin_device_test(device, "烧写MAC"):
            return
        self.device_detail_panel.update_test_result("burn_mac", "testing")
        self.device_detail_panel.append_log(f"开始烧写MAC地址: {device.sn}")

        try:
            mqtt_client = self._ensure_mqtt_client(device)
            if not mqtt_client:
                return

            test_engine = TestEngine(mqtt_client, self.config, device.hw_ver)

            start_time = time.time()
            success, message = test_engine.burn_mac_addresses(
                device.sn,
                lambda msg: self.device_detail_panel.append_log(msg)
            )
            duration = round(time.time() - start_time, 2)

            self.test_record_storage.upsert_record({
                'device_sn': device.sn,
                'test_type': '烧写MAC',
                'status': 'passed' if success else 'failed',
                'duration': duration,
                'steps': [],
            })

            if success:
                self.device_detail_panel.append_log(f"✅ {message}")
                self.device_detail_panel.update_test_result("burn_mac", "passed", message)
                show_message(self, '成功', message)
            else:
                self.device_detail_panel.append_log(f"❌ {message}")
                self.device_detail_panel.update_test_result("burn_mac", "failed", message)

        except Exception as e:
            self.device_detail_panel.append_log(f"❌ 烧写MAC失败: {str(e)}")
            self.device_detail_panel.update_test_result("burn_mac", "failed")
            QMessageBox.critical(self, '错误', f'烧写MAC失败: {str(e)}')
        finally:
            try:
                test_engine.close()
            except Exception:
                pass
            self._finish_device_test(device.sn)

    def start_remote_pairing(self, device):
        if not self._begin_device_test(device, "遥控器配对"):
            return
        self.device_detail_panel.update_test_result("remote_pairing", "testing")
        self.device_detail_panel.append_log(f"开始遥控器配对: {device.sn}")

        try:
            mqtt_client = self._ensure_mqtt_client(device)
            if not mqtt_client:
                self._finish_device_test(device.sn)
                return

            test_engine = TestEngine(mqtt_client, self.config, device.hw_ver)

            self.countdown_dialog = CountdownDialog(self)
            start_time = time.time()
            thread = SingleTestThread(test_engine, None)
            thread.test_func = lambda: test_engine.test_remote_pairing(
                pairing_duration=2000, open_timeout=10, report_callback=thread._report_callback)
            thread.progress_signal.connect(self.device_detail_panel.append_log)
            thread.countdown_signal.connect(self._on_countdown_update)
            thread.finished_signal.connect(
                lambda success, st=start_time, te=test_engine, sn=device.sn:
                self._on_remote_pairing_finished(success, time.time() - st, sn, te)
            )
            thread.start()
            self._single_test_thread = thread

        except Exception as e:
            self._finish_device_test(device.sn)
            self.device_detail_panel.append_log(f"❌ 遥控器配对失败: {str(e)}")
            self.device_detail_panel.update_test_result("remote_pairing", "failed")
            QMessageBox.critical(self, '错误', f'遥控器配对失败: {str(e)}')

    def _on_remote_pairing_finished(self, success: bool, duration: float, sn: str, test_engine):
        if self.countdown_dialog:
            self.countdown_dialog.close()
            self.countdown_dialog = None
        import uuid
        from datetime import datetime

        # 保存测试记录
        steps = [{'name': s['name'], 'success': s['success'], 'message': s['message']} for s in test_engine.result.steps]
        record = {
            'id': str(uuid.uuid4()),
            'device_sn': sn,
            'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'test_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'test_type': '遥控器配对测试',
            'status': 'passed' if success else 'failed',
            'duration': round(duration, 2),
            'steps': steps
        }
        self.test_record_storage.upsert_record(record)

        if success:
            self.device_detail_panel.append_log("✅ 遥控器配对成功")
            self.device_detail_panel.update_test_result("remote_pairing", "passed")
        else:
            self.device_detail_panel.append_log("❌ 遥控器配对失败")
            self.device_detail_panel.update_test_result("remote_pairing", "failed")
        test_engine.close()
        self._finish_device_test(sn)

    def start_emergency_switch_test(self, device):
        if not self._begin_device_test(device, "应急开关测试"):
            return
        self.device_detail_panel.update_test_result("emergency_switch", "testing")
        self.device_detail_panel.append_log(f"开始应急开关测试: {device.sn}")

        try:
            mqtt_client = self._ensure_mqtt_client(device)
            if not mqtt_client:
                self._finish_device_test(device.sn)
                return

            test_engine = TestEngine(mqtt_client, self.config, device.hw_ver)

            show_message(
                self,
                '应急开关测试',
                '即将进行应急开关测试\n\n'
                '1. 门锁将上锁\n'
                '2. 请按应急开关\n'
                '3. 系统将在10秒内检测门锁是否开启\n\n'
                '请点击确定开始测试'
            )

            start_time = time.time()
            thread = SingleTestThread(test_engine, lambda: test_engine.test_emergency_switch(timeout=10))
            thread.progress_signal.connect(self.device_detail_panel.append_log)
            thread.finished_signal.connect(
                lambda success, st=start_time, te=test_engine, sn=device.sn:
                self._on_emergency_switch_finished(success, time.time() - st, sn, te)
            )
            thread.start()
            self._single_test_thread = thread

        except Exception as e:
            self._finish_device_test(device.sn)
            self.device_detail_panel.append_log(f"❌ 应急开关测试失败: {str(e)}")
            self.device_detail_panel.update_test_result("emergency_switch", "failed")
            QMessageBox.critical(self, '错误', f'应急开关测试失败: {str(e)}')

    def _on_emergency_switch_finished(self, success: bool, duration: float, sn: str, test_engine):
        import uuid
        from datetime import datetime

        # 保存测试记录
        steps = [{'name': s['name'], 'success': s['success'], 'message': s['message']} for s in test_engine.result.steps]
        record = {
            'id': str(uuid.uuid4()),
            'device_sn': sn,
            'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'test_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'test_type': '应急开关测试',
            'status': 'passed' if success else 'failed',
            'duration': round(duration, 2),
            'steps': steps
        }
        self.test_record_storage.upsert_record(record)

        if success:
            self.device_detail_panel.append_log("✅ 应急开关测试成功")
            self.device_detail_panel.update_test_result("emergency_switch", "passed")
        else:
            self.device_detail_panel.append_log("❌ 应急开关测试失败")
            self.device_detail_panel.update_test_result("emergency_switch", "failed")
        test_engine.close()
        self._finish_device_test(sn)

    # ---------------------------------------------------------------
    # Firmware & OTA
    # ---------------------------------------------------------------
    def upload_firmware(self):
        firmware_path, _ = QFileDialog.getOpenFileName(
            self,
            '选择固件文件',
            '',
            '固件文件 (*.fwpkg *.bin);;所有文件 (*.*)'
        )

        if not firmware_path:
            return

        if not self.tftp_server:
            QMessageBox.critical(self, '错误', 'TFTP服务器未启动')
            return

        try:
            import os
            self.tftp_server.set_firmware_file(firmware_path)
            self.current_firmware_path = firmware_path
            self.current_firmware_name = os.path.basename(firmware_path)

            file_size = os.path.getsize(firmware_path)
            size_mb = file_size / (1024 * 1024)

            self.device_detail_panel.update_firmware_status(self.current_firmware_name, size_mb)

            logger.info(f"固件已上传: {self.current_firmware_name}, 大小: {size_mb:.2f} MB")
            show_message(self, '成功', f'固件已上传成功\n\n文件: {self.current_firmware_name}\n大小: {size_mb:.2f} MB')
        except Exception as e:
            logger.error(f"上传固件失败: {e}")
            QMessageBox.critical(self, '错误', f'上传固件失败: {str(e)}')

    def start_ota_upgrade(self, sn: str):
        device = self.devices.get(sn)
        if not device:
            return

        if not self.current_firmware_path:
            QMessageBox.warning(self, '警告', '请先上传固件文件')
            return

        if not self.tftp_server:
            QMessageBox.critical(self, '错误', 'TFTP服务器未启动\n\n请使用sudo运行程序')
            return

        if not self.tftp_server.firmware_data:
            QMessageBox.critical(self, '错误', '固件未加载到TFTP服务器\n\n请重新上传固件')
            return

        try:
            tftp_server_ip = self.tftp_server.host
            tftp_port = self.tftp_server.port

            self.device_detail_panel.append_log(f"开始OTA升级: {device.sn}")
            self.device_detail_panel.append_log(f"固件文件: {self.current_firmware_name}")
            self.device_detail_panel.append_log(f"TFTP服务器: {tftp_server_ip}:{tftp_port}")
            self.device_detail_panel.append_log(f"固件大小: {len(self.tftp_server.firmware_data)} 字节")

            logger.info(f"OTA升级 - 设备: {device.sn}, IP: {device.ip}")
            logger.info(f"OTA升级 - TFTP: {tftp_server_ip}:{tftp_port}/{self.current_firmware_name}")
            logger.info(f"OTA升级 - 固件大小: {len(self.tftp_server.firmware_data)} 字节")

            mqtt_client = self._ensure_mqtt_client(device)
            if not mqtt_client:
                return

            mqtt_client.register_callback(f"ota_log_{device.sn}", lambda topic, msg: self._on_device_log(device.sn, topic, msg))

            test_engine = TestEngine(mqtt_client, self.config, device.hw_ver)

            self.device_detail_panel.append_log("正在发送OTA升级指令...")
            file_size = len(self.tftp_server.firmware_data)

            self.device_ota_in_progress.add(device.sn)
            self.device_ip_to_sn[device.ip] = device.sn
            self.device_ota_progress[device.sn] = 0
            self.device_detail_panel.progress_bar.setVisible(True)
            self.device_detail_panel.progress_bar.setValue(0)

            ota_thread = OTAThread(test_engine, tftp_server_ip, tftp_port, self.current_firmware_name, file_size)
            ota_thread.log_signal.connect(lambda msg: self._emit_ota_log(device.sn, msg))
            ota_thread.finished_signal.connect(
                lambda success, sn=device.sn, te=test_engine, mc=mqtt_client:
                self._on_ota_finished(sn, success, te, mc)
            )
            ota_thread.start()

            show_message(self, '提示', 'OTA升级已启动\n设备正在下载固件')

        except Exception as e:
            self.device_detail_panel.append_log(f"❌ OTA升级异常: {str(e)}")
            QMessageBox.critical(self, '错误', f'OTA升级异常: {str(e)}')
            self.device_ota_in_progress.discard(device.sn)

    # ---------------------------------------------------------------
    # Print & Reset
    # ---------------------------------------------------------------
    def _upload_test_data(self, sn: str) -> bool:
        """上传设备测试数据到服务器"""
        try:
            from datetime import datetime
            import requests

            records = self.test_record_storage.get_records_by_sn(sn)
            if not records:
                logger.warning(f"设备 {sn} 没有测试记录")
                return True

            upload_data = []
            for record in records:
                create_time = datetime.strptime(record['create_time'], '%Y-%m-%d %H:%M:%S')
                update_time = datetime.strptime(record['test_time'], '%Y-%m-%d %H:%M:%S')

                upload_data.append({
                    'cur_createTime': int(create_time.timestamp() * 1000),
                    'cur_updateTime': int(update_time.timestamp() * 1000),
                    'deviceName': record['test_type'],
                    'result': 'true' if record['status'] == 'passed' else 'false',
                    'sn': sn
                })

            self.device_detail_panel.append_log(f"正在上传测试数据，共 {len(upload_data)} 条...")

            response = requests.post(
                'http://ishop-oqa.weidian.com/checkData/addBatch',
                json=upload_data,
                timeout=10
            )

            if response.status_code == 200:
                self.device_detail_panel.append_log("✅ 测试数据上传成功")
                logger.info(f"设备 {sn} 测试数据上传成功")
                return True
            else:
                self.device_detail_panel.append_log(f"❌ 测试数据上传失败: HTTP {response.status_code}")
                logger.error(f"上传失败: HTTP {response.status_code}")
                return False

        except Exception as e:
            self.device_detail_panel.append_log(f"❌ 测试数据上传异常: {str(e)}")
            logger.error(f"上传测试数据异常: {e}")
            return False

    def print_label(self, sn: str):
        device = self.devices.get(sn)
        if not device:
            return

        # 检查所有测试项是否全部通过
        all_passed = all(
            widget.current_status == "passed"
            for widget in self.device_detail_panel.test_widgets.values()
        )
        if not all_passed:
            QMessageBox.warning(
                self, '无法打印',
                '只有全部测试项通过后才能打印标签。\n请先完成所有测试项。'
            )
            return

        self.device_detail_panel.append_log(f"打印标签: {sn}")

        upload_success = self._upload_test_data(sn)
        if not upload_success:
            reply = QMessageBox.question(
                self, '上传失败',
                '测试数据上传失败，是否继续打印标签？',
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return

        try:
            success = self.label_printer.print_label(sn, "PASSED")
            if success:
                self.device_detail_panel.append_log("标签打印成功")
            else:
                QMessageBox.warning(self, '警告', '标签打印失败')
        except Exception as e:
            QMessageBox.critical(self, '错误', f'打印失败: {str(e)}')

    def start_reset_config(self, sn: str):
        device = self.devices.get(sn)
        if not device:
            return
        if not self._begin_device_test(device, "重置NV配置"):
            return

        reply = QMessageBox.warning(
            self,
            '确认重置',
            f'确定要重置设备 {sn} 的NV配置吗？\n\n'
            '此操作将清除NV区域的所有配置，包括：\n'
            '- MQTT配置\n'
            '- WiFi配置\n'
            '- 设备信息等\n\n'
            '操作后需要重启设备才能生效。',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            self._finish_device_test(sn)
            return

        self.device_detail_panel.append_log(f"开始重置NV配置: {sn}")

        try:
            mqtt_client = self._ensure_mqtt_client(device)
            if not mqtt_client:
                return

            test_engine = TestEngine(mqtt_client, self.config, device.hw_ver)

            success, message = test_engine.reset_config(
                lambda msg: self.device_detail_panel.append_log(msg)
            )

            if success:
                self.device_detail_panel.append_log(f"✅ {message}")
                show_message(self, '成功', f'{message}\n\n请重启设备使配置生效。')
            else:
                self.device_detail_panel.append_log(f"❌ {message}")
                QMessageBox.critical(self, '失败', message)

        except Exception as e:
            self.device_detail_panel.append_log(f"❌ 重置配置失败: {str(e)}")
            QMessageBox.critical(self, '错误', f'重置配置失败: {str(e)}')
        finally:
            try:
                test_engine.close()
            except Exception:
                pass
            self._finish_device_test(sn)

    # ---------------------------------------------------------------
    # MQTT helper
    # ---------------------------------------------------------------
    def _ensure_mqtt_client(self, device):
        if device.sn not in self.device_mqtt_clients:
            # 门控工具默认使用本地broker模式，使用本机IP
            broker_ip = self._get_local_broker_ip()
            logger.info(f"门控工具使用本地Broker模式，MQTT连接到本机IP: {broker_ip}")

            mqtt_client = MQTTClient(
                broker_ip,
                self.config.mqtt_port,
                device.get_product_id(),
                device.sn
            )

            if not mqtt_client.connect():
                QMessageBox.critical(self, '错误', 'MQTT连接失败')
                return None

            self.device_mqtt_clients[device.sn] = mqtt_client

        return self.device_mqtt_clients[device.sn]

    def _get_local_broker_ip(self):
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

    def _validate_device_online(self, device) -> bool:
        existing_client = self.device_mqtt_clients.get(device.sn)
        query_msg = QueryStatusMessage(self.config.device_psk)

        if existing_client and existing_client.connected:
            response = existing_client.request(query_msg.to_json(), query_msg.mid, timeout=0.5)
            if response and response.get('header', {}).get('code', 0) == 0:
                logger.debug(f"设备 {device.sn} 在线校验通过（已有MQTT连接）")
                return True
            # 如果query超时但MQTT连接正常，也认为设备在线
            logger.debug(f"设备 {device.sn} query超时，但MQTT已连接，认为设备在线")
            return True

        # 创建临时探测客户端，大幅缩短超时时间以加快设备发现
        probe_client = MQTTClient(
            self._get_local_broker_ip(),
            self.config.mqtt_port,
            device.get_product_id(),
            device.sn,
            client_id_prefix=f"doorcheck_probe_{uuid.uuid4().hex[:8]}"
        )
        try:
            # 连接超时缩短到0.5秒，加快发现速度
            if not probe_client.connect(timeout=0.5):
                logger.debug(f"设备 {device.sn} MQTT连接失败，判定为离线")
                return False

            # query超时缩短到0.5秒，设备若已通过mDNS+HTTP配置请求，则必在线
            response = probe_client.request(query_msg.to_json(), query_msg.mid, timeout=0.5)
            if response and response.get('header', {}).get('code', 0) == 0:
                logger.debug(f"设备 {device.sn} 在线校验通过（query响应正常）")
                return True

            # 如果能连接MQTT但query超时，也认为设备在线（可能是旧固件不支持query）
            logger.debug(f"设备 {device.sn} 能连接MQTT但不响应query，仍认为在线")
            return True
        except Exception as e:
            logger.debug(f"设备 {device.sn} 在线校验异常: {e}")
            return False
        finally:
            probe_client.disconnect()

    # ---------------------------------------------------------------
    # Network services
    # ---------------------------------------------------------------
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
            # 启动时确定一次本机IP，确保HTTP server和MQTT client使用同一个地址
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_broker_ip = s.getsockname()[0]
                s.close()
                logger.info(f"HTTP配置服务将告知设备Broker地址: {local_broker_ip}")
            except Exception as e:
                logger.warning(f"获取本机IP失败: {e}，使用配置的broker地址")
                local_broker_ip = self.config.mqtt_broker

            self.config_server = ConfigServer(
                host='0.0.0.0',
                port=self.config.http_port,
                mqtt_broker=local_broker_ip,
                mqtt_port=self.config.mqtt_port,
                secret_key=self.config.device_psk,
                broker_mode='local',  # 门控工具默认使用本地模式
                # 门锁固件(v1.1.7+)要求username/password非空，且需为base64(AES-128-ECB)密文，
                # 留空会让设备在HTTP解析阶段就失败并陷入复重试循环
                mqtt_username=encrypt_mqtt_credential(self.config.device_psk),
                mqtt_password=encrypt_mqtt_credential(self.config.device_psk)
            )
            logger.info(
                f"MQTT凭据下发: username={mask_credential(self.config_server.mqtt_username)}, "
                f"password={mask_credential(self.config_server.mqtt_password)}"
            )
            self.http_thread = threading.Thread(
                target=self.config_server.start,
                daemon=True
            )
            self.http_thread.start()
            logger.info(f"HTTP配置服务已启动: 0.0.0.0:{self.config.http_port}")
        except Exception as e:
            logger.error(f"HTTP配置服务启动失败: {e}")

    def start_tftp_server(self):
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()

            self.tftp_server = TFTPServer(host=local_ip, port=69)
            self.tftp_server.set_progress_callback(self._on_tftp_progress)
            self.tftp_server.start()
            logger.info(f"TFTP服务器已启动: {local_ip}:69")
        except Exception as e:
            logger.error(f"TFTP服务器启动失败: {e}")
            logger.warning("TFTP端口69需要管理员权限，请使用sudo运行")

    def init_broadcast_mqtt(self):
        try:
            # 门控工具默认使用本地broker模式，使用本机IP
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                broker_ip = s.getsockname()[0]
                s.close()
                logger.info(f"门控工具使用本地Broker模式，广播MQTT连接到本机IP: {broker_ip}")
            except Exception as e:
                logger.warning(f"获取本机IP失败: {e}，使用配置的broker地址")
                broker_ip = self.config.mqtt_broker

            self.broadcast_mqtt_client = MQTTClient(
                broker_ip,
                self.config.mqtt_port,
                self.config.product_id,
                "broadcast"
            )
            if self.broadcast_mqtt_client.connect(use_wildcard=True):
                logger.info("广播MQTT客户端已连接")
                self.broadcast_mqtt_client.register_callback("heartbeat_monitor", self._on_heartbeat_received)
            else:
                logger.warning("广播MQTT客户端连接失败")
                self.broadcast_mqtt_client = None
        except Exception as e:
            logger.error(f"初始化广播MQTT客户端失败: {e}")
            self.broadcast_mqtt_client = None

    # ---------------------------------------------------------------
    # Network scan
    # ---------------------------------------------------------------
    def start_network_scan(self):
        self.device_list_panel.scan_btn.setEnabled(False)
        self.device_list_panel.scan_btn.setText("扫描中...")
        self.statusBar().showMessage("正在扫描网络，请稍候...")

        scan_thread = threading.Thread(target=self._do_network_scan, daemon=True)
        scan_thread.start()

    def _do_network_scan(self):
        import socket
        import ipaddress

        scan_port = self.config.get('network_scan.port', 57020)
        scan_subnet = self.config.get('network_scan.subnet', '')

        if not scan_subnet:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 80))
                local_ip = s.getsockname()[0]
                s.close()
                scan_subnet = str(ipaddress.ip_network(local_ip + '/24', strict=False))
            except Exception:
                scan_subnet = '192.168.1.0/24'

        try:
            hosts = list(ipaddress.ip_network(scan_subnet, strict=False).hosts())
        except Exception as e:
            logger.error(f"网络扫描子网解析失败: {e}")
            self._on_network_scan_done([])
            return

        logger.info(f"网络扫描: {scan_subnet} 端口 {scan_port} ({len(hosts)} 个主机)")
        found_ips = []
        lock = threading.Lock()

        def _probe(ip):
            try:
                with socket.create_connection((str(ip), scan_port), timeout=1.0):
                    with lock:
                        found_ips.append(str(ip))
            except OSError:
                pass

        threads = [threading.Thread(target=_probe, args=(h,), daemon=True) for h in hosts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)

        devices = []
        for ip in sorted(found_ips):
            mac = self._arp_lookup(ip)
            sn = mac.replace(':', '') if mac else None
            devices.append({'ip': ip, 'mac': mac, 'sn': sn})
            logger.info(f"网络扫描发现: IP={ip} MAC={mac}")

        self._on_network_scan_done(devices)

    def _arp_lookup(self, ip: str):
        import subprocess
        import platform

        def _run(cmd):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=5)
                return r.stdout.decode('utf-8', errors='replace')
            except Exception:
                return ''

        # ping 触发 ARP
        try:
            if platform.system() == 'Windows':
                subprocess.run(['ping', '-n', '1', '-w', '500', ip], capture_output=True, timeout=3)
            else:
                subprocess.run(['ping', '-c', '1', '-W', '1', ip], capture_output=True, timeout=3)
        except Exception:
            pass

        if platform.system() == 'Windows':
            for exe in ['arp', 'arp.exe']:
                out = _run([exe, '-a'])
                for line in out.splitlines():
                    if ip in line:
                        for token in line.split():
                            if len(token) == 17 and token.count('-') == 5:
                                return token.replace('-', ':').upper()
        else:
            for cmd in [['ip', 'neigh', 'show', ip], ['arp', '-n', ip]]:
                out = _run(cmd)
                for token in out.split():
                    if len(token) == 17 and token.count(':') == 5:
                        return token.upper()
        return None

    def _on_network_scan_done(self, devices: list):
        self.network_scan_done_signal.emit(devices)

    def _apply_network_scan_result(self, devices: list):
        self.device_list_panel.scan_btn.setEnabled(True)
        self.device_list_panel.scan_btn.setText("网络扫描")

        if not devices:
            self.statusBar().showMessage("网络扫描完成，未发现设备")
            return

        new_count = 0
        for d in devices:
            sn = d.get('sn')
            ip = d['ip']
            if not sn:
                logger.warning(f"网络扫描: IP={ip} 未能获取MAC，跳过")
                continue
            if sn not in self.devices:
                # 创建设备信息，默认为门控设备类型
                device = DeviceInfo(
                    sn=sn,
                    type=DEVICE_TYPE_SMART_DOOR,
                    type_code=DEVICE_TYPE_MAP[DEVICE_TYPE_SMART_DOOR],
                    ip=ip,
                    port=self.config.get('network_scan.port', 57020),
                    model='Unknown'
                )
                self.device_found_signal.emit(device)
                new_count += 1
            else:
                # 更新 IP
                existing = self.devices[sn]
                if existing.ip != ip:
                    existing.ip = ip
                    self.device_list_panel.remove_device(sn)
                    self.device_list_panel.add_device(existing)

        self.statusBar().showMessage(
            f"网络扫描完成，发现 {len(devices)} 个设备，新增 {new_count} 个"
        )

    # ---------------------------------------------------------------
    # Device discovery
    # ---------------------------------------------------------------
    def start_device_discovery(self):
        logger.info("启动设备发现（仅门控设备）...")
        try:
            self.zeroconf = Zeroconf()

            self.master_mdns = MasterMdnsService(self.zeroconf, port=self.config.http_port)
            self.master_mdns.register()

            # 只接受门控设备
            self.listener = DeviceDiscoveryListener(
                on_device_found=self.on_device_found,
                on_device_removed=self.on_device_removed,
                device_types=[DEVICE_TYPE_SMART_DOOR]  # 设备类型过滤
            )
            self.browser = ServiceBrowser(self.zeroconf, self.config.mdns_service_type, self.listener)
            logger.info("设备发现已启动，过滤条件：仅智能门控")
        except Exception as e:
            logger.error(f"设备发现启动失败: {e}")

    def on_device_found(self, device: DeviceInfo):
        self.device_found_signal.emit(device)

    def on_device_removed(self, device_sn: str):
        self.device_removed_signal.emit(device_sn)

    # ---------------------------------------------------------------
    # Heartbeat
    # ---------------------------------------------------------------
    def start_heartbeat_monitor(self):
        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(self._check_device_heartbeat)
        self.heartbeat_timer.start(30000)
        logger.info("心跳监控已启动，检查间隔: 30秒")

    def _on_heartbeat_received(self, topic: str, message: dict):
        try:
            if "status" in topic:
                header = message.get("header", {})
                action = header.get("action", "")

                if action == "heartbeat":
                    device_info = header.get("device", {})
                    device_sn = device_info.get("sn", "")

                    if device_sn:
                        self.device_last_heartbeat[device_sn] = time.time()
                        self.device_heartbeat_miss_count[device_sn] = 0

                        device = self.devices.get(device_sn)
                        if device:
                            device.hw_ver = device_info.get("hw_ver", device.hw_ver)
                            device.fw_ver = device_info.get("fw_ver", device.fw_ver)
                            device.model = device_info.get("model", device.model)

                            # 更新MQTT连接状态，显示实际的MQTT服务器IP
                            mqtt_broker_ip = self._get_local_broker_ip()
                            self.device_list_panel.update_device_mqtt_status(device_sn, True, mqtt_broker_ip)
                        else:
                            pending_device = self.pending_discovered_devices.get(device_sn)
                            if pending_device:
                                pending_device.hw_ver = device_info.get("hw_ver", pending_device.hw_ver)
                                pending_device.fw_ver = device_info.get("fw_ver", pending_device.fw_ver)
                                pending_device.model = device_info.get("model", pending_device.model)
                                self.device_validated_signal.emit(pending_device)
                                logger.info(f"收到待校验设备 {device_sn} 心跳，自动加入设备列表")

                        logger.debug(f"收到设备 {device_sn} 心跳")
        except Exception as e:
            logger.error(f"处理心跳消息失败: {e}")

    def _check_device_heartbeat(self):
        current_time = time.time()
        offline_devices = []

        for device_sn, last_heartbeat in list(self.device_last_heartbeat.items()):
            if device_sn in self.device_ota_in_progress or device_sn in self.device_test_in_progress:
                continue

            if current_time - last_heartbeat > self.heartbeat_timeout:
                miss_count = self.device_heartbeat_miss_count.get(device_sn, 0)
                miss_count += 1
                self.device_heartbeat_miss_count[device_sn] = miss_count

                if miss_count >= self.heartbeat_max_miss:
                    offline_devices.append(device_sn)
                    logger.warning(f"设备 {device_sn} 心跳超时 {miss_count} 次，判定为离线")
                else:
                    logger.debug(f"设备 {device_sn} 心跳超时 {miss_count}/{self.heartbeat_max_miss} 次")

        for device_sn in offline_devices:
            self.device_list_panel.update_device_mqtt_status(device_sn, False)
            self.device_removed_signal.emit(device_sn)

    def _check_offline_devices(self):
        current_time = time.time()
        offline_devices = []

        for sn, device in self.devices.items():
            last_heartbeat = self.device_last_heartbeat.get(sn, 0)
            if last_heartbeat > 0 and current_time - last_heartbeat > self.heartbeat_timeout:
                offline_devices.append(sn)
                logger.warning(f"刷新检测: 设备 {sn} 不在线，移除")

        for device_sn in offline_devices:
            self.device_removed_signal.emit(device_sn)

    # ---------------------------------------------------------------
    # Device found/removed handlers (main thread)
    # ---------------------------------------------------------------
    def _on_device_found_main_thread(self, device: DeviceInfo):
        # mDNS/Zeroconf may return cached records after power-off, so verify via MQTT first.
        self._schedule_device_validation(device)

    def _add_or_update_device_main_thread(self, device: DeviceInfo):
        with self.pending_device_validation_lock:
            self.pending_discovered_devices.pop(device.sn, None)
            self.pending_device_validations.discard(device.sn)

        existing = self.devices.get(device.sn)
        if existing:
            if not device.hw_ver:
                device.hw_ver = existing.hw_ver
            if not device.fw_ver:
                device.fw_ver = existing.fw_ver
            self.devices[device.sn] = device
            self.device_list_panel.remove_device(device.sn)
            self.device_list_panel.add_device(device)
            logger.info(f"更新设备: {device.get_display_name()} ({device.ip})")
        else:
            self.devices[device.sn] = device
            self.device_list_panel.add_device(device)
            logger.info(f"发现设备: {device.get_display_name()} ({device.ip})")

        self.device_last_heartbeat[device.sn] = time.time()
        self.device_heartbeat_miss_count[device.sn] = 0
        self.device_ip_to_sn[device.ip] = device.sn

        # Update MQTT status - 使用实际的本地Broker IP
        self.device_list_panel.update_device_mqtt_status(device.sn, True, self._get_local_broker_ip())

        if device.sn in self.device_ota_in_progress:
            self.device_ota_in_progress.discard(device.sn)
            logger.info(f"设备 {device.sn} OTA完成，重新上线")

        # Restore status on card
        status = self.device_test_status.get(device.sn)
        if status:
            self.device_list_panel.update_device_status(device.sn, status)

        self.statusBar().showMessage(f"发现设备: {device.sn}")

    def _schedule_device_validation(self, device: DeviceInfo):
        last_heartbeat = self.device_last_heartbeat.get(device.sn, 0)
        if device.sn in self.devices and last_heartbeat and time.time() - last_heartbeat <= self.heartbeat_timeout:
            self._add_or_update_device_main_thread(device)
            return

        with self.pending_device_validation_lock:
            self.pending_discovered_devices[device.sn] = device
            if device.sn in self.pending_device_validations:
                return
            self.pending_device_validations.add(device.sn)
            generation = self.discovery_generation

        thread = threading.Thread(
            target=self._validate_device_online_worker,
            args=(device.sn, generation),
            daemon=True
        )
        thread.start()

    def _validate_device_online_worker(self, device_sn: str, generation: int):
        max_attempts = 1  # 优化：只尝试1次，设备通过HTTP配置请求即可认为在线
        retry_delay = 0.5  # 优化：如果需要重试，延迟缩短到0.5秒
        try:
            for attempt in range(max_attempts):
                if generation != self.discovery_generation:
                    logger.debug(f"设备 {device_sn} 校验已取消（generation变化）")
                    return

                device = self.pending_discovered_devices.get(device_sn)
                if not device:
                    logger.debug(f"设备 {device_sn} 已从待校验列表移除")
                    return

                logger.debug(f"设备 {device_sn} 在线校验 尝试 {attempt + 1}/{max_attempts}")
                if self._validate_device_online(device):
                    if generation == self.discovery_generation:
                        logger.info(f"设备 {device_sn} 在线校验通过")
                        self.device_validated_signal.emit(device)
                    return

                if attempt < max_attempts - 1:
                    time.sleep(retry_delay)

            device = self.pending_discovered_devices.get(device_sn)
            if device:
                logger.info(f"忽略离线或缓存设备: {device.sn} ({device.ip}) - {max_attempts}次校验均失败")
        finally:
            with self.pending_device_validation_lock:
                if generation == self.discovery_generation:
                    self.pending_device_validations.discard(device_sn)
                    self.pending_discovered_devices.pop(device_sn, None)

    def _on_device_removed_main_thread(self, device_sn: str):
        if device_sn not in self.devices:
            return

        del self.devices[device_sn]
        self.device_list_panel.remove_device(device_sn)

        # 清理心跳数据
        self.device_last_heartbeat.pop(device_sn, None)
        self.device_heartbeat_miss_count.pop(device_sn, None)

        if self.selected_device_sn == device_sn:
            self.selected_device_sn = None
            self.device_detail_panel.clear_device()

        if device_sn in self.device_mqtt_clients:
            self.device_mqtt_clients[device_sn].disconnect()
            del self.device_mqtt_clients[device_sn]

        if device_sn in self.device_test_threads:
            del self.device_test_threads[device_sn]

        if device_sn in self.device_test_status:
            del self.device_test_status[device_sn]

        if device_sn in self.device_last_heartbeat:
            del self.device_last_heartbeat[device_sn]

        if device_sn in self.device_ota_progress:
            del self.device_ota_progress[device_sn]

        self.device_test_in_progress.discard(device_sn)

        for ip, sn in list(self.device_ip_to_sn.items()):
            if sn == device_sn:
                del self.device_ip_to_sn[ip]

        self.statusBar().showMessage(f"设备离线: {device_sn}")
        logger.info(f"设备离线: {device_sn}")

    # ---------------------------------------------------------------
    # OTA progress
    # ---------------------------------------------------------------
    def _on_tftp_progress(self, transfer_id: str, progress: int, sent_bytes: int, total_bytes: int):
        self.ota_progress_signal.emit(transfer_id, progress, sent_bytes, total_bytes)

    def _on_ota_progress_update(self, transfer_id: str, progress: int, sent_bytes: int, total_bytes: int):
        client_ip = transfer_id.split(':')[0]
        device_sn = self.device_ip_to_sn.get(client_ip)

        if not device_sn:
            logger.warning(f"无法找到IP {client_ip} 对应的设备SN，当前映射: {self.device_ip_to_sn}")
            return

        last_progress = self.device_ota_progress.get(device_sn, -1)
        if progress > last_progress and (progress // 5 > last_progress // 5 or last_progress < 0):
            self.device_ota_progress[device_sn] = progress

            # Only update UI if this device is currently selected
            if device_sn == self.selected_device_sn:
                size_mb = total_bytes / (1024 * 1024)
                sent_mb = sent_bytes / (1024 * 1024)
                self.device_detail_panel.update_ota_progress(progress, sent_mb, size_mb)

            logger.info(f"设备 {device_sn} OTA进度: {progress}%")

        if progress >= 100 and device_sn in self.device_ota_in_progress:
            self.device_ota_in_progress.discard(device_sn)
            self.device_ota_progress.pop(device_sn, None)
            size_mb = total_bytes / (1024 * 1024)
            logger.info(f"设备 {device_sn} 固件传输完成，共 {size_mb:.2f} MB")

            if device_sn == self.selected_device_sn:
                self.device_detail_panel.append_log(f"✅ 固件传输完成，共 {size_mb:.2f} MB，请等待设备重启")
                self.device_detail_panel.hide_progress_bar()

            show_message(self, 'OTA升级', f'固件传输完成\n\n设备: {device_sn}\n大小: {size_mb:.2f} MB\n\n请等待设备重启')

    def _on_device_log(self, device_sn: str, topic: str, message: dict):
        if "log" not in topic:
            return

        try:
            body = message.get("body", {})
            log_msg = body.get("message", "")

            if not log_msg and isinstance(body.get("logs"), list):
                log_msg = "\n".join(
                    item.get("message", "")
                    for item in body.get("logs", [])
                    if item.get("message")
                )

            if not log_msg:
                return

            if device_sn == self.selected_device_sn:
                self.device_detail_panel.append_log(f"[设备] {log_msg}")

            logger.info(f"设备 {device_sn} 日志: {log_msg}")
        except Exception as e:
            logger.error(f"处理设备日志失败: {e}")

    def _emit_ota_log(self, device_sn: str, message: str):
        if device_sn == self.selected_device_sn:
            self.device_detail_panel.append_log(message)

    def _on_ota_finished(self, device_sn: str, success: bool, test_engine=None, mqtt_client=None):
        if test_engine:
            test_engine.close()
        if mqtt_client:
            mqtt_client.unregister_callback(f"ota_log_{device_sn}")
        if not success:
            self.device_ota_in_progress.discard(device_sn)

    # ---------------------------------------------------------------
    # Refresh devices
    # ---------------------------------------------------------------
    def refresh_devices(self):
        logger.info("开始刷新设备列表...")

        if self.device_test_in_progress or self.device_ota_in_progress:
            self.statusBar().showMessage("测试或OTA进行中，暂不能刷新设备")
            QMessageBox.warning(self, '操作进行中', '当前有测试或OTA任务正在执行，请完成后再刷新设备。')
            return

        self._clear_discovered_devices()

        # 完全重建Zeroconf实例以清除mDNS缓存
        try:
            if self.browser:
                self.browser.cancel()
                logger.info("已停止旧的ServiceBrowser")

            # 清除listener中的已发现设备记录
            if self.listener:
                with self.listener._lock:
                    self.listener.discovered_devices.clear()
                    logger.info("已清除listener设备记录")

            # 关闭旧的Zeroconf实例（清除底层mDNS缓存）
            if self.zeroconf:
                # 注销master_mdns服务
                if self.master_mdns:
                    self.master_mdns.unregister()
                    logger.info("已注销master mDNS服务")

                self.zeroconf.close()
                logger.info("已关闭旧的Zeroconf实例，清除mDNS缓存")
                self.zeroconf = None

            # 重新创建Zeroconf实例和ServiceBrowser
            time.sleep(0.5)  # 短暂延迟确保端口释放

            self.zeroconf = Zeroconf()
            logger.info("已创建新的Zeroconf实例")

            # 重新注册master_mdns服务
            self.master_mdns = MasterMdnsService(self.zeroconf, port=self.config.http_port)
            self.master_mdns.register()
            logger.info("已重新注册master mDNS服务")

            # 创建新的listener（使用相同的回调）
            self.listener = DeviceDiscoveryListener(
                on_device_found=self.on_device_found,
                on_device_removed=self.on_device_removed,
                device_types=[DEVICE_TYPE_SMART_DOOR]
            )

            # 启动新的ServiceBrowser
            self.browser = ServiceBrowser(self.zeroconf, self.config.mdns_service_type, self.listener)
            logger.info("已启动新的ServiceBrowser，正在扫描网络...")
        except Exception as e:
            logger.error(f"重启设备发现失败: {e}")

        # 如果有已知设备，也通过MQTT发送discover命令
        if self.broadcast_mqtt_client and self.broadcast_mqtt_client.connected and len(self.devices) > 0:
            try:
                discover_msg = DiscoverMessage(self.config.device_psk)
                payload = discover_msg.to_json()

                for sn, device in self.devices.items():
                    try:
                        topic = f"{device.get_product_id()}/{device.sn}/command"
                        self.broadcast_mqtt_client.client.publish(topic, payload, qos=0)
                        logger.info(f"向设备 {device.sn} 发送discover命令")
                    except Exception as e:
                        logger.error(f"向设备 {device.sn} 发送discover命令失败: {e}")

                logger.info(f"已向 {len(self.devices)} 个设备发送discover命令")

            except Exception as e:
                logger.error(f"广播discover命令失败: {e}")

    def _clear_discovered_devices(self):
        for mqtt_client in list(self.device_mqtt_clients.values()):
            mqtt_client.disconnect()
        self.device_mqtt_clients.clear()

        self.devices.clear()
        self.device_last_heartbeat.clear()
        self.device_heartbeat_miss_count.clear()
        self.device_ip_to_sn.clear()
        with self.pending_device_validation_lock:
            self.discovery_generation += 1
            self.pending_device_validations.clear()
            self.pending_discovered_devices.clear()
        self.selected_device_sn = None
        self.device_detail_panel.clear_device()
        self.device_list_panel.clear_devices()
        self.statusBar().showMessage("已清空旧设备，正在重新发现...")
        logger.info("刷新发现前已清空旧设备和连接，等待在线校验结果")

    # ---------------------------------------------------------------
    # Close
    # ---------------------------------------------------------------
    def closeEvent(self, event):
        logger.info("程序退出，开始清理资源...")

        # 1. 停止心跳监控
        try:
            if hasattr(self, 'heartbeat_timer'):
                self.heartbeat_timer.stop()
                logger.info("心跳监控已停止")
        except Exception as e:
            logger.error(f"停止心跳监控失败: {e}")

        # 2. 断开所有MQTT连接
        for mqtt_client in self.device_mqtt_clients.values():
            try:
                mqtt_client.disconnect()
            except Exception as e:
                logger.error(f"断开MQTT客户端失败: {e}")

        if self.broadcast_mqtt_client:
            try:
                self.broadcast_mqtt_client.disconnect()
                logger.info("广播MQTT客户端已断开")
            except Exception as e:
                logger.error(f"断开广播MQTT客户端失败: {e}")

        # 3. 停止mDNS服务发现（关键：按正确顺序清理）
        try:
            # 先停止 ServiceBrowser
            if hasattr(self, 'browser') and self.browser:
                self.browser.cancel()
                logger.info("ServiceBrowser已停止")
                time.sleep(0.2)  # 给 ServiceBrowser 时间完成清理

            # 再注销 mDNS 服务
            if self.master_mdns:
                self.master_mdns.unregister()
                logger.info("mDNS服务已注销")
                time.sleep(0.2)

            # 最后关闭 Zeroconf
            if self.zeroconf:
                self.zeroconf.close()
                logger.info("Zeroconf已关闭")
                time.sleep(0.3)  # 确保UDP端口完全释放
        except Exception as e:
            logger.error(f"清理Zeroconf资源失败: {e}")

        # 4. 停止HTTP配置服务
        if hasattr(self, 'config_server') and self.config_server:
            try:
                self.config_server.stop()
                logger.info("HTTP配置服务已停止")
            except Exception as e:
                logger.error(f"停止HTTP服务失败: {e}")

        # 5. 停止MQTT Broker
        if self.mqtt_broker:
            try:
                self.mqtt_broker.stop()
                logger.info("MQTT Broker已停止")
            except Exception as e:
                logger.error(f"停止MQTT Broker失败: {e}")

        # 6. 停止TFTP服务器
        if self.tftp_server:
            try:
                self.tftp_server.stop()
                logger.info("TFTP服务器已停止")
            except Exception as e:
                logger.error(f"停止TFTP服务器失败: {e}")

        logger.info("所有资源清理完成")
        event.accept()
