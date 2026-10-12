import os
import sys
import threading
import yaml
from PyQt5.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                             QMessageBox, QFileDialog, QInputDialog, QSplitter,
                             QDialog, QListWidget, QDialogButtonBox, QLabel,
                             QProgressBar, QListWidgetItem, QTabWidget, QPushButton)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon
from src.master_control.gui.device_list_panel import DeviceListPanel
from src.master_control.gui.device_detail_panel import DeviceDetailPanel
from src.master_control.gui.test_results_window import TestResultsWindow
from src.master_control.gui.printer_config_dialog import PrinterConfigDialog
from src.master_control.core.discovery import DeviceDiscovery
from src.master_control.core.device import MasterDevice
from src.master_control.core.tester import DeviceTester
from src.master_control.core.database import Database
from src.master_control.core.test_result import TestStatus
from src.master_control.utils.excel_exporter import ExcelExporter
from src.master_control.utils.universal_printer import create_printer_from_config
from src.master_control.utils.config import config
from src.master_control.utils.logger import logger
from src.master_control.utils.data_uploader import DataUploader
from src.utils.config import Config as AppConfig


class DeviceConnectWorker(QThread):
    """异步连接设备"""
    success_signal = pyqtSignal(str, object)  # sn, tester
    failed_signal = pyqtSignal(str, str)  # sn, error_msg
    log_signal = pyqtSignal(str)

    def __init__(self, device):
        super().__init__()
        self.device = device

    def run(self):
        # 整体兜底：run() 里任何未捕获异常都会让 Qt 静默 abort 进程
        try:
            sn = self.device.sn
            ip = self.device.ip

            self.log_signal.emit(f"正在连接设备 {sn} ({ip})...")

            try:
                # 创建 tester 并连接
                tester = DeviceTester(ip, sn)

                # 设置更长的超时，避免误报
                if not tester.ssh:
                    tester.ssh = None

                # 尝试连接（paramiko.connect 内部已有超时机制）
                if tester.connect():
                    self.log_signal.emit(f"设备 {sn} 连接成功")
                    self.success_signal.emit(sn, tester)
                else:
                    self.log_signal.emit(f"设备 {sn} 连接失败")
                    self.failed_signal.emit(sn, "SSH连接失败，请检查设备网络和SSH服务")
            except Exception as e:
                logger.error(f"连接设备异常 {sn}: {e}", exc_info=True)
                self.failed_signal.emit(sn, f"连接异常: {e}")
        except Exception as e:
            logger.error(f"连接线程异常: {e}", exc_info=True)


class OsVersionWorker(QThread):
    """异步获取设备OS版本"""
    result_signal = pyqtSignal(str, str)  # sn, version

    def __init__(self, sn: str, tester):
        super().__init__()
        self.sn = sn
        self.tester = tester

    def run(self):
        try:
            version = "获取失败"
            try:
                success, stdout, _ = self.tester.ssh.execute_command(
                    "cat /etc/wd-image-version 2>/dev/null", timeout=5
                )
                version = stdout.strip() if success and stdout.strip() else "未知"
            except Exception as e:
                logger.debug(f"[{self.sn}] 获取系统版本失败: {e}")

            # emit 放在 try 内：窗口关闭后信号接收者可能已销毁，此处抛异常同样会 abort
            self.result_signal.emit(self.sn, version)
        except Exception as e:
            logger.error(f"[{self.sn}] 系统版本线程异常: {e}", exc_info=True)


class SwitchModeWorker(QThread):
    """模式切换线程"""

    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, tester):
        super().__init__()
        self.tester = tester

    def run(self):
        # 整体兜底，避免任何未捕获异常静默终止进程
        try:
            self.log_signal.emit(f"[{self.tester.sn}] 切换到生产模式...")
            try:
                success, results = self.tester.switch_to_normal_mode()
                for msg in results:
                    self.log_signal.emit(f"[{self.tester.sn}] {msg}")
                if success:
                    self.finished_signal.emit(True, "已切换到生产模式")
                else:
                    self.finished_signal.emit(False, "切换生产模式失败")
            except Exception as e:
                logger.error(f"[{self.tester.sn}] 切换模式异常: {e}", exc_info=True)
                self.log_signal.emit(f"[{self.tester.sn}] 切换异常: {e}")
                self.finished_signal.emit(False, f"切换异常: {e}")
        except Exception as e:
            logger.error(f"切换模式线程异常: {e}", exc_info=True)


class TestWorker(QThread):

    log_signal = pyqtSignal(str)
    result_signal = pyqtSignal(str, str, object, str, str)
    finished_signal = pyqtSignal(str)

    def __init__(self, test_name: str, tester):
        super().__init__()
        self.test_name = test_name
        self.tester = tester

    def run(self):
        # run() 内未捕获异常会让 Qt 静默 abort 进程，统一兜底
        try:
            self._run_impl()
        except Exception as e:
            logger.error(f"单项测试线程异常: {e}", exc_info=True)
            try:
                self.finished_signal.emit(getattr(self.tester, 'sn', ''))
            except Exception:
                pass

    def _run_impl(self):
        self.log_signal.emit(f"[{self.tester.sn}] 开始执行 {self.test_name} 测试...")

        try:
            methods = {
                "poe": "test_poe_ports",
                "disk": "test_disk",
                "fan": "test_fan",
                "lte_4g": "test_4g",
                "wifi": "test_wifi",
                "bluetooth": "test_bluetooth",
                "nearlink": "test_nearlink",
                "mac_write": "test_mac_write",
                "hdmi": "test_hdmi",
                "battery": "test_battery",
            }

            method_name = methods.get(self.test_name)
            method = getattr(self.tester, method_name, None) if method_name else None
            if method is None:
                self.log_signal.emit(f"[{self.tester.sn}] 未知或不支持的检测项: {self.test_name}")
                self.result_signal.emit(self.test_name, self.tester.sn, TestStatus.FAILED,
                                        "不支持的检测项", f"当前设备不支持 {self.test_name}")
                return

            result = method()
            if result:
                self.log_signal.emit(f"[{self.tester.sn}] {self.test_name} 测试完成: {result.message}")
                self.result_signal.emit(self.test_name, self.tester.sn,
                                        result.status, result.message, result.detail)
        except Exception as e:
            logger.error(f"[{self.tester.sn}] {self.test_name} 测试异常: {e}", exc_info=True)
            self.log_signal.emit(f"[{self.tester.sn}] {self.test_name} 测试异常: {e}")
            self.result_signal.emit(self.test_name, self.tester.sn, TestStatus.FAILED,
                                    "测试异常", str(e))
        finally:
            self.finished_signal.emit(self.tester.sn)


