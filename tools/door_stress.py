"""
门锁开关压测脚本
"""
import sys
import os
import time
import threading
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.network.mqtt_client import MQTTClient
from src.core.protocol import OpenDoorMessage, CloseDoorMessage
from src.utils.config import Config


def setup_logger():
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f'stress_test_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')

    logger = logging.getLogger('stress_test')
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    logger.addHandler(handler)
    return logger


class DoorStressTest:
    def __init__(self, mqtt_client: MQTTClient, psk: str, open_duration: int, logger):
        self.mqtt = mqtt_client
        self.psk = psk
        self.open_duration = open_duration
        self.logger = logger
        self.response = None
        self.response_event = threading.Event()
        self.mqtt.register_callback("stress_test", self._on_message)

    def _on_message(self, topic, message):
        if "reply" in topic:
            self.response = message
            self.response_event.set()

    def _send_command(self, message, timeout=10):
        if not self.mqtt.publish(message.to_json()):
            return False, "发送失败"
        self.response_event.clear()
        self.response = None
        if not self.response_event.wait(timeout):
            return False, "响应超时"
        code = self.response.get('header', {}).get('code', -1)
        return code == 0, f"code={code}"

    def open_door(self):
        return self._send_command(OpenDoorMessage(self.psk, self.open_duration))

    def close_door(self):
        return self._send_command(CloseDoorMessage(self.psk))


def main(device_sn, cycles, interval=3.0):
    logger = setup_logger()
    config = Config()

    logger.info(f"连接 MQTT {config.mqtt_broker}:{config.mqtt_port}")
    mqtt = MQTTClient(config.mqtt_broker, config.mqtt_port, config.product_id, device_sn)

    if not mqtt.connect(timeout=10):
        logger.error("MQTT 连接失败")
        sys.exit(1)
    logger.info("MQTT 连接成功")

    tester = DoorStressTest(mqtt, config.device_psk, config.test_open_duration, logger)
    success, fail = 0, 0
    logger.info(f"开始压测: {cycles} 次开关门, 间隔 {interval}s")

    try:
        for i in range(1, cycles + 1):
            ok, detail = tester.open_door()
            logger.info(f"[{i}/{cycles}] 开门: {'OK' if ok else 'FAIL'} ({detail})")
            success += ok
            fail += not ok
            time.sleep(interval)

            ok, detail = tester.close_door()
            logger.info(f"[{i}/{cycles}] 关门: {'OK' if ok else 'FAIL'} ({detail})")
            success += ok
            fail += not ok

            if i < cycles:
                time.sleep(interval)

    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        mqtt.disconnect()
        total = success + fail
        logger.info(f"压测结束: 总操作 {total}, 成功 {success}, 失败 {fail}")
        if total > 0:
            logger.info(f"成功率: {success / total * 100:.1f}%")


if __name__ == "__main__":
    main("E466E512A914", 3, 1.0)
