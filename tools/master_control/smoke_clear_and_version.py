"""离屏冒烟测试：验证设备列表清空 + 版本号同行显示

不联网、不启动 mDNS 扫描，只构造窗口验证 UI 结构与清空逻辑。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from src.master_control.core.device import MasterDevice
from src.master_control.gui.main_window import MainWindow

app = QApplication(sys.argv)
win = MainWindow()

# ---- 1. 版本号与 SN/IP 同一行、无「系统版本」前缀、无复制SN 按钮 ----
panel = win.device_panels  # 空
from src.master_control.gui.device_detail_panel import DeviceDetailPanel
p = DeviceDetailPanel()
p.set_device("SN-TEST-0001", "192.168.1.10")
p.set_os_version("1.2.3")

info_text = p.device_info_label.text()
ver_text = p.os_version_label.text()
print("device_info_label:", info_text)
print("os_version_label :", ver_text)
print("same_row         :", p.os_version_label.parent() is p.device_info_label.parent())
assert "SN: SN-TEST-0001" in info_text and "IP: 192.168.1.10" in info_text
assert ver_text == "1.2.3", ver_text
assert "系统版本" not in ver_text
assert not hasattr(p, "copy_sn_btn") and not hasattr(p, "copy_sn"), "复制SN 应已移除"

# ---- 2. 清空设备列表：卡片、Tab、discovery 缓存全部清掉 ----
for i in range(3):
    d = MasterDevice(ip=f"192.168.1.{10+i}", sn=f"SN-TEST-{i:04d}", model="MASTER")
    win.discovery.devices[d.sn] = d
    win.device_list_panel.add_device(d)
    tp = DeviceDetailPanel()
    tp.set_device(d.sn, d.ip)
    win.device_panels[d.sn] = tp
    win.device_tabs.addTab(tp, d.sn)

print("before -> cards:", len(win.device_list_panel.device_cards),
      "tabs:", win.device_tabs.count(),
      "discovery:", len(win.discovery.devices))

win._clear_device_list()

print("after  -> cards:", len(win.device_list_panel.device_cards),
      "tabs:", win.device_tabs.count(),
      "discovery:", len(win.discovery.devices))
assert len(win.device_list_panel.device_cards) == 0
assert win.device_tabs.count() == 0
assert len(win.discovery.devices) == 0
assert win.device_list_panel.current_selected_card is None

# ---- 3. 待选设备通过 discovery 重新出现后仍能正常加卡片 ----
d = MasterDevice(ip="192.168.1.50", sn="SN-NEW-0001")
win.discovery.devices[d.sn] = d
win.device_list_panel.add_device(d)
assert len(win.device_list_panel.device_cards) == 1
print("re-add after clear: OK")

win.close()
print("SMOKE_OK")
