# -*- coding: utf-8 -*-
"""USB 3.0 接口（链路速率）检测

质检时会在 USB 口上接一个 USB 3.0 设备（U 盘/移动硬盘），
**只要 3.0 协议上枚举出了设备就判通过**，退化成 2.0（480M）就判不通过。

「3.0 协议下有设备」在系统里的直接证据是**协商速率**：
设备以 USB 3.x 超速模式枚举时，链路速率是 5000M / 10000M / 20000M；
只有 USB 2.0 时是 480M（全速 12M、低速 1.5M 更是老设备）。

接口形态与芯片相关（只看契约，不影响判定逻辑）：

===========  =============  ==================================================
芯片          接口          说明
===========  =============  ==================================================
RK3576       Type-A         蓝色 Type-A 口，直连 SoC 的 xHCI
海思310P      Type-C         正反插 Type-C 口
===========  =============  ==================================================

识别顺序：

1. `lsusb -t`（最直观：树形结构 + 每行末尾的协商速率）
2. 设备上没有 lsusb（usbutils 未安装的裁剪固件）时，退回读 sysfs：
   遍历 `/sys/bus/usb/devices/*/speed`，速率单位同样是 Mbps
3. 两条路都拿不到数据 → 判失败并写明原因（不能「读不到就当通过」）

判定只看**非 root hub** 的设备：root hub 自己也会报 5000M
（`Class=root_hub, Driver=xhci-hcd/1p, 5000M`），把它算进去就永远通过，
这是本检测最容易踩的坑。
"""
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# 判定为 USB 3.x 的最低协商速率（Mbps）：5000 = USB 3.2 Gen1（原 3.0）
DEFAULT_MIN_SPEED_MBPS = 5000

# 一条 SSH 命令读完整个 USB 树
LSUSB_CMD = 'lsusb -t'
LSUSB_TIMEOUT = 10
# sysfs 兜底：设备目录名形如 1-1、2-1.4；跳过 usbN（root hub）与 1-1:1.0（接口）
SYSFS_CMD = (
    'for d in /sys/bus/usb/devices/*; do '
    'n=$(basename "$d"); '
    'case "$n" in usb[0-9]*|*:*) continue;; esac; '
    '[ -f "$d/speed" ] || continue; '
    'printf "%s|%s|%s|%s\\n" "$n" '
    '"$(cat "$d/speed" 2>/dev/null)" '
    '"$(cat "$d/bDeviceClass" 2>/dev/null)" '
    '"$(cat "$d/product" 2>/dev/null)"; '
    'done'
)
SYSFS_TIMEOUT = 10

# lsusb -t 的根行： /:  Bus 02.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/4p, 5000M
_ROOT_RE = re.compile(r'Bus\s+(\d+)\.Port\s+\d+\s*:\s*Dev\s+(\d+)')
# 子行：     |__ Port 1: Dev 2, If 0, Class=Mass Storage, Driver=usb-storage, 5000M
_PORT_RE = re.compile(r'Port\s+\d+\s*:\s*Dev\s+(\d+)')
_CLASS_RE = re.compile(r'Class=([^,]+)')
_DRIVER_RE = re.compile(r'Driver=([^,\s]+)')
_IF_RE = re.compile(r'\bIf\s+(\d+)')
# 行尾速率： 5000M / 480M / 1.5M（个别 lsusb 版本不带速率）
_SPEED_RE = re.compile(r',\s*(\d+(?:\.\d+)?)([MG])\s*$')

# sysfs 的 bDeviceClass 是十六进制字符串（'00'、'09'、'08' …）
_SYSFS_CLASS_NAMES = {
    '00': '设备',
    '01': 'Audio',
    '02': 'CDC',
    '03': 'HID',
    '06': 'Image',
    '07': 'Printer',
    '08': 'Mass Storage',
    '09': 'Hub',
    '0a': 'CDC Data',
    '0e': 'Video',
    'e0': 'Wireless',
    'ef': 'Vendor Specific',
    'ff': 'Vendor Specific',
}


