from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime
from enum import Enum


class TestStatus(Enum):
    NOT_TESTED = "not_tested"
    TESTING = "testing"
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class TestResult:
    status: TestStatus = TestStatus.NOT_TESTED
    message: str = ""
    detail: str = ""
    tested_at: Optional[datetime] = None
    
    def set_passed(self, message: str = "通过", detail: str = ""):
        self.status = TestStatus.PASSED
        self.message = message
        self.detail = detail
        self.tested_at = datetime.now()
    
    def set_failed(self, message: str = "失败", detail: str = ""):
        self.status = TestStatus.FAILED
        self.message = message
        self.detail = detail
        self.tested_at = datetime.now()
    
    def set_testing(self):
        self.status = TestStatus.TESTING
        self.message = "测试中..."
        self.tested_at = datetime.now()


@dataclass
class DeviceTestResults:
    sn: str
    ip: str
    poe: TestResult = field(default_factory=TestResult)
    disk: TestResult = field(default_factory=TestResult)
    wifi: TestResult = field(default_factory=TestResult)
    bluetooth: TestResult = field(default_factory=TestResult)
    nearlink: TestResult = field(default_factory=TestResult)
    lte_4g: TestResult = field(default_factory=TestResult)
    mac_write: TestResult = field(default_factory=TestResult)
    fan: TestResult = field(default_factory=TestResult)
    hdmi: TestResult = field(default_factory=TestResult)  # HDMI 接口检测（仅部分芯片支持）
    battery: TestResult = field(default_factory=TestResult)  # 电池检测
    
    def get_overall_status(self) -> TestStatus:
        results = [self.poe, self.disk, self.lte_4g]
        
        if any(r.status == TestStatus.FAILED for r in results):
            return TestStatus.FAILED
        
        if all(r.status == TestStatus.PASSED for r in results):
            return TestStatus.PASSED
        
        if any(r.status == TestStatus.TESTING for r in results):
            return TestStatus.TESTING
        
        return TestStatus.NOT_TESTED
    
    def to_dict(self) -> Dict:
        return {
            'sn': self.sn,
            'ip': self.ip,
            'poe': {'status': self.poe.status.value, 'message': self.poe.message, 'detail': self.poe.detail},
            'disk': {'status': self.disk.status.value, 'message': self.disk.message, 'detail': self.disk.detail},
            'wifi': {'status': self.wifi.status.value, 'message': self.wifi.message, 'detail': self.wifi.detail},
            'bluetooth': {'status': self.bluetooth.status.value, 'message': self.bluetooth.message, 'detail': self.bluetooth.detail},
            'nearlink': {'status': self.nearlink.status.value, 'message': self.nearlink.message, 'detail': self.nearlink.detail},
            'lte_4g': {'status': self.lte_4g.status.value, 'message': self.lte_4g.message, 'detail': self.lte_4g.detail},
            'mac_write': {'status': self.mac_write.status.value, 'message': self.mac_write.message, 'detail': self.mac_write.detail},
            'fan': {'status': self.fan.status.value, 'message': self.fan.message, 'detail': self.fan.detail},
            'hdmi': {'status': self.hdmi.status.value, 'message': self.hdmi.message, 'detail': self.hdmi.detail},
            'battery': {'status': self.battery.status.value, 'message': self.battery.message, 'detail': self.battery.detail},
            'overall_status': self.get_overall_status().value
        }
