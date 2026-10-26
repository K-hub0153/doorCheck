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
    
    def __post_init__(self):
        if self.last_seen is None:
            self.last_seen = datetime.now()