class AutoTestWorker(QThread):
    """一键测试线程

    组1(硬件检测)与组2(无线通信)并发执行，组内顺序执行。

    注意：不使用 ThreadPoolExecutor。
    QThread 只能保证 run() 返回后线程结束，而线程池的 worker 是非守护线程，
    QThread 被销毁时池里的线程可能还在跑（POE 网段 ping 最久要 30s），
    Qt 会直接 qFatal/abort 杀掉整个进程，表现为日志毫无征兆地中断。
    这里改成两个真正的 QThread 子线程，销毁前可以 wait() 等待。
    """

    log_signal = pyqtSignal(str)
    result_signal = pyqtSignal(str, str, object, str, str)
    finished_signal = pyqtSignal(bool)

    def __init__(self, tester):
        super().__init__()
        self.tester = tester
        self.test_results = {}
        self._stop_flag = False
        self._sub_workers = []

    def _run_group(self, tests, worker_index: int):
        """在工作线程中顺序执行一组检测，结果写入 self.test_results"""
        for test_name, test_method in tests:
            if self._stop_flag:
                return

            self.result_signal.emit(test_name, self.tester.sn, TestStatus.TESTING, "检测中...", "")
            try:
                result = test_method()
                self.test_results[test_name] = result
                self.log_signal.emit(f"[{self.tester.sn}] {test_name} 测试完成")
                self.result_signal.emit(test_name, self.tester.sn,
                                        result.status, result.message, result.detail)
            except Exception as e:
                logger.error(f"[{self.tester.sn}] {test_name} 测试异常: {e}", exc_info=True)
                self.log_signal.emit(f"[{self.tester.sn}] {test_name} 测试异常: {e}")
                self.test_results[test_name] = None
                self.result_signal.emit(test_name, self.tester.sn,
                                        TestStatus.FAILED, "测试异常", str(e))

    def run(self):
        # QThread.run() 里未捕获的异常会让 Qt 直接 abort 整个进程（且不打印 traceback），
        # 所以这里整体兜底，保证任何意外都不会静默杀掉程序。
        try:
            self._run_impl()
        except Exception as e:
            logger.error(f"[{self.tester.sn}] 一键测试线程异常: {e}", exc_info=True)
            self.log_signal.emit(f"[{self.tester.sn}] 一键测试异常: {e}")
            try:
                self.finished_signal.emit(False)
            except Exception:
                pass

    def _run_impl(self):
        self.log_signal.emit(f"[{self.tester.sn}] 开始一键测试...")
        self.test_results = {}

        # 用 getattr 取方法：设备型号差异导致某个检测项不存在时，
        # 只跳过该项，不要让整个线程带着异常死掉
        def collect(specs):
            out = []
            for test_name, method_name in specs:
                method = getattr(self.tester, method_name, None)
                if method is None:
                    logger.error(f"[{self.tester.sn}] tester 缺少方法 {method_name}，跳过 {test_name}")
                    self.log_signal.emit(f"[{self.tester.sn}] 跳过 {test_name}（当前设备不支持）")
                    self.test_results[test_name] = None
                    continue
                out.append((test_name, method))
            return out

        group1_tests = collect([
            ("disk", "test_disk"),
            ("fan", "test_fan"),
            ("hdmi", "test_hdmi"),
            ("lte_4g", "test_4g"),
            ("battery", "test_battery"),
            ("poe", "test_poe_ports")
        ])

        group2_tests = collect([
            ("wifi", "test_wifi"),
            ("bluetooth", "test_bluetooth"),
            ("nearlink", "test_nearlink"),
        ])

        # 两个子线程并发跑两组检测
        sub1 = threading.Thread(target=self._run_group, args=(group1_tests, 1),
                                name=f"autotest-g1-{self.tester.sn}", daemon=True)
        sub2 = threading.Thread(target=self._run_group, args=(group2_tests, 2),
                                name=f"autotest-g2-{self.tester.sn}", daemon=True)
        self._sub_workers = [sub1, sub2]

        sub1.start()
        sub2.start()

        # 阻塞等待子线程结束，这是 run() 不提前返回、线程不被提前销毁的关键
        for t in self._sub_workers:
            t.join()

        if self._stop_flag:
            self.log_signal.emit(f"[{self.tester.sn}] 测试已取消")
            self.finished_signal.emit(False)
            return

        all_passed = all(r and r.status == TestStatus.PASSED
                         for r in self.test_results.values())

        if not all_passed:
            self.log_signal.emit(f"[{self.tester.sn}] 测试未全部通过，停止测试")
            self.finished_signal.emit(False)
            return

        self.log_signal.emit(f"[{self.tester.sn}] 所有测试通过，开始MAC烧写...")
        mac_result = None
        try:
            mac_result = self.tester.test_mac_write()
        except Exception as e:
            logger.error(f"[{self.tester.sn}] MAC 烧写异常: {e}", exc_info=True)
            self.log_signal.emit(f"[{self.tester.sn}] MAC 烧写异常: {e}")

        if mac_result:
            self.result_signal.emit("mac_write", self.tester.sn,
                                    mac_result.status, mac_result.message, mac_result.detail)

        self.log_signal.emit(f"[{self.tester.sn}] 一键测试完成")
        self.finished_signal.emit(True)

    def stop(self):
        """请求停止：置标志位，并等待子线程收尾

        必须等子线程真正退出，否则 QThread 对象被销毁时子线程还在访问
        tester/SSH，同样会触发 "QThread: Destroyed while thread is still running"。
        当前正在执行的单条命令无法中断，这里给足超时并记录未及时结束的情况。
        """
        self._stop_flag = True
        for t in self._sub_workers:
            if t.is_alive():
                t.join(timeout=5.0)
                if t.is_alive():
                    logger.warning(f"[{self.tester.sn}] 子线程 {t.name} 未在5秒内退出")

    def join_sub_workers(self, timeout: float = 5.0):
        """等待子线程结束，供清理流程在 wait() 之后兜底调用"""
        for t in self._sub_workers:
            if t.is_alive():
                t.join(timeout=timeout)


