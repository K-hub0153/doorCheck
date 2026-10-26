import time
from typing import Callable, Optional, Dict
from .test_result import TestResult, TestStatus
from ..network.speaker_http_client import SpeakerHTTPClient
from ..utils.logger import logger


class SpeakerType:
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    UNKNOWN = "unknown"


class SpeakerTestEngine:
    def __init__(self, http_client: SpeakerHTTPClient):
        self.http_client = http_client
        self.result = TestResult()
        self.on_progress_callback = None
        self.speaker_type = SpeakerType.UNKNOWN
    
    def _report_progress(self, message: str):
        logger.info(message)
        if self.on_progress_callback:
            self.on_progress_callback(message)
    
    def set_progress_callback(self, callback: Callable):
        self.on_progress_callback = callback
    
    def detect_speaker_type(self) -> str:
        self._report_progress("【检测音箱类型】")
        
        result = self.http_client.get_stream_urls()
        if result and result.get("code") == 0:
            streams = result.get("data", {}).get("streams", [])
            if streams:
                self.speaker_type = SpeakerType.OUTDOOR
                self._report_progress("✓ 检测到摄像头，判定为室外音箱")
                self.result.add_step("检测音箱类型", True, "室外音箱")
                return SpeakerType.OUTDOOR
        
        ir_result = self.http_client.send_ir_blaster("0x00", "0x00")
        if ir_result and ir_result.get("code") == 0:
            self.speaker_type = SpeakerType.INDOOR
            self._report_progress("✓ 检测到红外模块，判定为室内音箱")
            self.result.add_step("检测音箱类型", True, "室内音箱")
            return SpeakerType.INDOOR
        
        self.speaker_type = SpeakerType.UNKNOWN
        self._report_progress("⚠ 无法确定音箱类型")
        self.result.add_step("检测音箱类型", False, "无法确定类型")
        return SpeakerType.UNKNOWN
    
    def test_network(self) -> bool:
        self._report_progress("【测试有线网络】")
        
        if self.http_client.health_check():
            self._report_progress("✓ 网络连接正常")
            self.result.add_step("有线网络测试", True)
            return True
        else:
            self._report_progress("✗ 网络连接失败")
            self.result.add_step("有线网络测试", False, "无法连接")
            return False
    
    def test_mac_address(self, wifi_mac: str, starflash_mac: str) -> bool:
        self._report_progress("【测试MAC地址烧录】")
        
        result = self.http_client.set_mac_addresses(wifi_mac, starflash_mac)
        if result and result.get("code") == 0:
            self._report_progress(f"✓ MAC地址烧录成功")
            self._report_progress(f"  WiFi MAC: {wifi_mac}")
            self._report_progress(f"  星闪 MAC: {starflash_mac}")
            self.result.add_step("MAC地址烧录", True)
            return True
        else:
            error_msg = result.get("message", "未知错误") if result else "请求失败"
            self._report_progress(f"✗ MAC地址烧录失败: {error_msg}")
            self.result.add_step("MAC地址烧录", False, error_msg)
            return False
    
    def query_mac_address(self) -> Optional[Dict]:
        self._report_progress("【查询MAC地址】")
        
        result = self.http_client.get_mac_addresses()
        if result and result.get("code") == 0:
            data = result.get("data", {})
            self._report_progress(f"✓ MAC地址查询成功")
            self._report_progress(f"  有线MAC: {data.get('ethernet_mac')}")
            self._report_progress(f"  WiFi MAC: {data.get('wifi_mac')}")
            self._report_progress(f"  蓝牙MAC: {data.get('bluetooth_mac')}")
            self._report_progress(f"  星闪MAC: {data.get('starflash_mac')}")
            self.result.add_step("MAC地址查询", True)
            return data
        else:
            self._report_progress("✗ MAC地址查询失败")
            self.result.add_step("MAC地址查询", False)
            return None
    
    def test_version(self) -> bool:
        self._report_progress("【查询版本信息】")
        
        result = self.http_client.get_version()
        if result and result.get("code") == 0:
            data = result.get("data", {})
            app_version = data.get("app_version", "未知")
            self._report_progress(f"✓ 版本查询成功: {app_version}")
            self.result.add_step("版本查询", True, app_version)
            return True
        else:
            self._report_progress("✗ 版本查询失败")
            self.result.add_step("版本查询", False)
            return False
    
    def test_factory_mode(self) -> bool:
        self._report_progress("【测试产测模式切换】")
        
        result = self.http_client.get_factory_mode()
        if result and result.get("code") == 0:
            mode = result.get("data", {}).get("mode", "unknown")
            self._report_progress(f"✓ 当前模式: {mode}")
            self.result.add_step("产测模式查询", True, mode)
            return True
        else:
            self._report_progress("✗ 产测模式查询失败")
            self.result.add_step("产测模式查询", False)
            return False
    
    def test_speaker(self) -> bool:
        self._report_progress("【测试喇叭】")
        
        self.http_client.set_ao_volume(0)
        time.sleep(0.5)
        
        result = self.http_client.play_audio()
        if result and result.get("code") == 0:
            self._report_progress("✓ 音频播放成功，请确认是否听到声音")
            time.sleep(3)
            self.http_client.stop_audio()
            self.result.add_step("喇叭测试", True)
            return True
        else:
            self._report_progress("✗ 音频播放失败")
            self.result.add_step("喇叭测试", False)
            return False
    
    def test_mic(self, duration: int = 3) -> bool:
        self._report_progress("【测试MIC】")
        
        self.http_client.set_ai_volume(10)
        time.sleep(0.5)
        
        self._report_progress(f"开始录音 {duration} 秒，请对着麦克风说话...")
        result = self.http_client.mic_record_play(duration)
        
        if result and result.get("code") == 0:
            self._report_progress("✓ MIC录音并播放成功，请确认是否听到回放")
            self.result.add_step("MIC测试", True)
            return True
        else:
            self._report_progress("✗ MIC测试失败")
            self.result.add_step("MIC测试", False)
            return False
    
    def test_nearlink(self, duration: int = 5) -> bool:
        self._report_progress("【测试星闪】")
        
        result = self.http_client.scan_sle(duration)
        if result and result.get("code") == 0:
            devices = result.get("data", [])
            count = len(devices)
            self._report_progress(f"✓ 星闪扫描成功，发现 {count} 个设备")
            self.result.add_step("星闪测试", True, f"发现{count}个设备")
            return True
        else:
            self._report_progress("✗ 星闪扫描失败")
            self.result.add_step("星闪测试", False)
            return False
    
    def test_battery_voltage(self) -> bool:
        self._report_progress("【测试电池电压】")
        
        result = self.http_client.get_adc_voltage(channel=1)
        if result and result.get("code") == 0:
            data = result.get("data", {})
            battery_voltage = data.get("battery_voltage", 0)
            self._report_progress(f"✓ 电池电压: {battery_voltage}V")
            
            if battery_voltage < 3.0:
                self._report_progress("⚠ 警告: 电池电压过低")
                self.result.add_step("电池电压检测", False, f"{battery_voltage}V (过低)")
                return False
            
            self.result.add_step("电池电压检测", True, f"{battery_voltage}V")
            return True
        else:
            self._report_progress("✗ 电池电压检测失败")
            self.result.add_step("电池电压检测", False)
            return False
    
    def test_infrared(self) -> bool:
        self._report_progress("【测试红外遥控】")
        
        result = self.http_client.send_ir_blaster("0x4B", "0x36")
        if result and result.get("code") == 0:
            self._report_progress("✓ 红外信号发送成功，请用红外接收器验证")
            self.result.add_step("红外遥控测试", True)
            return True
        else:
            self._report_progress("✗ 红外信号发送失败")
            self.result.add_step("红外遥控测试", False)
            return False
    
    def test_microwave(self, duration: int = 5) -> bool:
        self._report_progress("【测试微波感应】")
        
        self._report_progress(f"开始采集微波数据 {duration} 秒，请在设备前挥手...")
        result = self.http_client.read_microwave(duration, interval=200)
        
        if result and result.get("code") == 0:
            data = result.get("data", {}).get("data", [])
            has_person_count = sum(1 for d in data if d.get("has_person") == 1)
            self._report_progress(f"✓ 微波感应测试完成，检测到人体 {has_person_count} 次")
            
            if has_person_count > 0:
                self.result.add_step("微波感应测试", True, f"检测{has_person_count}次")
                return True
            else:
                self._report_progress("⚠ 警告: 未检测到人体移动")
                self.result.add_step("微波感应测试", False, "未检测到人体")
                return False
        else:
            self._report_progress("✗ 微波感应测试失败")
            self.result.add_step("微波感应测试", False)
            return False
    
    def test_camera(self) -> bool:
        self._report_progress("【测试摄像头】")
        
        result = self.http_client.get_stream_urls()
        if result and result.get("code") == 0:
            data = result.get("data", {})
            streams = data.get("streams", [])
            if streams:
                main_stream = streams[0]
                url = main_stream.get("url", "")
                self._report_progress(f"✓ 摄像头正常，RTSP地址: {url}")
                self.result.add_step("摄像头测试", True)
                return True
            else:
                self._report_progress("✗ 未找到视频流")
                self.result.add_step("摄像头测试", False, "无视频流")
                return False
        else:
            self._report_progress("✗ 摄像头测试失败")
            self.result.add_step("摄像头测试", False)
            return False
    
    def run_common_tests(self) -> bool:
        self._report_progress("=" * 50)
        self._report_progress("开始通用功能测试")
        self._report_progress("=" * 50)
        
        if not self.test_network():
            return False
        
        if not self.test_version():
            return False
        
        if not self.test_speaker():
            return False
        
        if not self.test_mic():
            return False
        
        if not self.test_nearlink():
            return False
        
        if not self.test_battery_voltage():
            return False
        
        return True
    
    def run_indoor_tests(self) -> bool:
        self._report_progress("=" * 50)
        self._report_progress("开始室内音箱特定测试")
        self._report_progress("=" * 50)
        
        if not self.test_infrared():
            return False
        
        return True
    
    def run_outdoor_tests(self) -> bool:
        self._report_progress("=" * 50)
        self._report_progress("开始室外音箱特定测试")
        self._report_progress("=" * 50)
        
        if not self.test_camera():
            return False
        
        if not self.test_microwave():
            return False
        
        return True
    
    def run_full_test(self) -> TestResult:
        self.result = TestResult()
        self.result.status = TestStatus.RUNNING
        self.result.start_time = time.time()
        
        try:
            self._report_progress("开始音箱产测流程...")
            
            speaker_type = self.detect_speaker_type()
            
            if not self.run_common_tests():
                self.result.set_failed("通用功能测试失败")
                return self.result
            
            if speaker_type == SpeakerType.INDOOR:
                if not self.run_indoor_tests():
                    self.result.set_failed("室内音箱测试失败")
                    return self.result
            elif speaker_type == SpeakerType.OUTDOOR:
                if not self.run_outdoor_tests():
                    self.result.set_failed("室外音箱测试失败")
                    return self.result
            else:
                self.result.set_failed("无法确定音箱类型")
                return self.result
            
            self._report_progress("=" * 50)
            self._report_progress("✅ 所有测试通过！")
            self._report_progress("=" * 50)
            self.result.set_passed()
            
        except Exception as e:
            logger.error(f"测试异常: {e}")
            self.result.set_failed(f"测试异常: {str(e)}")
        
        return self.result