@dataclass
class UsbDevice:
    """USB 树上的一台设备（多接口设备只算一台）"""
    bus: str = ''               # '02'，仅用于展示与去重
    dev: str = ''               # '2'
    name: str = ''              # sysfs 的设备目录名（'1-2'），lsusb 解析时为空
    dev_class: str = ''         # 'Mass Storage' / 'Hub' / 'root_hub'
    driver: str = ''            # 'usb-storage' / 'hub' / 'xhci_hcd'
    product: str = ''           # sysfs 兜底时读到的产品名
    speed_mbps: float = 0.0     # 协商速率，未知为 0
    is_root_hub: bool = False
    interfaces: List[int] = field(default_factory=list)

    @property
    def key(self) -> str:
        """同一台设备可能有多条 If 行 / 多条接口目录，用它去重"""
        return self.name or f'{self.bus}-{self.dev}'

    @property
    def speed_text(self) -> str:
        if not self.speed_mbps:
            return '速率未知'
        speed = self.speed_mbps
        text = f'{speed:g}M'
        if speed >= 1000:
            text += f' ({speed / 1000:g}Gbps)'
        return text

    @property
    def is_superspeed(self) -> bool:
        return self.speed_mbps >= DEFAULT_MIN_SPEED_MBPS

    @property
    def label(self) -> str:
        """详情里的一行： `Bus 02 Dev 2 | Mass Storage | usb-storage | 5000M`"""
        where = f'Bus {self.bus} Dev {self.dev}' if self.bus else f'端口 {self.name}'
        parts = [where]
        cls = self.dev_class or '未知类'
        if self.product and self.product not in cls:
            cls = f'{cls}({self.product})'
        parts.append(cls)
        if self.driver:
            parts.append(self.driver)
        parts.append(self.speed_text)
        return ' | '.join(parts)


def _parse_speed(text: str) -> float:
    """'5000M' → 5000.0；'1.5M' → 1.5；sysfs 的 '5000' 也认"""
    text = (text or '').strip()
    if not text:
        return 0.0
    match = re.match(r'^(\d+(?:\.\d+)?)([MG])?$', text, re.IGNORECASE)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def parse_lsusb_tree(text: str) -> List[UsbDevice]:
    """解析 `lsusb -t` 的树形输出

    子行不带 Bus 号，按上一条根行继承；同一 Dev 的多条 `If N` 行合并成一台设备
    （USB 3.0 复合设备会打印 2~5 条 If 行，按行计数会把一台设备算成好几台）。
    """
    devices: List[UsbDevice] = []
    index = {}
    current_bus = ''

    for raw_line in (text or '').splitlines():
        line = raw_line.rstrip()
        if not line.strip() or 'Dev' not in line:
            continue

        is_root = bool(_ROOT_RE.search(line))
        port_match = _PORT_RE.search(line)
        if not port_match:
            continue
        dev = port_match.group(1)
        if is_root:
            current_bus = _ROOT_RE.search(line).group(1)

        class_match = _CLASS_RE.search(line)
        dev_class = class_match.group(1).strip() if class_match else ''
        driver_match = _DRIVER_RE.search(line)
        driver = driver_match.group(1).strip() if driver_match else ''
        speed_match = _SPEED_RE.search(line)
        speed = _parse_speed(speed_match.group(1) + speed_match.group(2)) if speed_match else 0.0
        if_match = _IF_RE.search(line)

        key = (current_bus, dev)
        existing = index.get(key)
        if existing is not None:
            # 多接口设备的后续 If 行：只补接口号，不新增设备
            if if_match and int(if_match.group(1)) not in existing.interfaces:
                existing.interfaces.append(int(if_match.group(1)))
            if speed > existing.speed_mbps:
                existing.speed_mbps = speed
            continue

        is_root_hub = is_root or dev_class == 'root_hub'
        device = UsbDevice(
            bus=current_bus,
            dev=dev,
            dev_class='root_hub' if is_root_hub else dev_class,
            driver=driver,
            speed_mbps=speed,
            is_root_hub=is_root_hub,
            interfaces=[int(if_match.group(1))] if if_match else [],
        )
        index[key] = device
        devices.append(device)

    return devices


def parse_sysfs_output(text: str) -> List[UsbDevice]:
    """解析 SYSFS_CMD 的输出：`1-2|5000|00|Product Name`"""
    devices = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or '|' not in line:
            continue
        fields = line.split('|')
        name = fields[0].strip()
        if not name:
            continue
        speed = _parse_speed(fields[1] if len(fields) > 1 else '')
        raw_class = (fields[2] if len(fields) > 2 else '').strip().lower()
        product = (fields[3] if len(fields) > 3 else '').strip()
        devices.append(UsbDevice(
            name=name,
            bus=name.split('-')[0] if '-' in name else name,
            dev=name,
            dev_class=_SYSFS_CLASS_NAMES.get(raw_class, raw_class or '未知类'),
            product=product,
            speed_mbps=speed,
            is_root_hub=bool(re.match(r'^usb\d+$', name)),
        ))
    return devices