class DiscoveryWorker(QThread):
    
    device_found = pyqtSignal(object)
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    scan_complete_signal = pyqtSignal(list)
    
    def __init__(self, discovery: DeviceDiscovery, mode: str = "mdns", subnet: str = None):
        super().__init__()
        self.discovery = discovery
        self.mode = mode
        self.subnet = subnet
    
    def run(self):
        # QThread.run() 未捕获异常会静默 abort 进程，这里整体兜底
        try:
            self._run_impl()
        except Exception as e:
            logger.error(f"设备发现线程异常: {e}", exc_info=True)
            self.log_signal.emit(f"设备发现异常: {e}")

    def _run_impl(self):
        if self.mode == "mdns":
            self.log_signal.emit("开始 mDNS 设备发现...")
            self.discovery.start_mdns_discovery()
        elif self.mode == "scan":
            subnet_str = self.subnet if self.subnet else config.scan_subnet
            self.log_signal.emit(f"开始子网扫描: {subnet_str}")

            def progress_callback(scanned, total):
                self.progress_signal.emit(scanned, total)

            devices = self.discovery.scan_subnet(subnet=self.subnet, progress_callback=progress_callback)
            self.scan_complete_signal.emit(devices)
            self.log_signal.emit(f"扫描完成，发现 {len(devices)} 台设备")


