"""冒烟测试：验证「刷新发现」/「子网扫描」入口都会清空设备列表

真实网络动作（mDNS、子网扫描）全部打桩，只验证入口的清理行为。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtWidgets import QApplication

from src.master_control.core import discovery as discovery_mod
from src.master_control.core.device import MasterDevice
from src.master_control.gui.main_window import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win._clear_device_list()

# ---- 打桩：不碰真实网络 ----
win.discovery.start_mdns_discovery = lambda: print("[stub] start_mdns_discovery")
win.discovery.restart_mdns_discovery = lambda: print("[stub] restart_mdns_discovery")
win.discovery.scan_subnet = lambda subnet=None, progress_callback=None: (
    print("[stub] scan_subnet", subnet) or []
)
discovery_mod.get_all_interfaces = lambda: [
    {"name": "stub0", "ip": "10.0.0.2", "subnet": "10.0.0.0/24"}
]


def seed(n=2):
    for i in range(n):
        d = MasterDevice(ip=f"10.0.0.{i+1}", sn=f"OLD-{i}")
        win.discovery.devices[d.sn] = d
        win.device_list_panel.add_device(d)
    print("  seeded cards:", len(win.device_list_panel.device_cards),
          "discovery:", len(win.discovery.devices))


print("-- 刷新发现路径 --")
seed()
win.start_mdns_discovery()
win.discovery_worker.wait(3000)
print("  after refresh cards:", len(win.device_list_panel.device_cards),
      "discovery:", len(win.discovery.devices))
assert len(win.device_list_panel.device_cards) == 0
assert len(win.discovery.devices) == 0

print("-- 子网扫描路径 --")
seed()
win.start_subnet_scan()
if win.discovery_worker:
    win.discovery_worker.wait(5000)
print("  after scan cards:", len(win.device_list_panel.device_cards),
      "discovery:", len(win.discovery.devices))
assert len(win.device_list_panel.device_cards) == 0
assert len(win.discovery.devices) == 0

win.close()
print("ENTRY_SMOKE_OK")