def superspeed_devices(devices: List[UsbDevice],
                       min_speed: float = DEFAULT_MIN_SPEED_MBPS) -> List[UsbDevice]:
    """USB 3.x 上枚举出来的设备（排除 root hub 自己）"""
    return [d for d in devices if not d.is_root_hub and d.speed_mbps >= min_speed]


def usb2_devices(devices: List[UsbDevice]) -> List[UsbDevice]:
    """只在 2.0 速率上的设备（用于失败时说明「插了但没跑在 3.0」）"""
    return [d for d in devices
            if not d.is_root_hub and 0 < d.speed_mbps < DEFAULT_MIN_SPEED_MBPS]


def probe(ssh, min_speed: float = DEFAULT_MIN_SPEED_MBPS,
          retries: int = 0, interval: float = 1.0) -> Tuple[List[UsbDevice], str, List[str]]:
    """读取 USB 树，返回 (设备列表, 数据来源, 失败原因列表)

    retries 用于**热插拔**场景：设备刚插上时枚举要 1~2 秒，
    一键测试恰好在这之前读到空表就会误判。重试只在没找到 3.0 设备时进行。
    """
    notes: List[str] = []
    attempt = 0

    while True:
        devices, source, error = _probe_once(ssh, min_speed)
        # 重试时同一条错误会重复产生，去重后再进详情，避免刷三行一样的备注
        if error and error not in notes:
            notes.append(error)
        if superspeed_devices(devices, min_speed) or attempt >= max(0, retries):
            if attempt:
                notes.append(f'第 {attempt + 1} 次探测才发现 3.0 设备（热插拔枚举延迟）')
            return devices, source, notes
        attempt += 1
        time.sleep(max(0.0, interval))


def _probe_once(ssh, min_speed: float) -> Tuple[List[UsbDevice], str, str]:
    """单次探测：先 lsusb -t，失败退回 sysfs"""
    try:
        success, stdout, stderr = ssh.execute_command(LSUSB_CMD, timeout=LSUSB_TIMEOUT)
    except Exception as e:
        success, stdout, stderr = False, '', str(e)

    if success and (stdout or '').strip():
        devices = parse_lsusb_tree(stdout)
        if devices:
            return devices, 'lsusb -t', ''
        return devices, 'lsusb -t', 'lsusb -t 输出里没有解析到任何 USB 设备'
    lsusb_error = (stderr or '').strip() or '命令失败或输出为空'

    try:
        success, stdout, stderr = ssh.execute_command(SYSFS_CMD, timeout=SYSFS_TIMEOUT)
    except Exception as e:
        success, stdout, stderr = False, '', str(e)

    if success and (stdout or '').strip():
        devices = parse_sysfs_output(stdout)
        if devices:
            return devices, '/sys/bus/usb/devices', ''
        return devices, '/sys/bus/usb/devices', 'sysfs 里没有任何 USB 设备速率文件'
    sysfs_error = (stderr or '').strip() or '命令失败或输出为空'
    return [], '未获取', f'lsusb -t 不可用({lsusb_error})；sysfs 兜底也失败({sysfs_error})'


def format_detail(devices: List[UsbDevice], source: str,
                  min_speed: float = DEFAULT_MIN_SPEED_MBPS,
                  connector: str = '', notes: Optional[List[str]] = None) -> str:
    """详情文本：接口、判定标准、数据来源、完整设备列表（3.0 设备打标记）"""
    lines = []
    if connector:
        lines.append(f'接口类型: {connector}')
    lines.append(f'判定标准: 至少 1 个设备协商速率 >= {min_speed:g} Mbps（USB 3.x）')
    lines.append(f'数据来源: {source}')

    found = superspeed_devices(devices, min_speed)
    others = [d for d in devices if d not in found and not d.is_root_hub]

    if found:
        lines.append(f'USB 3.0 设备 ({len(found)} 个):')
        lines.extend(f'  [3.0] {d.label}' for d in found)
    else:
        lines.append('USB 3.0 设备: 无')

    if others:
        lines.append(f'其他设备 ({len(others)} 个, 未跑在 3.0):')
        lines.extend(f'  [2.0] {d.label}' for d in others)

    if notes:
        lines.append('备注:')
        lines.extend(f'  {note}' for note in notes if note)
    return '\n'.join(lines)