class MainWindow(QMainWindow):
    
    def __init__(self):
        super().__init__()
        self.discovery = DeviceDiscovery()
        self.database = Database()
        self.data_uploader = DataUploader(
            retry_times=config.upload_retry_times,
            retry_delay=config.upload_retry_delay,
        )
        
        # 通用打印机（ZPL/TSPL 自动适配）
        printer_cfg = config.printer_config
        self.universal_printer = create_printer_from_config(printer_cfg)
        
        # 多设备状态管理（按 SN 索引）
        self.device_testers = {}      # {sn: DeviceTester}
        self.device_panels = {}        # {sn: DeviceDetailPanel}
        self.device_test_workers = {}  # {sn: {test_name: TestWorker}}
        self.device_auto_workers = {}  # {sn: AutoTestWorker}
        self.device_os_workers = {}    # {sn: OsVersionWorker}
        self.device_connect_workers = {}  # {sn: DeviceConnectWorker}

        self.discovery_worker = None
        self.test_results_window = None
        
        # 连接状态监控定时器
        self.connection_check_timer = QTimer()
        self.connection_check_timer.timeout.connect(self.check_device_connections)
        self.connection_check_timer.start(5000)  # 每5秒检查一次

        # 配置文件热加载定时器：外部改完 config.yaml 自动生效，不用重启
        self.config_watch_timer = QTimer()
        self.config_watch_timer.timeout.connect(self._watch_config_file)
        self.config_watch_timer.start(2000)  # 每2秒检查一次

        self.init_ui()
        self.connect_signals()
        from PyQt5.QtCore import Qt as QtConnectionType
        self.discovery.device_discovered.connect(
            self.on_device_discovered,
            QtConnectionType.QueuedConnection
        )

        # 自动发现已禁用，使用手动刷新
        # self.discovery_timer = QTimer()
        # self.discovery_timer.timeout.connect(self.start_mdns_discovery)
        # self.discovery_timer.start(10000)
        # self.start_mdns_discovery()
    
    def init_ui(self):
        self.setWindowTitle(f"主控设备产测工具 - {AppConfig().app_version}")
        self.setGeometry(100, 100, 1400, 800)
        
        # 创建菜单栏
        menubar = self.menuBar()

        
        # 设置菜单
        settings_menu = menubar.addMenu('设置')
        
        printer_action = settings_menu.addAction('🖨️ 打印机配置')
        printer_action.triggered.connect(self.on_select_printer)

            # 查看菜单
        view_menu = menubar.addMenu('查看')
        
        test_results_action = view_menu.addAction('📊 测试结果')
        test_results_action.triggered.connect(self.on_test_results_clicked)
        
        # 设置菜单栏样式（确保在不同主题下可见）
        self.setStyleSheet("""
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
                background-color: #5D7491;
                border-radius: 4px;
            }
            QMenu {
                background-color: #4A5F7A;
                color: white;
                border: 1px solid #5D7491;
            }
            QMenu::item {
                padding: 6px 20px;
            }
            QMenu::item:selected {
                background-color: #3498db;
            }
        """)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout()
        
        splitter = QSplitter(Qt.Horizontal)
        
        self.device_list_panel = DeviceListPanel()
        self.device_list_panel.setMinimumWidth(350)
        self.device_list_panel.setMaximumWidth(500)
        splitter.addWidget(self.device_list_panel)
        
        # 右侧改为 Tab 页，每个设备一个 Tab
        self.device_tabs = QTabWidget()
        self.device_tabs.setTabsClosable(True)
        self.device_tabs.tabCloseRequested.connect(self.on_tab_close_requested)
        splitter.addWidget(self.device_tabs)
        
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        
        main_layout.addWidget(splitter)
        central_widget.setLayout(main_layout)
        
        self.statusBar().showMessage("就绪")
    
    def connect_signals(self):
        self.device_list_panel.device_selected.connect(self.on_device_selected)
        self.device_list_panel.device_deleted.connect(self.on_device_deleted)
        self.device_list_panel.device_reconnect_requested.connect(self.on_device_reconnect)
        self.device_list_panel.refresh_btn.clicked.connect(self.start_mdns_discovery)
        self.device_list_panel.scan_btn.clicked.connect(self.start_subnet_scan)
    
    def check_device_connections(self):
        """定期检查所有已连接设备的连接状态"""
        for sn, tester in list(self.device_testers.items()):
            if not tester.is_connected():
                logger.warning(f"检测到设备 {sn} 连接已断开")
                # 更新设备列表状态
                self.device_list_panel.update_device_status(sn, "disconnected")
                # 可选：显示通知
                self.statusBar().showMessage(f"设备 {sn} 连接已断开", 3000)
    
    def on_device_discovered(self, device: MasterDevice):
        logger.info(f"主窗口接收到设备发现信号: {device.sn} ({device.ip})")
        logger.info(f"发现新设备: {device.sn} ({device.ip})")
        self.device_list_panel.add_device(device)
        logger.info(f"设备已添加到面板: {device.sn}")
        self.statusBar().showMessage(f"发现设备: {device.sn}")
    
    def on_device_selected(self, sn: str):
        logger.info(f"选中设备: {sn}")

        device = self.discovery.devices.get(sn)
        if not device:
            QMessageBox.warning(self, "设备不存在", f"设备 {sn} 不存在")
            return

        # 已有 tab 则直接切换过去
        if sn in self.device_panels:
            panel = self.device_panels[sn]
            idx = self.device_tabs.indexOf(panel)
            if idx >= 0:
                self.device_tabs.setCurrentIndex(idx)
            return

        # 如果正在连接中，提示用户
        if sn in self.device_connect_workers:
            self.statusBar().showMessage(f"设备 {sn} 正在连接中，请稍候...", 2000)
            return

        # 异步连接设备
        self.statusBar().showMessage(f"正在连接设备 {sn}...")
        worker = DeviceConnectWorker(device)
        worker.success_signal.connect(self._on_device_connect_success)
        worker.failed_signal.connect(self._on_device_connect_failed)
        worker.log_signal.connect(lambda msg: logger.info(msg))
        worker.finished.connect(lambda: self._cleanup_connect_worker(sn))

        self.device_connect_workers[sn] = worker
        worker.start()

    def _on_device_connect_success(self, sn: str, tester):
        """设备连接成功回调"""
        device = self.discovery.devices.get(sn)
        if not device:
            # 设备已被「刷新发现」/「子网扫描」清空，丢弃这次过期连接，避免凭空冒出新 Tab
            logger.info(f"设备 {sn} 已不在当前列表中，丢弃本次连接结果")
            try:
                tester.disconnect()
            except Exception as e:
                logger.warning(f"释放过期连接失败 {sn}: {e}")
            return

        self.device_testers[sn] = tester
        self.device_test_workers[sn] = {}

        # 创建独立详情面板
        panel = DeviceDetailPanel()
        panel.set_device(device.sn, device.ip)
        panel.set_required_tests(self._get_required_tests(tester))
        panel.set_require_all_passed(config.print_require_all_passed)
        panel.test_item_clicked.connect(self.on_test_item_clicked)
        panel.auto_test_clicked.connect(self.on_auto_test_clicked)
        panel.test_results_clicked.connect(self.on_test_results_clicked)
        panel.print_label_clicked.connect(self.on_print_label)
        panel.shutdown_device_clicked.connect(self.on_shutdown_device)
        panel.switch_normal_mode_clicked.connect(self.on_switch_normal_mode)
        self.device_panels[sn] = panel

        # 添加 Tab，短 SN 显示
        short_sn = sn.split('-')[0] + '-...' + sn[-4:] if len(sn) > 12 else sn
        idx = self.device_tabs.addTab(panel, short_sn)
        self.device_tabs.setTabToolTip(idx, sn)
        self.device_tabs.setCurrentIndex(idx)

        # 更新设备状态为已连接
        self.device_list_panel.update_device_status(sn, "connected")
        self.statusBar().showMessage(f"已连接设备: {device.sn} ({device.ip})", 3000)
        logger.info(f"成功连接设备: {device.sn}")
        self._load_device_test_results(sn, panel)

        # 异步获取OS版本
        os_worker = OsVersionWorker(sn, tester)
        os_worker.result_signal.connect(self._on_os_version_received)
        self.device_os_workers[sn] = os_worker
        os_worker.start()

    def _on_device_connect_failed(self, sn: str, error_msg: str):
        """设备连接失败回调"""
        QMessageBox.warning(self, "连接失败", f"无法连接到设备 {sn}\n\n错误: {error_msg}")
        logger.error(f"连接设备失败: {sn} - {error_msg}")
        # 更新设备状态为断开
        self.device_list_panel.update_device_status(sn, "disconnected")
        self.statusBar().showMessage(f"连接失败: {sn}", 3000)

    def _get_required_tests(self, tester) -> list:
        """根据芯片能力返回必须全部通过的检测项列表"""
        required = ['poe', 'disk', 'fan', 'lte_4g', 'battery', 'wifi', 'bluetooth', 'nearlink', 'mac_write']
        try:
            if tester and tester.chip_config.has_hdmi:
                required.insert(3, 'hdmi')
        except Exception as e:
            logger.warning(f"获取芯片配置失败，使用默认检测项列表: {e}")
        return required

    def _cleanup_connect_worker(self, sn: str):
        """清理连接worker"""
        self.device_connect_workers.pop(sn, None)

    def on_tab_close_requested(self, index: int):
        """关闭 Tab 时断开对应设备连接"""
        panel = self.device_tabs.widget(index)
        sn = None
        for s, p in self.device_panels.items():
            if p is panel:
                sn = s
                break
        if sn:
            self._cleanup_device(sn)
        self.device_tabs.removeTab(index)

    def _stop_worker(self, worker, name: str, sn: str):
        """安全停止一个 QThread worker

        注意：绝对不能只调 quit()+wait() 就放手。
        wait() 超时只是返回 False，QThread 对象一旦失去引用被销毁，
        Qt 会 qFatal("QThread: Destroyed while thread is still running") 直接 abort 进程。
        所以这里必须循环等待，直到线程真的结束才允许返回。
        """
        if worker is None or not worker.isRunning():
            return

        # 先请求停止（AutoTestWorker 有 stop()，其余 worker 只支持 quit()）
        if hasattr(worker, 'stop'):
            try:
                worker.stop()
            except Exception as e:
                logger.warning(f"停止 {name} 时出错: {e}")

        worker.quit()
        if worker.wait(5000):
            return

        # 还没结束，继续等。测试命令本身可能很长，不能强行放弃
        logger.warning(f"worker {name}({sn}) 5秒未结束，继续等待...")
        for i in range(10):
            if worker.wait(3000):
                logger.info(f"worker {name}({sn}) 已结束")
                return
            logger.warning(f"worker {name}({sn}) 仍在运行，已等待 {(i + 1) * 3 + 5}秒")

        # 极端情况下仍未停止：保持引用防止被 GC 销毁（宁可泄漏也不能崩进程）
        logger.error(f"worker {name}({sn}) 长时间未结束，保留引用以避免进程崩溃")
        if not hasattr(self, '_zombie_workers'):
            self._zombie_workers = []
        self._zombie_workers.append(worker)

    def _cleanup_device(self, sn: str):
        """清理设备的所有资源"""
        # 停止正在运行的 worker（每个都必须等到真正结束）
        auto_worker = self.device_auto_workers.pop(sn, None)
        if auto_worker:
            self._stop_worker(auto_worker, "AutoTestWorker", sn)
            auto_worker.join_sub_workers(timeout=5.0)

        for test_name, worker in self.device_test_workers.pop(sn, {}).items():
            self._stop_worker(worker, f"TestWorker[{test_name}]", sn)

        os_worker = self.device_os_workers.pop(sn, None)
        if os_worker:
            self._stop_worker(os_worker, "OsVersionWorker", sn)

        # 断开 SSH
        tester = self.device_testers.pop(sn, None)
        if tester:
            tester.disconnect()

        self.device_panels.pop(sn, None)
        # 更新设备状态为已断开
        self.device_list_panel.update_device_status(sn, "disconnected")
        logger.info(f"已清理设备资源: {sn}")
    
    def on_device_reconnect(self, sn: str):
        """处理设备重连请求"""
        logger.info(f"收到设备重连请求: {sn}")
        
        device = self.discovery.devices.get(sn)
        if not device:
            QMessageBox.warning(self, "设备不存在", f"设备 {sn} 不存在")
            return

        # 如果正在连接中，提示用户
        if sn in self.device_connect_workers:
            self.statusBar().showMessage(f"设备 {sn} 正在连接中，请稍候...", 2000)
            return

        # 如果已有 tester，先断开
        if sn in self.device_testers:
            old_tester = self.device_testers[sn]
            old_tester.disconnect()
            logger.info(f"已断开旧连接: {sn}")
        
        # 异步重新连接
        self.statusBar().showMessage(f"正在重连设备: {sn}...")
        worker = DeviceConnectWorker(device)
        worker.success_signal.connect(self._on_device_reconnect_success)
        worker.failed_signal.connect(self._on_device_reconnect_failed)
        worker.log_signal.connect(lambda msg: logger.info(msg))
        worker.finished.connect(lambda: self._cleanup_connect_worker(sn))
        
        self.device_connect_workers[sn] = worker
        worker.start()

    def _on_device_reconnect_success(self, sn: str, tester):
        """设备重连成功回调"""
        self.device_testers[sn] = tester
        # 重连后这些字典可能已被 _cleanup_device 清掉，必须重新初始化，
        # 否则后续点检测项会在 self.device_test_workers[sn][...] 上 KeyError
        self.device_test_workers.setdefault(sn, {})
        self.device_list_panel.update_device_status(sn, "connected")
        self.statusBar().showMessage(f"重连成功: {sn}", 3000)
        logger.info(f"设备重连成功: {sn}")

        # 如果已有详情面板，刷新测试结果
        if sn in self.device_panels:
            panel = self.device_panels[sn]
            self._load_device_test_results(sn, panel)

    def _on_device_reconnect_failed(self, sn: str, error_msg: str):
        """设备重连失败回调"""
        device = self.discovery.devices.get(sn)
        self.device_list_panel.update_device_status(sn, "disconnected")
        self.statusBar().showMessage(f"重连失败: {sn}", 3000)
        logger.error(f"设备重连失败: {sn} - {error_msg}")

        ip_info = f"\nIP: {device.ip}" if device else ""
        QMessageBox.warning(self, "重连失败", f"无法重新连接到设备 {sn}{ip_info}\n\n错误: {error_msg}")
    
    def on_device_deleted(self, sn: str):
        reply = QMessageBox.question(
            self, '确认删除', 
            f'确定要删除设备 {sn} 吗？',
            QMessageBox.Yes | QMessageBox.No, 
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # 清理设备资源（包括关闭 tab）
            if sn in self.device_panels:
                panel = self.device_panels[sn]
                idx = self.device_tabs.indexOf(panel)
                if idx >= 0:
                    self.device_tabs.removeTab(idx)
                self._cleanup_device(sn)
            
            self.discovery.remove_device(sn)
            self.device_list_panel.remove_device(sn)
            logger.info(f"删除设备: {sn}")
            self.statusBar().showMessage(f"已删除设备: {sn}")
    
    def _clear_device_list(self):
        """清空设备列表：关闭所有详情 Tab、释放设备资源、清空发现缓存

        每次「刷新发现」或「子网扫描」前调用，避免新旧设备混在一起。
        正在连接中的 DeviceConnectWorker 不在这里强杀（quit() 对它无效，
        等待会卡住界面），改为在 _on_device_connect_success 里丢弃结果。
        """
        # 先释放已连接设备的资源（worker、SSH、tester），再关 Tab
        for sn in list(self.device_panels.keys()):
            self._cleanup_device(sn)

        while self.device_tabs.count():
            self.device_tabs.removeTab(0)

        self.discovery.clear_devices()
        self.device_list_panel.clear_devices()
        logger.info("设备列表已清空")
        self.statusBar().showMessage("设备列表已清空")

    def start_mdns_discovery(self):
        if self.discovery_worker and self.discovery_worker.isRunning():
            return

        self._clear_device_list()
        self.statusBar().showMessage("启动 mDNS 设备发现...")
        self.discovery.restart_mdns_discovery()
        self.discovery_worker = DiscoveryWorker(self.discovery, "mdns")
        self.discovery_worker.finished.connect(lambda: self.statusBar().showMessage("mDNS 发现已启动"))
        self.discovery_worker.start()
    
    def start_subnet_scan(self):
        if self.discovery_worker and self.discovery_worker.isRunning():
            return

        from src.master_control.core.discovery import get_all_interfaces
        interfaces = get_all_interfaces()

        selected_subnet = None
        if len(interfaces) > 1:
            # 多个网卡，显示选择对话框
            dialog = QDialog(self)
            dialog.setWindowTitle("选择网卡")
            layout = QVBoxLayout(dialog)

            label = QLabel("检测到多个网卡，请选择要扫描的网卡：")
            layout.addWidget(label)

            list_widget = QListWidget()
            for iface in interfaces:
                item_text = f"{iface['name']} - {iface['ip']} ({iface['subnet']})"
                list_widget.addItem(item_text)
            list_widget.setCurrentRow(0)
            layout.addWidget(list_widget)

            button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            button_box.accepted.connect(dialog.accept)
            button_box.rejected.connect(dialog.reject)
            layout.addWidget(button_box)

            if dialog.exec_() == QDialog.Accepted:
                selected_index = list_widget.currentRow()
                if selected_index >= 0:
                    selected_subnet = interfaces[selected_index]['subnet']
            else:
                return  # 用户取消
        elif len(interfaces) == 1:
            selected_subnet = interfaces[0]['subnet']
        else:
            selected_subnet = None

        subnet_str = selected_subnet if selected_subnet else config.scan_subnet

        # 扫描前清空当前设备列表（放在网卡选择之后，取消选择时不清空）
        self._clear_device_list()

        self.statusBar().showMessage(f"正在扫描子网: {subnet_str}...")

        self.discovery_worker = DiscoveryWorker(self.discovery, "scan", subnet=selected_subnet)
        self.discovery_worker.progress_signal.connect(self.on_scan_progress)
        self.discovery_worker.scan_complete_signal.connect(self.on_scan_complete)
        self.discovery_worker.start()
    
    def on_scan_progress(self, scanned: int, total: int):
        self.statusBar().showMessage(f"正在扫描子网... ({scanned}/{total})")
    
    def on_scan_complete(self, devices: list):
        self.statusBar().showMessage(f"子网扫描完成，发现 {len(devices)} 台设备")
        
        if not devices:
            QMessageBox.information(self, "扫描结果", "未发现任何设备")
            return
        
        dialog = DeviceSelectionDialog(devices, self)
        if dialog.exec_() == QDialog.Accepted:
            selected_devices = dialog.get_selected_devices()
            for device_info in selected_devices:
                device = MasterDevice(
                    ip=device_info['ip'],
                    sn=device_info['sn'],
                    model=device_info['model'],
                    port=device_info['port'],
                    disk_model=device_info.get('disk_model', ''),
                    disk_size=device_info.get('disk_size', '')
                )
                self.discovery.add_device(device)
    
    def _on_os_version_received(self, sn: str, version: str):
        """OS版本获取完成，更新对应面板"""
        panel = self.device_panels.get(sn)
        if panel:
            panel.set_os_version(version)
        self.device_os_workers.pop(sn, None)
        logger.info(f"[{sn}] 系统版本: {version}")

    def _load_device_test_results(self, sn: str, panel: DeviceDetailPanel):
        """加载设备的历史测试结果并显示（仅用于参考展示，不参与打印门控）"""
        records = self.database.get_records_by_sn(sn)
        if not records:
            return

        type_map = {
            'POE': 'poe',
            'DISK': 'disk',
            '4G': 'lte_4g',
            'WIFI': 'wifi',
            'BLE': 'bluetooth',
            'SLE': 'nearlink',
            'MAC': 'mac_write',
            'FAN': 'fan',
            'HDMI': 'hdmi',
            'BAT': 'battery'
        }

        for record in records:
            test_name = type_map.get(record['sub_type'])
            if test_name:
                status = TestStatus.PASSED if record['results'] == 'PASS' else TestStatus.FAILED
                message = record.get('remarks', '')
                panel.load_history_result(test_name, status, message)

    def on_switch_normal_mode(self, sn: str):
        """切换到生产（正常）模式"""
        tester = self.device_testers.get(sn)
        if not tester:
            QMessageBox.warning(self, "未连接设备", f"设备 {sn} 未连接")
            return

        self.statusBar().showMessage(f"正在切换生产模式: {sn}...")
        existing = self.device_test_workers.get(sn, {}).get('_switch_mode')
        if existing and existing.isRunning():
            QMessageBox.warning(self, "操作进行中", f"设备 {sn} 正在切换模式，请稍候")
            return

        worker = SwitchModeWorker(tester)
        panel = self.device_panels.get(sn)
        if panel:
            worker.log_signal.connect(panel.append_log)
        worker.finished_signal.connect(lambda ok, msg, w=worker: self._on_switch_mode_finished(ok, msg, w, sn))
        worker.start()
        # 保持引用，防止被 GC；用 setdefault 兼容设备重连后未初始化的情况
        self.device_test_workers.setdefault(sn, {})['_switch_mode'] = worker

    def _on_switch_mode_finished(self, success: bool, message: str, worker=None, sn: str = None):
        # 释放引用并回收线程，避免长期累积
        if worker is not None:
            self._stop_worker(worker, "SwitchModeWorker", sn or "?")
            self.device_test_workers.get(sn, {}).pop('_switch_mode', None)

        if success:
            self.statusBar().showMessage(message)
            QMessageBox.information(self, "成功", message)
        else:
            self.statusBar().showMessage(message)
            QMessageBox.warning(self, "切换失败", message)

    def on_test_item_clicked(self, test_name: str, sn: str, ip: str):
        tester = self.device_testers.get(sn)
        if not tester:
            QMessageBox.warning(self, "未连接设备", f"设备 {sn} 未连接")
            return

        panel = self.device_panels.get(sn)
        if not panel:
            return

        # 同一检测项如果已在跑，直接覆盖字典里的引用会让旧 QThread 失去引用被 GC，
        # Qt 随即 qFatal 崩进程 —— 必须挡住重复点击
        running = self.device_test_workers.get(sn, {}).get(test_name)
        if running and running.isRunning():
            QMessageBox.warning(self, "测试进行中", f"设备 {sn} 的该检测项正在进行中")
            return

        logger.info(f"开始测试: {test_name} - {sn}")
        panel.update_test_result(test_name, TestStatus.TESTING)

        worker = TestWorker(test_name, tester)
        worker.log_signal.connect(panel.append_log)
        worker.result_signal.connect(self.on_test_result)
        worker.finished_signal.connect(lambda s, w=worker, t=test_name: self._on_single_test_finished(s, t, w))

        # setdefault：设备重连/清理后该键可能不存在，直接下标会 KeyError
        self.device_test_workers.setdefault(sn, {})[test_name] = worker
        worker.start()

    def _on_single_test_finished(self, sn: str, test_name: str, worker=None):
        """单项测试结束：回收引用并释放 QThread 对象"""
        if worker is not None:
            self._stop_worker(worker, f"TestWorker[{test_name}]", sn)
        self.device_test_workers.get(sn, {}).pop(test_name, None)
        logger.info(f"测试完成: {sn} / {test_name}")

    def on_auto_test_clicked(self, sn: str, ip: str):
        tester = self.device_testers.get(sn)
        if not tester:
            QMessageBox.warning(self, "未连接设备", f"设备 {sn} 未连接")
            return

        existing = self.device_auto_workers.get(sn)
        if existing and existing.isRunning():
            QMessageBox.warning(self, "测试进行中", f"设备 {sn} 的一键测试正在进行中")
            return

        panel = self.device_panels.get(sn)
        if panel:
            panel.clear_results()

        logger.info(f"开始一键测试: {sn}")

        worker = AutoTestWorker(tester)
        if panel:
            worker.log_signal.connect(panel.append_log)
        worker.result_signal.connect(self.on_test_result)
        worker.finished_signal.connect(lambda ok: self.on_auto_test_finished(sn, ok))
        self.device_auto_workers[sn] = worker
        worker.start()

    def on_auto_test_finished(self, sn: str, success: bool):
        msg = "一键测试完成" if success else f"[{sn}] 一键测试失败"
        self.statusBar().showMessage(msg)

    def on_test_result(self, test_name: str, sn: str, status: TestStatus, message: str, detail: str):
        # 路由到对应设备面板
        panel = self.device_panels.get(sn)
        if panel:
            panel.update_test_result(test_name, status, message, detail)

        # 测试中状态不写数据库
        if status == TestStatus.TESTING:
            return

        sub_type_map = {
            'poe': 'POE',
            'disk': 'DISK',
            'lte_4g': '4G',
            'wifi': 'WIFI',
            'bluetooth': 'BLE',
            'nearlink': 'SLE',
            'mac_write': 'MAC',
            'fan': 'FAN',
            'hdmi': 'HDMI',
            'battery': 'BAT'
        }
        sub_type = sub_type_map.get(test_name, test_name.upper())
        results = 'PASS' if status == TestStatus.PASSED else 'FAIL'

        self.database.save_test_result(sn, sub_type, results, message)
        logger.info(f"测试结果已保存: SN={sn}, 类型={sub_type}, 结果={results}")

    def _apply_print_gate_config(self):
        """把打印门控与上传重试配置同步到各设备面板（热加载时调用）"""
        require_all = config.print_require_all_passed
        for panel in self.device_panels.values():
            panel.set_require_all_passed(require_all)

        self.data_uploader.retry_times = config.upload_retry_times
        self.data_uploader.retry_delay = config.upload_retry_delay

    def _watch_config_file(self):
        """定时检查配置文件是否被外部修改，改了立刻重载并重建打印机

        这样直接编辑 config/config.yaml 就能生效，不需要重启应用，
        也不需要通过"打印机配置"对话框保存。
        """
        try:
            if not config.reload_if_changed():
                return

            logger.info(f"检测到配置文件变更，已重新加载: {config.config_path}")

            # 配置变了就重建打印机实例（协议/DPI/布局/打印机名都可能改了）
            self.universal_printer = create_printer_from_config(config.printer_config)
            self._apply_print_gate_config()
            self.statusBar().showMessage("配置文件已更新，配置实时生效", 3000)

        except Exception as e:
            logger.error(f"配置热加载失败: {e}", exc_info=True)

    def on_test_results_clicked(self):
        if not self.test_results_window:
            self.test_results_window = TestResultsWindow(self.database, self)
        self.test_results_window.show()
        self.test_results_window.raise_()
        self.test_results_window.activateWindow()
    
    def on_select_printer(self):
        """打开打印机配置对话框"""
        try:
            # 用 Config 解析出的同一份路径，避免对话框存的文件和程序读的文件不是同一个
            config_path = str(config.config_path)
            with open(config_path, 'r', encoding='utf-8') as f:
                current_config = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"读取配置文件失败: {e}")
            QMessageBox.critical(self, "错误", f"读取配置文件失败:\n{e}")
            return

        dialog = PrinterConfigDialog(current_config, self)
        if dialog.exec_() == QDialog.Accepted:
            # 重新加载配置（强制读盘）
            try:
                config.load_config(force=True)
            except Exception as e:
                logger.error(f"重新加载配置失败: {e}", exc_info=True)
                QMessageBox.critical(self, "错误", f"重新加载配置失败:\n{e}")
                return

            # 重新创建打印机实例，立即生效
            printer_cfg = config.printer_config
            self.universal_printer = create_printer_from_config(printer_cfg)

            logger.info("打印机配置已更新并重新加载（已立即生效）")
            self.statusBar().showMessage("打印机配置已更新，已立即生效", 3000)

    def on_print_label(self, sn: str):
        try:
            # 门控：默认要求所有检测项通过；printer.require_all_passed=false 时免检
            panel = self.device_panels.get(sn)
            if config.print_require_all_passed and panel and not panel.all_tests_passed():
                not_passed = panel.get_not_passed_display_names()
                QMessageBox.warning(
                    self, "无法打印标签",
                    f"设备 {sn} 以下检测项未通过，必须全部通过后才能打印标签：\n\n" +
                    "、".join(not_passed) +
                    "\n\n如确需免检打印，可在 config.yaml 中设置 "
                    "printer.require_all_passed: false"
                )
                logger.warning(f"[{sn}] 打印标签被拒绝，未通过项: {not_passed}")
                return
            if panel and not panel.all_tests_passed():
                logger.warning(
                    f"[{sn}] 已关闭检测项校验（printer.require_all_passed=false），"
                    f"仍允许打印，未通过项: {panel.get_not_passed_display_names()}")

            # ========== 步骤1：打印前 POE 拔除确认 ==========
            if config.print_confirm_poe_unplugged:
                reply = QMessageBox.question(
                    self, "打印前确认",
                    "请确认：\n\n已拔掉所有 POE 设备（摄像头等）\n\n是否继续打印？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No
                )
                if reply == QMessageBox.No:
                    self.statusBar().showMessage("已取消打印")
                    logger.info(f"[{sn}] 用户取消打印（未确认拔除POE设备）")
                    return

            # 获取设备所有测试记录
            records = self.database.get_records_by_sn(sn)
            if not records:
                QMessageBox.warning(self, "警告", "该设备没有测试数据")
                return

            # 上传测试数据
            self.statusBar().showMessage(f"正在上传测试数据: {sn}...")
            success, message = self.data_uploader.upload_test_data(records)

            if not success:
                self.statusBar().showMessage(f"上传失败: {message}")
                # 上传失败，询问是否继续打印
                reply = QMessageBox.question(
                    self, '上传失败',
                    f'{message}\n\n是否继续打印标签？',
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No
                )
                if reply == QMessageBox.No:
                    self.statusBar().showMessage("已取消打印")
                    return
            else:
                self.statusBar().showMessage(f"数据上传成功: {sn}")

            # 提取磁盘大小
            disk_size = None
            for record in records:
                if record['sub_type'] == 'DISK' and record['results'] == 'PASS':
                    remarks = record.get('remarks', '')
                    import re
                    match = re.search(r'(\d+(?:\.\d+)?[TGMK]B?)', remarks, re.IGNORECASE)
                    if match:
                        raw_size = match.group(1).upper()
                        disk_size = self._format_disk_capacity(raw_size)
                        break

            # 打印标签（统一使用 universal_printer，支持 ZPL/TSPL 自动适配）
            print_success = self.universal_printer.print_label(sn, disk_size)
            if not print_success:
                self.statusBar().showMessage("打印失败或功能未启用")
                logger.warning(f"[{sn}] 打印失败")
                return

            self.statusBar().showMessage(f"标签打印成功: {sn}")
            logger.info(f"标签打印成功: {sn}")

            # ========== 步骤2：打印成功后清除设备数据 ==========
            if config.print_cleanup_after_print:
                tester = self.device_testers.get(sn)
                if tester and tester.ssh and tester.ssh.connected:
                    self.statusBar().showMessage(f"正在清除设备数据: {sn}...")
                    cleanup_cmd = "rm -rf /data/wd-nvr/data/app.db* /data/zlmrecord/onvif/*"
                    logger.info(f"[{sn}] 执行清理命令: {cleanup_cmd}")
                    success, stdout, stderr = tester.ssh.execute_command(cleanup_cmd, timeout=10)
                    
                    if success:
                        self.statusBar().showMessage(f"设备数据清除成功: {sn}")
                        logger.info(f"[{sn}] 设备数据清除成功")
                    else:
                        logger.warning(f"[{sn}] 设备数据清除失败: {stderr}")
                        QMessageBox.warning(
                            self, "清除失败",
                            f"设备 {sn} 数据清除失败：\n{stderr}"
                        )
                else:
                    logger.warning(f"[{sn}] 设备未连接或 SSH 不可用，跳过清理步骤")
                    QMessageBox.warning(self, "清除失败", f"设备 {sn} 未连接，无法清除数据")
                    
        except Exception as e:
            QMessageBox.critical(self, "错误", f"标签打印失败: {e}")
            self.statusBar().showMessage(f"打印失败: {e}")
            logger.error(f"标签打印失败: {e}")
    
    def on_shutdown_device(self, sn: str):
        """关机按钮：点击后立即向设备发送关机指令"""
        try:
            tester = self.device_testers.get(sn)
            if not tester or not tester.ssh or not tester.ssh.connected:
                QMessageBox.warning(self, "无法关机", f"设备 {sn} 未连接")
                logger.warning(f"[{sn}] 关机失败：设备未连接")
                return

            logger.info(f"[{sn}] 执行关机命令...")
            # 重定向全部 fd 并后台执行，避免 paramiko 读取阻塞在未关闭的管道上
            shutdown_cmd = ("nohup shutdown -h now "
                            "> /dev/null 2>&1 < /dev/null &")
            tester.ssh.execute_command(shutdown_cmd, timeout=5)
            self.statusBar().showMessage(f"已向设备 {sn} 发送关机命令")
            logger.info(f"[{sn}] 关机命令已发送")

        except Exception as e:
            QMessageBox.critical(self, "错误", f"关机操作失败: {e}")
            logger.error(f"[{sn}] 关机操作异常: {e}")
    
    def _format_disk_capacity(self, raw_size: str) -> str:
        """
        将实际硬盘容量格式化为标称容量
        
        Args:
            raw_size: 原始容量字符串，如 '477G', '1.8T', '931.5G'
            
        Returns:
            标称容量字符串，如 '512G', '2T', '1T'
        """
        import re
        
        # 解析数值和单位
        match = re.match(r'(\d+(?:\.\d+)?)\s*([TGMK])B?', raw_size.upper())
        if not match:
            return raw_size  # 无法解析，返回原值
        
        value = float(match.group(1))
        unit = match.group(2)
        
        # 统一转换为 GB
        if unit == 'T':
            value_gb = value * 1024
        elif unit == 'G':
            value_gb = value
        elif unit == 'M':
            value_gb = value / 1024
        elif unit == 'K':
            value_gb = value / (1024 * 1024)
        else:
            return raw_size
        
        # 映射到标称容量（考虑实际容量约为标称的 93%）
        # 512G 实际约 477G, 1T 实际约 931G, 2T 实际约 1.8T
        if value_gb < 600:  # < 600GB → 512G
            return '512G'
        elif value_gb < 1200:  # 600GB ~ 1200GB → 1T
            return '1T'
        elif value_gb < 2400:  # 1.2TB ~ 2.4TB → 2T
            return '2T'
        elif value_gb < 5000:  # 2.4TB ~ 5TB → 4T
            return '4T'
        elif value_gb < 10000:  # 5TB ~ 10TB → 8T
            return '8T'
        else:  # >= 10TB
            # 返回原值（TB 单位）
            if unit == 'T':
                return f"{int(value)}T"
            else:
                return f"{int(value_gb / 1024)}T"
    
    def closeEvent(self, event):
        # 先停定时器，避免关闭过程中又有回调进来
        self.connection_check_timer.stop()

        # 清理所有设备连接
        for sn in list(self.device_testers.keys()):
            self._cleanup_device(sn)

        # 清理残留的连接 worker
        for sn in list(self.device_connect_workers.keys()):
            worker = self.device_connect_workers.pop(sn, None)
            self._stop_worker(worker, "DeviceConnectWorker", sn)

        if self.discovery_worker and self.discovery_worker.isRunning():
            self.discovery_worker.quit()
            self.discovery_worker.wait(5000)

        self.discovery.stop_mdns_discovery()
        self.database.close()
        event.accept()


