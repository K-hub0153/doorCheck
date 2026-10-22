import csv
import sys
import time
import queue
import threading
import requests
from typing import Callable, Optional, Dict
from .test_result import TestResult, TestStatus
from ..network.mqtt_client import MQTTClient
from .protocol import (OpenDoorMessage, CloseDoorMessage, QueryStatusMessage,
                               OTAUpgradeMessage, RemotePairingMessage,
                               WriteWifiBleMacMessage, ReadWifiBleMaxMessage,
                               WriteSleMaxMessage, ReadSleMaxMessage,
                               ResetConfigMessage,
                               BleDiscoverMessage, SleDiscoverMessage, WifiDiscoverMessage)
from ..utils.logger import logger
from ..utils.config import Config
from ..utils.paths import get_app_dir


class TestEngine:
    # 遥控器配对的设备侧硬件时序（固件 1.1.4 实测，见设备日志）：
    #   1. 收到 remote_pairing 后，设备保持 GPIO8 共 duration 毫秒（此阶段按键无效）
    #   2. 之后才开启 GPIO3 采集窗口，固定 10 秒，与 duration 无关
    #      设备日志原文: "GPIO3 requires 3 highs in 10000 ms"
    #   3. 窗口结束（或按满次数）才发 remote_pairing_result 通知
    # 因此结果通知最早在 duration + 10s 后到达，等待超时不能按 duration 推算。
    PAIRING_KEY_WINDOW_SECS = 10   # 设备侧 GPIO3 采集窗口，固定值
    PAIRING_REQUIRED_PRESSES = 3   # 窗口内需要的按键次数
    PAIRING_RESULT_MARGIN_SECS = 5  # 通知传输 + 处理余量

    def __init__(self, mqtt_client: MQTTClient, config: Config, device_hw_ver: str = None):
        self.mqtt_client = mqtt_client
        self.config = config
        self.result = TestResult()
        self.emergency_event = threading.Event()
        self.remote_open_event = threading.Event()
        self.remote_pairing_result = None  # 存储配对结果
        self.device_offline_event = threading.Event()
        self.on_progress_callback = None
        self.on_test_item_callback = None
        self.device_hw_ver = device_hw_ver or ""

        # 硬件状态监控
        self.hardware_status_mismatch = False  # 硬件状态不一致标志
        self.last_hardware_check_time = 0  # 最后一次检查时间

        self.mqtt_client.register_callback("test_engine", self._on_message_received)

    def _on_message_received(self, topic: str, message: Dict):
        logger.debug(f"测试引擎收到消息: {topic}")
        header_device = message.get('header', {}).get('device', {})
        if header_device.get('sn') == self.mqtt_client.device_sn:
            hw_ver = header_device.get('hw_ver', '')
            if hw_ver:
                self.device_hw_ver = hw_ver

        if "event" in topic:
            action = message.get('header', {}).get('action', '')
            if action == "emergency_switch":
                emergency_status = message.get('body', {}).get('emergencyStatus', '')
                logger.info(f"收到应急开关事件通知: emergencyStatus={emergency_status}")
                if str(emergency_status) == "1":
                    self.emergency_event.set()
            elif action == "remote_pairing_result":
                result = message.get('body', {}).get('result', '')
                logger.info(f"收到遥控器配对结果通知: result={result}")
                self.remote_pairing_result = result
            elif action == "remote_open":
                logger.info(f"收到遥控器开门事件通知")
                self.remote_open_event.set()
            elif action == "offline":
                reason = message.get('body', {}).get('reason', '')
                logger.warning(f"设备离线事件: reason={reason}")
                self.device_offline_event.set()

        # 监听设备日志消息，检测硬件状态不一致
        elif "log" in topic:
            self._check_device_logs(message)

    def _check_device_logs(self, message: Dict):
        """检查设备日志中是否有硬件状态不一致的警告"""
        body = message.get('body', {})
        logs = body.get('logs', [])

        for log_entry in logs:
            log_message = log_entry.get('message', '')
            log_level = log_entry.get('level', '')

            # 检测 "Door status mismatch" 警告
            if 'Door status mismatch' in log_message or 'door status mismatch' in log_message.lower():
                logger.warning(f"⚠️ 检测到硬件状态不一致: {log_message}")
                self.hardware_status_mismatch = True
                self.last_hardware_check_time = time.time()

    def _verify_hardware_status(self) -> bool:
        """
        验证硬件状态是否正常

        Returns:
            True: 硬件状态正常
            False: 硬件状态异常（检测到状态不一致）
        """
        # 清除旧的硬件状态标志（只检查最近5秒内的日志）
        current_time = time.time()
        if self.last_hardware_check_time > 0 and (current_time - self.last_hardware_check_time) > 5:
            # 5秒前的警告可以忽略
            self.hardware_status_mismatch = False

        if self.hardware_status_mismatch:
            logger.error("❌ 硬件状态异常：检测到门锁状态不一致（isr!=hw），请检查电机或电磁锁硬件")
            return False

        logger.info("✓ 硬件状态正常")
        return True

    def close(self):
        """释放测试引擎注册的MQTT回调，避免后续测试被旧回调干扰。"""
        self.mqtt_client.unregister_callback("test_engine")

    def _send_and_wait(self, msg_obj, timeout: int = None) -> Optional[Dict]:
        """发送消息并等待对应 mid 的响应"""
        if timeout is None:
            timeout = self.config.test_timeout
        payload = msg_obj.to_json()
        return self.mqtt_client.request(payload, msg_obj.mid, timeout=timeout)

    def _query_door_state(self, timeout: int = 5) -> Optional[str]:
        # 检查设备是否在线
        if self.device_offline_event.is_set():
            logger.error("设备已离线，无法查询状态")
            return None

        query_msg = QueryStatusMessage(self.config.device_psk)
        response = self._send_and_wait(query_msg, timeout)
        if not response:
            # 检查是否因为设备离线导致超时
            if self.device_offline_event.is_set():
                logger.error("查询状态失败：设备已离线")
            else:
                logger.error("查询状态超时")
            return None

        body = response.get('body', {})
        actual_state = body.get('status', '')
        logger.info(f"当前状态: {actual_state}")
        return actual_state

    def _verify_door_state(self, expected_state: str, timeout: int = 5) -> bool:
        logger.info(f"验证门锁状态，期望: {expected_state}")
        actual_state = self._query_door_state(timeout)

        state_mapping = {
            "opened": ["opened", "unlocked"],
            "closed": ["closed", "locked"]
        }

        expected_states = state_mapping.get(expected_state, [expected_state])
        is_match = actual_state in expected_states

        if is_match:
            logger.info(f"✓ 状态验证通过: {actual_state} 匹配 {expected_state}")
        else:
            logger.error(f"✗ 状态验证失败: {actual_state} 不匹配 {expected_state}")

        return is_match

    def _report_progress(self, message: str):
        logger.info(message)
        if self.on_progress_callback:
            self.on_progress_callback(message)

    def _report_test_item(self, test_name: str, status: str, message: str = ""):
        if self.on_test_item_callback:
            self.on_test_item_callback(test_name, status, message)

    @staticmethod
    def _version_tuple(version: str) -> tuple:
        if not version:
            return ()
        parts = []
        for part in str(version).strip().lstrip('vV').split('.'):
            digits = ''.join(ch for ch in part if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        return tuple(parts)

    @classmethod
    def _version_gte(cls, version: str, minimum: str) -> bool:
        current = list(cls._version_tuple(version))
        target = list(cls._version_tuple(minimum))
        width = max(len(current), len(target))
        current.extend([0] * (width - len(current)))
        target.extend([0] * (width - len(target)))
        return tuple(current) >= tuple(target)

    def test_hardware_version(self, min_version: str = "1.1") -> bool:
        self._report_progress(f"【前置检查】硬件版本检查，要求 >= {min_version}")

        if not self.device_hw_ver:
            message = "未获取到设备硬件版本 hw_ver"
            self.result.add_step("硬件版本检查", False, message)
            self._report_progress(f"❌ {message}")
            return False

        passed = self._version_gte(self.device_hw_ver, min_version)
        message = f"当前硬件版本 {self.device_hw_ver}，要求 >= {min_version}"
        self.result.add_step("硬件版本检查", passed, message)
        self._report_progress(("✅ " if passed else "❌ ") + message)
        return passed

    def test_open_door(self) -> bool:
        self._report_progress("【步骤1】测试开锁功能")

        open_msg = OpenDoorMessage(self.config.device_psk, self.config.test_open_duration)
        response = self._send_and_wait(open_msg)
        if not response:
            self.result.add_step("等待开锁响应", False, "超时")
            return False

        self.result.add_step("等待开锁响应", True)
        time.sleep(1)

        if not self._verify_door_state("opened"):
            self.result.add_step("验证开锁状态", False, "状态不符合预期")
            return False

        self.result.add_step("验证开锁状态", True)
        return True

    def test_close_door(self) -> bool:
        self._report_progress("【步骤2】测试关锁功能")

        close_msg = CloseDoorMessage(self.config.device_psk)
        response = self._send_and_wait(close_msg)
        if not response:
            self.result.add_step("等待关锁响应", False, "超时")
            return False

        self.result.add_step("等待关锁响应", True)
        time.sleep(1)

        if not self._verify_door_state("closed"):
            self.result.add_step("验证关锁状态", False, "状态不符合预期")
            return False

        self.result.add_step("验证关锁状态", True)
        return True

    def run_full_test(self, report_callback: Callable = None) -> TestResult:
        self.result = TestResult()
        self.result.status = TestStatus.RUNNING
        self.result.start_time = time.time()
        failed_tests = []

        # 清除离线标志
        self.device_offline_event.clear()

        # 等待MQTT连接稳定（避免设备刚连接就收到命令导致崩溃）
        logger.info("等待MQTT连接稳定...")
        time.sleep(0.5)

        try:
            self._report_progress("开始产测流程...")

            t0 = time.time()
            ok = self.test_hardware_version("1.1")
            hw_message = self.result.steps[-1]['message'] if self.result.steps else ""
            self.result.sub_results.append({
                'test_type': '硬件版本检查',
                'status': 'passed' if ok else 'failed',
                'duration': round(time.time() - t0, 2),
                'steps': [self.result.steps[-1]] if self.result.steps else [],
            })
            if not ok:
                fail_message = "硬件版本检查未通过"
                self._report_progress(f"\n❌ {fail_message}")
                self.result.set_failed(fail_message)
                return self.result

            # 蓝牙检测作为首个测试项，硬件版本前置检查仍保持在最前。
            self._report_test_item("ble_discover", "testing")
            t0 = time.time()
            step_start = len(self.result.steps)
            ok = self.test_ble_discover()
            new_steps = self.result.steps[step_start:]
            detail = new_steps[-1]['message'] if new_steps else ""
            self._report_test_item("ble_discover", "passed" if ok else "failed", detail)
            if not ok:
                failed_tests.append("BLE检测")
            self.result.sub_results.append({
                'test_type': 'BLE检测',
                'status': 'passed' if ok else 'failed',
                'duration': round(time.time() - t0, 2), 'steps': new_steps,
            })

            # ── 阶段一：控制检测 ─────────────────────────────────────────
            # 注意：遥控器配对测试放在整个流程最后执行（见阶段三）。
            # 它会让门锁保持开启，而后续测试项都不依赖门锁处于关闭状态，
            # 放到最后就无需再等门锁自动落锁，可省去约30秒等待。
            self._report_test_item("emergency_switch", "testing")
            t0 = time.time()
            ok = self.test_emergency_switch(timeout=10, report_callback=report_callback)
            self._report_test_item("emergency_switch", "passed" if ok else "failed")
            if not ok:
                failed_tests.append("应急开关测试")
            if report_callback:
                report_callback("hide_dialog", 0)
            self.result.sub_results.append({
                'test_type': '应急开关测试',
                'status': 'passed' if ok else 'failed',
                'duration': round(time.time() - t0, 2), 'steps': [],
            })

            # ── 阶段二：无线检测 ─────────────────────────────────────────
            self._report_test_item("burn_mac", "testing")
            t0 = time.time()
            burn_ok, burn_msg = self.burn_mac_addresses(
                self.mqtt_client.device_sn, self._report_progress)
            self.result.add_step("烧写MAC地址", burn_ok, burn_msg)
            self._report_test_item("burn_mac", "passed" if burn_ok else "failed", burn_msg)
            if not burn_ok:
                failed_tests.append(f"MAC地址烧写: {burn_msg}")
            self.result.sub_results.append({
                'test_type': '烧写MAC',
                'status': 'passed' if burn_ok else 'failed',
                'duration': round(time.time() - t0, 2), 'steps': [],
            })

            for test_name, test_fn, label in [
                ("wifi_discover", self.test_wifi_discover, "WiFi检测"),
                ("sle_discover", self.test_sle_discover, "SLE检测"),
            ]:
                self._report_test_item(test_name, "testing")
                t0 = time.time()
                step_start = len(self.result.steps)
                ok = test_fn()
                new_steps = self.result.steps[step_start:]
                detail = new_steps[-1]['message'] if new_steps else ""
                self._report_test_item(test_name, "passed" if ok else "failed", detail)
                if not ok:
                    failed_tests.append(label)
                self.result.sub_results.append({
                    'test_type': label,
                    'status': 'passed' if ok else 'failed',
                    'duration': round(time.time() - t0, 2), 'steps': new_steps,
                })

            # ── 阶段三：遥控器配对（放最后，测完即结束，无需等门锁落锁）─────
            self._report_test_item("remote_pairing", "testing")
            t0 = time.time()
            ok = self.test_remote_pairing(pairing_duration=2000, open_timeout=10,
                                          report_callback=report_callback)
            self._report_test_item("remote_pairing", "passed" if ok else "failed")
            if report_callback:
                report_callback("hide_dialog", 0)
            if not ok:
                failed_tests.append("遥控器配对测试")
            self.result.sub_results.append({
                'test_type': '遥控器配对测试',
                'status': 'passed' if ok else 'failed',
                'duration': round(time.time() - t0, 2), 'steps': [],
            })

            if failed_tests:
                fail_message = "以下测试项未通过: " + ", ".join(failed_tests)
                self._report_progress(f"\n❌ {fail_message}")
                self.result.set_failed(fail_message)
            else:
                self._report_progress("\n✅ 所有测试通过！")
                self.result.set_passed()

        except Exception as e:
            logger.error(f"测试异常: {e}")
            self.result.set_failed(f"测试异常: {str(e)}")

        return self.result

    def set_progress_callback(self, callback: Callable):
        self.on_progress_callback = callback

    def set_test_item_callback(self, callback: Callable):
        self.on_test_item_callback = callback

    def test_remote_pairing(self, pairing_duration: int = 2000, open_timeout: int = 8,
                            report_callback: Callable = None, initial_state: str = None) -> bool:
        self._report_progress("测试遥控器配对")

        # 清除离线标志和遥控器事件标志
        self.device_offline_event.clear()
        self.remote_open_event.clear()

        # 如果没有传入初始状态，说明是单独调用，需要等待连接稳定
        if initial_state is None:
            logger.info("等待MQTT连接稳定...")
            time.sleep(0.5)

        current_state = initial_state or self._query_door_state()
        if not current_state:
            if self.device_offline_event.is_set():
                self.result.add_step("检查初始门锁状态", False, "设备已离线")
            else:
                self.result.add_step("检查初始门锁状态", False, "查询状态失败")
            return False

        logger.info(f"当前门锁状态: {current_state}")

        # 测试前校验：如果门是开的，先强制上锁
        if current_state in ["opened", "unlocked"]:
            logger.info("门锁处于开启状态，强制上锁")
            self._report_progress("门锁处于开启状态，强制上锁...")

            close_msg = CloseDoorMessage(self.config.device_psk)
            response = self._send_and_wait(close_msg)
            if not response:
                self.result.add_step("强制上锁响应", False, "超时")
                return False

            # 发送close命令后等待一段时间，让门锁完成关门动作
            logger.info("等待门锁完成关门动作...")
            time.sleep(3)

            self.result.add_step("测试前门锁状态", True, "门已强制上锁")
        else:
            self.result.add_step("测试前门锁状态", True, "门锁已处于关闭状态")

        # 【新增】硬件状态检查：验证门锁硬件是否真正锁上
        logger.info("检查硬件状态...")
        self._report_progress("检查硬件状态...")

        # 清除旧的硬件状态标志，重新检测
        self.hardware_status_mismatch = False

        # 等待1秒，让设备日志推送到来
        time.sleep(1)

        # 验证硬件状态
        if not self._verify_hardware_status():
            self.result.add_step("硬件状态检查", False, "门锁状态不一致，硬件未真正锁定")
            return False

        self.result.add_step("硬件状态检查", True, "硬件状态正常")

        # 开始配对测试
        self.remote_pairing_result = None  # 清除之前的配对结果
        pairing_msg = RemotePairingMessage(self.config.device_psk, duration=pairing_duration)
        response = self._send_and_wait(pairing_msg)
        if not response:
            self.result.add_step("等待配对响应", False, "超时")
            return False

        self.result.add_step("等待配对响应", True)

        # 阶段一：等待 remote_pairing_result
        # 设备时序：先 GPIO8 保持 pairing_duration，之后才开 10 秒 GPIO3 采集窗口，
        # 窗口结束才发结果通知。所以超时必须覆盖 duration + 采集窗口 + 传输余量，
        # 不能按 duration 推算，否则会在设备出结论前就误判失败。
        prepare_secs = int(pairing_duration / 1000)
        pairing_timeout = (prepare_secs + self.PAIRING_KEY_WINDOW_SECS
                           + self.PAIRING_RESULT_MARGIN_SECS)
        logger.info(
            f"等待遥控器配对结果: 准备{prepare_secs}秒 + 采集窗口"
            f"{self.PAIRING_KEY_WINDOW_SECS}秒(需按{self.PAIRING_REQUIRED_PRESSES}次)"
            f" + 余量{self.PAIRING_RESULT_MARGIN_SECS}秒, 超时{pairing_timeout}秒")
        self._report_progress(
            f"配对准备中，请稍候后连续按遥控器配对键 {self.PAIRING_REQUIRED_PRESSES} 次...")

        pairing_result_received = False
        key_window_announced = False
        start_time = time.time()

        while time.time() - start_time < pairing_timeout:
            elapsed = time.time() - start_time

            # 两阶段倒计时：GPIO8 准备期按键无效，之后才是真正的采集窗口
            if report_callback:
                if elapsed < prepare_secs:
                    remaining = prepare_secs - int(elapsed)
                    if remaining > 0:
                        report_callback("pairing_prepare", remaining)
                else:
                    key_remaining = (prepare_secs + self.PAIRING_KEY_WINDOW_SECS
                                     - int(elapsed))
                    if key_remaining > 0:
                        if not key_window_announced:
                            key_window_announced = True
                            self._report_progress(
                                f"请连续按遥控器配对键 {self.PAIRING_REQUIRED_PRESSES} 次")
                        report_callback("pairing_countdown", key_remaining)

            # 检查是否收到配对结果
            if self.remote_pairing_result is not None:
                pairing_result_received = True
                if self.remote_pairing_result == "success":
                    logger.info("✓ 遥控器配对成功")
                    self._report_progress("✓ 遥控器配对成功")
                    self.result.add_step("遥控器配对", True, "配对成功")
                    break
                else:
                    # 设备侧 timeout 绝大多数是采集窗口内按键次数不够（highs=1/3），
                    # 把要求次数写进原因，避免只看到 "timeout" 无从下手
                    reason = f"配对失败: {self.remote_pairing_result}"
                    if self.remote_pairing_result == "timeout":
                        reason += (f"（设备未在{self.PAIRING_KEY_WINDOW_SECS}秒窗口内采集到"
                                   f"{self.PAIRING_REQUIRED_PRESSES}次按键，请确认连续按满"
                                   f"{self.PAIRING_REQUIRED_PRESSES}次）")
                    logger.error(f"✗ 遥控器配对失败: {reason}")
                    self._report_progress(f"✗ {reason}")
                    self.result.add_step("遥控器配对", False, reason)
                    return False

            time.sleep(0.5)

        # 如果配对超时仍未收到结果
        if not pairing_result_received:
            reason = f"{pairing_timeout}秒内未收到设备配对结果通知"
            logger.error(f"✗ {reason}")
            self._report_progress(f"✗ {reason}")
            self.result.add_step("遥控器配对", False, reason)
            return False

        # 阶段二：开门倒计时（open_timeout），等待 remote_open 事件
        if report_callback:
            report_callback("open_countdown", open_timeout)

        logger.info(f"等待遥控器开门事件，超时时间: {open_timeout}秒")
        self._report_progress(f"请按遥控器开门键，等待检测中...")

        start_time = time.time()
        open_success = False

        while time.time() - start_time < open_timeout:
            current_time = time.time()
            remaining = open_timeout - int(current_time - start_time)
            if report_callback and remaining > 0:
                report_callback("open_countdown", remaining)

            # 等待 remote_open 事件
            if self.remote_open_event.wait(timeout=0.3):
                logger.info("✓ 收到遥控器开门事件")
                self._report_progress("✓ 检测到遥控器开门事件")
                if report_callback:
                    report_callback("hide_dialog", 0)
                open_success = True
                break

            time.sleep(0.1)

        if not open_success:
            reason = f"{open_timeout}秒内未收到遥控器开门事件"
            logger.error(f"✗ {reason}")
            self._report_progress(f"✗ {reason}")
            self.result.add_step("遥控器开门", False, reason)
            return False

        self.result.add_step("遥控器开门", True, "遥控器开门成功")
        logger.info("✓ 遥控器配对测试完成")
        return True

    def test_emergency_switch(self, timeout: int = 10, report_callback: Callable = None) -> bool:
        self._report_progress("测试应急开关功能")

        # 无条件强制上锁（设备上报状态可能不准，单项测试时门也可能是开的）
        logger.info("应急开关测试前，强制上锁确保初始状态")
        self._report_progress("强制上锁，准备应急开关测试...")

        close_msg = CloseDoorMessage(self.config.device_psk)
        response = self._send_and_wait(close_msg)
        if not response:
            self.result.add_step("强制上锁响应", False, "超时")
            return False

        # 等待门锁完成关门动作（轮询确认）
        logger.info("等待门锁完成关门动作...")
        close_wait_start = time.time()
        locked = False
        while time.time() - close_wait_start < 10:
            time.sleep(1)
            current_state = self._query_door_state(timeout=3)
            if current_state in ["closed", "locked"]:
                logger.info(f"✓ 门锁已关闭，状态: {current_state}")
                locked = True
                break
            logger.debug(f"等待关门中，当前状态: {current_state}")

        if not locked:
            logger.warning("等待门锁关闭超时，继续测试")

        self.result.add_step("测试前门锁状态", True, "门已强制上锁")

        # 开始应急开关测试
        logger.info("门锁已上锁，等待用户按应急开关...")
        self._report_progress("请按应急开关进行测试，等待检测中...")
        self.emergency_event.clear()

        start_time = time.time()
        emergency_success = False

        while time.time() - start_time < timeout:
            current_time = time.time()
            if report_callback:
                remaining = timeout - (current_time - start_time)
                if remaining > 0:
                    report_callback("emergency_countdown", int(remaining) + 1)

            if self.emergency_event.wait(timeout=0.3):
                logger.info("✓ 收到应急开关事件通知，下发开锁指令")
                self._report_progress("收到应急开关事件，正在下发开锁指令...")
                open_msg = OpenDoorMessage(self.config.device_psk, self.config.test_open_duration)
                response = self._send_and_wait(open_msg)
                if not response:
                    self.result.add_step("应急开关开锁", False, "等待开锁响应超时")
                    return False
                self.result.add_step("应急开关开锁", True)
                logger.info("✓ 应急开关开锁指令已执行，应急开关功能正常")
                # 开锁成功，立即隐藏弹窗
                if report_callback:
                    report_callback("hide_dialog", 0)
                emergency_success = True
                break

        if not emergency_success:
            self.result.add_step("验证应急开关", False, f"{timeout}秒内未收到应急开关事件通知")
            return False

        self.result.add_step("验证应急开关", True, "应急开关功能正常")
        return True

    def test_ota_upgrade(self, tftp_server: str, tftp_port: int = 69, firmware_file: str = "update.fwpkg",
                         file_size: int = 0) -> bool:
        self._report_progress(f"【OTA升级】开始固件升级: {tftp_server}:{tftp_port}/{firmware_file}")

        ota_msg = OTAUpgradeMessage(self.config.device_psk, tftp_server, tftp_port, firmware_file, file_size)
        if not self.mqtt_client.publish(ota_msg.to_json()):
            logger.error("发送OTA升级指令失败")
            return False

        logger.info("✓ OTA升级指令已发送，设备将通过TFTP下载固件")
        self._report_progress("OTA升级指令已发送，设备正在下载固件...")
        return True

    def burn_mac_addresses(self, device_sn: str, progress_callback: Callable = None) -> tuple[bool, str]:
        try:
            if progress_callback:
                progress_callback("开始烧写MAC地址...")

            logger.info(f"开始为设备 {device_sn} 烧写MAC地址")

            mac_data = self._allocate_mac_from_csv()
            if mac_data:
                logger.info("已从本地CSV文件获取MAC地址")
            else:
                logger.warning("本地CSV分配失败，尝试从云端API获取MAC地址")
                if progress_callback:
                    progress_callback("本地CSV不可用，从云端API分配...")
                mac_data = self._allocate_mac_from_api(device_sn)
            if not mac_data:
                error_msg = "获取MAC地址分配失败（本地CSV和API均失败）"
                logger.error(error_msg)
                if progress_callback:
                    progress_callback(f"❌ {error_msg}")
                return False, error_msg

            wifi_mac = mac_data.get('wifiMac', '')
            sle_mac = mac_data.get('sparkMac', '')

            logger.info(f"获取到MAC地址 - WiFi: {wifi_mac}, SLE: {sle_mac}")
            if progress_callback:
                progress_callback(f"已获取MAC地址 - WiFi: {wifi_mac}, SLE: {sle_mac}")

            if progress_callback:
                progress_callback("检查设备当前MAC地址...")

            current_wifi_mac, current_sle_mac = self._read_current_mac()
            if not current_wifi_mac or not current_sle_mac:
                error_msg = "读取设备当前MAC地址失败"
                logger.error(error_msg)
                if progress_callback:
                    progress_callback(f"❌ {error_msg}")
                return False, error_msg

            logger.info(f"当前MAC地址 - WiFi: {current_wifi_mac}, SLE: {current_sle_mac}")
            if progress_callback:
                progress_callback(f"当前MAC - WiFi: {current_wifi_mac}, SLE: {current_sle_mac}")

            need_burn_wifi = current_wifi_mac.startswith('00')
            need_burn_sle = current_sle_mac.startswith('00')

            if not need_burn_wifi and not need_burn_sle:
                msg = "设备MAC地址已烧写，无需重复烧写"
                logger.info(msg)
                if progress_callback:
                    progress_callback(f"✅ {msg}")
                return True, msg

            failed_items = []

            if need_burn_wifi:
                if progress_callback:
                    progress_callback(f"正在烧写WiFi/BLE MAC: {wifi_mac}...")

                wifi_mac_clean = wifi_mac.replace(":", "").replace("-", "").upper()
                if not self._write_wifi_ble_mac(wifi_mac_clean):
                    failed_items.append("WiFi/BLE MAC烧写失败")
                    logger.error("WiFi/BLE MAC烧写失败")
                else:
                    logger.info("WiFi/BLE MAC烧写成功")
                    if progress_callback:
                        progress_callback("✅ WiFi/BLE MAC烧写成功")
            else:
                logger.info("WiFi/BLE MAC已烧写，跳过")
                if progress_callback:
                    progress_callback("WiFi/BLE MAC已烧写，跳过")

            if need_burn_sle:
                if progress_callback:
                    progress_callback(f"正在烧写SLE MAC: {sle_mac}...")

                sle_mac_clean = sle_mac.replace(":", "").replace("-", "").upper()
                if not self._write_sle_mac(sle_mac_clean):
                    failed_items.append("SLE MAC烧写失败")
                    logger.error("SLE MAC烧写失败")
                else:
                    logger.info("SLE MAC烧写成功")
                    if progress_callback:
                        progress_callback("✅ SLE MAC烧写成功")
            else:
                logger.info("SLE MAC已烧写，跳过")
                if progress_callback:
                    progress_callback("SLE MAC已烧写，跳过")

            if progress_callback:
                progress_callback("验证烧写结果...")

            time.sleep(1)

            verify_wifi_mac, verify_sle_mac = self._read_current_mac()
            if not verify_wifi_mac or not verify_sle_mac:
                failed_items.append("验证烧写结果失败")
            else:
                logger.info(f"验证MAC地址 - WiFi: {verify_wifi_mac}, SLE: {verify_sle_mac}")
                if progress_callback:
                    progress_callback(f"验证MAC - WiFi: {verify_wifi_mac}, SLE: {verify_sle_mac}")

                if need_burn_wifi and verify_wifi_mac.startswith('00'):
                    failed_items.append("WiFi/BLE MAC验证失败（仍为00开头）")

                if need_burn_sle and verify_sle_mac.startswith('00'):
                    failed_items.append("SLE MAC验证失败（仍为00开头）")

            if failed_items:
                error_msg = "、".join(failed_items)
                logger.error(f"MAC烧写失败: {error_msg}")
                if progress_callback:
                    progress_callback(f"❌ 烧写失败: {error_msg}")
                return False, error_msg

            success_msg = "MAC地址烧写成功"
            logger.info(success_msg)
            if progress_callback:
                progress_callback(f"✅ {success_msg}")
            return True, success_msg

        except Exception as e:
            error_msg = f"烧写MAC异常: {str(e)}"
            logger.error(error_msg)
            if progress_callback:
                progress_callback(f"❌ {error_msg}")
            return False, error_msg

    def _allocate_mac_from_api(self, device_sn: str) -> Optional[Dict]:
        try:
            url = "http://ishop-oqa.weidian.com/api/mac/allocate"
            headers = {
                "X-API-KEY": "WD_MAC_ALLOC_SECRET",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "User-Agent": "doorChecker/1.0"
            }
            data = {
                "sn": device_sn,
                "moduleType": "WS73"
            }

            logger.info(f"调用MAC分配API: {url}")
            response = requests.post(url, json=data, headers=headers, timeout=10)

            if response.status_code != 200:
                logger.error(f"API调用失败: HTTP {response.status_code}")
                return None

            result = response.json()
            if result.get('code') != 200:
                logger.error(f"API返回错误: {result.get('message')}")
                return None

            return result.get('data')

        except Exception as e:
            logger.error(f"调用MAC分配API异常: {e}")
            return None

    def _get_csv_path(self) -> str:
        """获取 mac_pool.csv 路径，兼容开发环境和 PyInstaller 打包环境"""
        import os
        return os.path.join(get_app_dir(), 'mac_pool.csv')

    def _allocate_mac_from_csv(self) -> Optional[Dict]:
        """从本地 CSV 文件分配一组 MAC 地址，分配后将 status 置为 1"""
        import os
        csv_path = self._get_csv_path()

        if not os.path.exists(csv_path):
            logger.error(f"本地MAC池文件不存在: {csv_path}")
            return None

        try:
            with open(csv_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            allocated = None
            allocated_idx = -1
            for idx, row in enumerate(rows):
                if str(row.get('status', '')).strip() != '1':
                    allocated = row
                    allocated_idx = idx
                    break

            if allocated is None:
                logger.error("本地MAC池已耗尽，所有地址均已分配")
                return None

            wifi_mac = allocated.get('wifi', '').strip()
            sle_mac = allocated.get('sle', '').strip()

            if not wifi_mac or not sle_mac:
                logger.error(f"CSV第{allocated_idx + 2}行数据不完整: wifi={wifi_mac}, sle={sle_mac}")
                return None

            # 标记已分配
            rows[allocated_idx]['status'] = '1'
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=['wifi', 'sle', 'status'])
                writer.writeheader()
                writer.writerows(rows)

            logger.info(f"从本地CSV分配MAC地址 - WiFi: {wifi_mac}, SLE: {sle_mac}")
            return {'wifiMac': wifi_mac, 'sparkMac': sle_mac}

        except Exception as e:
            logger.error(f"读取本地MAC池异常: {e}")
            return None

    def _read_current_mac(self) -> tuple[str, str]:
        wifi_mac = self._read_wifi_ble_mac()
        sle_mac = self._read_sle_mac()
        return wifi_mac or "", sle_mac or ""

    def _read_wifi_ble_mac(self) -> Optional[str]:
        read_msg = ReadWifiBleMaxMessage(self.config.device_psk)
        response = self._send_and_wait(read_msg)
        if not response:
            logger.error("读取WiFi/BLE MAC响应超时")
            return None
        wifi_mac = response.get('body', {}).get('wifi_mac', '')
        logger.debug(f"读取到WiFi MAC: {wifi_mac}")
        return wifi_mac

    def _read_sle_mac(self) -> Optional[str]:
        read_msg = ReadSleMaxMessage(self.config.device_psk)
        response = self._send_and_wait(read_msg)
        if not response:
            logger.error("读取SLE MAC响应超时")
            return None
        sle_mac = response.get('body', {}).get('mac', '')
        logger.debug(f"读取到SLE MAC: {sle_mac}")
        return sle_mac

    def _write_wifi_ble_mac(self, mac: str) -> bool:
        write_msg = WriteWifiBleMacMessage(self.config.device_psk, mac)
        response = self._send_and_wait(write_msg)
        if not response:
            logger.error("烧写WiFi/BLE MAC响应超时")
            return False
        code = response.get('header', {}).get('code', -1)
        if code == 0:
            logger.info("WiFi/BLE MAC烧写指令执行成功")
            return True
        logger.error(f"WiFi/BLE MAC烧写失败: {response.get('body', ).get('error', 'Unknown error')}")
        return False

    def _write_sle_mac(self, mac: str) -> bool:
        write_msg = WriteSleMaxMessage(self.config.device_psk, mac)
        response = self._send_and_wait(write_msg)
        if not response:
            logger.error("烧写SLE MAC响应超时")
            return False
        code = response.get('header', {}).get('code', -1)
        if code == 0:
            logger.info("SLE MAC烧写指令执行成功")
            return True
        else:
            error_msg = response.get('body', {}).get('error', 'Unknown error')
            logger.error(f"SLE MAC烧写失败: {error_msg}")
            return False

    def test_wifi_discover(self) -> bool:
        self._report_progress("【无线检测】WiFi AP扫描")
        msg = WifiDiscoverMessage(self.config.device_psk, self.config.discover_duration)
        timeout = self.config.discover_duration / 1000 + self.config.test_timeout + 10
        response = self._send_and_wait(msg, int(timeout))
        if not response:
            self.result.add_step("WiFi扫描响应", False, "超时")
            return False
        aps = response.get('body', {}).get('aps', [])
        threshold = self.config.wifi_rssi_threshold
        strongest = max((ap.get('rssi', -999) for ap in aps), default=-999)
        passed = any(ap.get('rssi', -999) >= threshold for ap in aps)
        detail = (
            f"共{len(aps)}个AP，最强RSSI {strongest}dBm，"
            f"阈值>={threshold}dBm，{'通过' if passed else '未发现满足条件的AP'}"
        )
        self.result.add_step("WiFi扫描结果", passed, detail)
        self._report_progress(detail)
        return passed

    def test_ble_discover(self) -> bool:
        self._report_progress("【无线检测】BLE蓝牙扫描")
        msg = BleDiscoverMessage(self.config.device_psk, self.config.discover_duration)
        timeout = self.config.discover_duration / 1000 + self.config.test_timeout
        response = self._send_and_wait(msg, int(timeout))
        if not response:
            self.result.add_step("BLE扫描响应", False, "超时")
            return False
        devices = response.get('body', {}).get('devices', [])
        threshold = self.config.ble_rssi_threshold
        passed = any(d.get('rssi', -999) > threshold for d in devices)
        self.result.add_step("BLE扫描结果", passed,
                             f"共{len(devices)}个设备，阈值>{threshold}dBm，{'通过' if passed else '未发现满足条件的设备'}")
        return passed

    def test_sle_discover(self) -> bool:
        self._report_progress("【无线检测】SLE星闪扫描")
        msg = SleDiscoverMessage(self.config.device_psk, self.config.discover_duration)
        timeout = self.config.discover_duration / 1000 + self.config.test_timeout
        response = self._send_and_wait(msg, int(timeout))
        if not response:
            self.result.add_step("SLE扫描响应", False, "超时")
            return False
        device_count = response.get('body', {}).get('device_count', 0)
        min_count = self.config.sle_min_count
        passed = device_count >= min_count
        self.result.add_step("SLE扫描结果", passed,
                             f"发现{device_count}个设备，最小要求{min_count}，{'通过' if passed else '未发现设备'}")
        return passed

    def reset_config(self, progress_callback: Callable = None) -> tuple[bool, str]:
        try:
            if progress_callback:
                progress_callback("发送重置NV配置指令...")

            reset_msg = ResetConfigMessage(self.config.device_psk)
            response = self._send_and_wait(reset_msg)
            if not response:
                error_msg = "重置配置响应超时"
                logger.error(error_msg)
                return False, error_msg

            code = response.get('header', {}).get('code', -1)
            body = response.get('body', {})
            result = body.get('result', '')
            message = body.get('message', '')

            if code == 0 and result == 'success':
                success_msg = f"NV配置重置成功: {message}"
                logger.info(success_msg)
                return True, success_msg
            else:
                error_msg = f"NV配置重置失败: {message or 'Unknown error'}"
                logger.error(error_msg)
                return False, error_msg

        except Exception as e:
            error_msg = f"重置配置异常: {str(e)}"
            logger.error(error_msg)
            return False, error_msg
