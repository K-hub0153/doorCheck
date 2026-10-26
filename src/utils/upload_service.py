import requests
from datetime import datetime
from typing import List, Dict
from .logger import logger


class UploadService:
    def __init__(self, url: str = "http://ishop-oqa.weidian.com/checkData/addBatch"):
        self.url = url

    def upload_records(self, records: List[Dict], check_type: int = 1) -> bool:
        """
        上传测试记录到服务器

        Args:
            records: 测试记录列表
            check_type: 质检类型，1=生产质检，2=仓库质检
        """
        if not records:
            logger.warning("没有记录需要上传")
            return False

        # 转换为服务器要求的格式
        payload = []
        for record in records:
            # 将时间字符串转换为毫秒时间戳
            create_time = self._parse_time(record['create_time'])
            update_time = self._parse_time(record['update_time'])

            payload.append({
                "cur_createTime": create_time,
                "cur_updateTime": update_time,
                "deviceName": record['sub_type'],
                "result": "true" if record['results'] == 'PASS' else "false",
                "sn": record['sn'],
                "check_type": check_type  # 新增质检类型字段
            })

        check_type_name = "生产质检" if check_type == 1 else "仓库质检"
        logger.info(f"准备上传 {len(records)} 条记录，质检类型: {check_type_name}")

        try:
            response = requests.post(self.url, json=payload, timeout=10)
            if response.status_code == 200 and response.text == "OK":
                logger.info(f"上传成功: {len(records)}条记录，质检类型: {check_type_name}")
                return True
            else:
                logger.error(f"上传失败: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logger.error(f"上传异常: {e}")
            return False

    def _parse_time(self, time_str: str) -> int:
        """将时间字符串转换为毫秒时间戳"""
        try:
            dt = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
            return int(dt.timestamp() * 1000)
        except Exception as e:
            logger.error(f"时间解析失败: {time_str}, {e}")
            return int(datetime.now().timestamp() * 1000)