class DeviceSelectionDialog(QDialog):
    
    def __init__(self, devices: list, parent=None):
        super().__init__(parent)
        self.devices = devices
        self.init_ui()
    
    def init_ui(self):
        self.setWindowTitle("选择设备")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        
        layout = QVBoxLayout()
        
        label = QLabel(f"发现 {len(self.devices)} 台设备，请选择要添加的设备：")
        layout.addWidget(label)
        
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.MultiSelection)
        
        for device in self.devices:
            port_info = ""
            if 'open_ports' in device and device['open_ports']:
                port_info = f" [端口: {','.join(map(str, device['open_ports']))}]"
            item_text = f"{device['sn']} - {device['ip']} ({device['model']}){port_info}"
            item = QListWidgetItem(item_text)
            item.setData(Qt.UserRole, device)
            self.list_widget.addItem(item)
            # 默认不选中
            # item.setSelected(True)
        
        layout.addWidget(self.list_widget)
        
        # 添加全选/全不选按钮
        button_layout = QHBoxLayout()
        
        select_all_btn = QPushButton("全选")
        select_all_btn.clicked.connect(self.select_all)
        button_layout.addWidget(select_all_btn)
        
        deselect_all_btn = QPushButton("全不选")
        deselect_all_btn.clicked.connect(self.deselect_all)
        button_layout.addWidget(deselect_all_btn)
        
        button_layout.addStretch()
        layout.addLayout(button_layout)
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
        self.setLayout(layout)
    
    def select_all(self):
        """全选所有设备"""
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setSelected(True)
    
    def deselect_all(self):
        """取消选择所有设备"""
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setSelected(False)
    
    def get_selected_devices(self) -> list:
        selected = []
        for item in self.list_widget.selectedItems():
            selected.append(item.data(Qt.UserRole))
        return selected
