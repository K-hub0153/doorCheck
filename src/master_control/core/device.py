from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class MasterDevice:
    ip: str
    sn: str
    model: str = "MASTER"
    port: int = 22
    status: str = "discovered"
    last_seen: Optional[datetime] = None
    disk_model: str = ""
    disk_size: str = ""
    # 扫描时确认机上只有 U 盘/移动硬盘、没有内置盘（区分「没读到」和「确实没有」）
    disk_missing: bool = False

    def __post_init__(self):
        if self.last_seen is None:
            self.last_seen = datetime.now()
