import time
import threading
from enum import Enum
from typing import Callable, Optional
from ..network.mqtt_client import MQTTClient
from .protocol import SpeakerOpenDoorMessage, SpeakerCloseDoorMessage, SpeakerQueryStatusMessage
from ..utils.logger import logger
from ..utils.config import Config


class TestStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class TestResult:
    def __init__(self):
        self.status = TestStatus.IDLE
        self.steps = []
        self.error_message = ""
        self.start_time = None
        self.end_time = None

    def add_step(self, step_name: str, success: bool, message: str = ""):
        self.steps.append({
            "name": step_name,
            "success": success,
            "message": message,
            "timestamp": time.time()
        })

    def set_passed(self):
        self.status = TestStatus.PASSED
        self.end_time = time.time()

    def set_failed(self, error: str):
        self.status = TestStatus.FAILED
        self.error_message = error
        self.end_time = time.time()
    
    def get_failed_steps_summary(self) -> str:
        """获取失败测试项的摘要，用于数据库备注字段"""
        failed_steps = [step for step in self.steps if not step['success']]
        if not failed_steps:
            return ""
        
        # 格式：测试项1: 原因1; 测试项2: 原因2
        summary_parts = []
        for step in failed_steps:
            step_name = step['name']
            message = step.get('message', '').strip()
            if message:
                summary_parts.append(f"{step_name}: {message}")
            else:
                summary_parts.append(f"{step_name}: 测试失败")
        
        return "; ".join(summary_parts)
