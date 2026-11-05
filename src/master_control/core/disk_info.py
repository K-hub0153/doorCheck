# -*- coding: utf-8 -*-
"""内置硬盘识别（把 U 盘 / 移动硬盘排除在外）

质检流程里 USB 3.0 检测要求**插着一个 U 盘或移动硬盘**，两块东西都会在
`lsblk` 里以 `sdX` 出现。早期实现直接 `lsblk ... | head -1` 取第一块盘，
接上 U 盘后扫描到的设备卡片和硬盘检测就会把 U 盘当成「本机硬盘」——
插拔顺序一变（U 盘先枚举成 sda）就误显、误判。

这里按**是不是可移动设备**来区分，两条证据缺一不可地看：

1. `/sys/block/<name>/removable` == 1 —— SD 卡、部分 U 盘
2. `/sys/block/<name>` 的软链路径里带 `usb` —— USB 桥接的 U 盘 / 移动硬盘。
   U 盘常常 `removable` 是 0（比如带 UASP 的盘），只看 removable 会漏，
   所以必须同时看总线；只用 `lsblk -o TRAN` 不行，老版本 util-linux
   没有这一列，会整条命令报错。

判定为可移动的一律不进「内置硬盘」列表，而是单独返回给上层做说明
（详情里写明「已忽略 U 盘」，避免产线看不懂为什么少了块盘）。

同一条命令一次读回 lsblk 与 sysfs，避免每条 SSH 多一轮往返。
"""
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# sysfs 段的分隔标记（lsblk 输出里不会出现这个字符串）
SYSFS_MARK = '##SYSFS##'

# 一次 SSH 读回：磁盘表 + 每块盘的可移动标记/总线归属
#
# 总线归属用 `cd <link> && pwd -P` 解析真实路径（busybox 的 readlink 不一定带 -f，
# 而 pwd -P 在所有 POSIX 环境里都可用），真实路径里出现 usb 即 USB 桥接盘。
PROBE_CMD = (
    'lsblk -d -n -P -o NAME,MODEL,SIZE 2>/dev/null; '
    "echo '" + SYSFS_MARK + "'; "
    'for d in /sys/block/*; do '
    'n=$(basename "$d"); '
    'r=$(cat "$d/removable" 2>/dev/null); '
    'p=$(cd "$d" 2>/dev/null && pwd -P); '
    'case "$p" in *usb*) u=1;; *) u=0;; esac; '
    "printf '%s|%s|%s\\n' \"$n\" \"${r:-0}\" \"$u\"; "
    'done'
)
PROBE_TIMEOUT = 15

# 只要整盘设备名，排除 loop/ram/zram/sr 这些
_DISK_LINE_RE = re.compile(r'NAME="(sd|nvme|mmcblk)')
# sysfs 兜底时同样只要整盘：sda / nvme0n1 / mmcblk0（不认 sda1、mmcblk0p1 分区）
_SYSFS_DISK_RE = re.compile(r'^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+)$')
_NAME_RE = re.compile(r'NAME="([^"]*)"')
_MODEL_RE = re.compile(r'MODEL="([^"]*)"')
_SIZE_RE = re.compile(r'SIZE="([^"]*)"')

# 多块内置盘同时存在时的优先级：NVMe > SATA > eMMC
_BUS_RANK = {'nvme': 0, 'sata': 1, 'mmc': 2}

# 型号 → 品牌（展示用，按顺序匹配，先命中先用）
_VENDOR_KEYWORDS = (
    ('SAMSUNG', 'Samsung'),
    ('WESTERN', 'Western Digital'),
    ('WD', 'Western Digital'),
    ('SEAGATE', 'Seagate'),
    ('TOSHIBA', 'Toshiba'),
    ('KINGSTON', 'Kingston'),
    ('SANDISK', 'SanDisk'),
    ('INTEL', 'Intel'),
    ('OSC', 'OSC'),
    ('FORESEE', 'FORESEE'),
    ('BIWIN', 'BIWIN'),
    ('LONGSYS', 'Longsys'),
)


@dataclass
class DiskInfo:
    """lsblk 里的一整块盘"""
    name: str = ''          # 'sda' / 'nvme0n1' / 'mmcblk0'
    model: str = ''         # 可能为空（eMMC、无型号的桥接盘）
    size: str = ''          # '465.8G'
    removable: bool = False  # /sys/block/<name>/removable == 1
    is_usb: bool = False     # sysfs 路径里带 usb

    @property
    def bus(self) -> str:
        """总线类型：nvme / sata / mmc / usb"""
        if self.is_usb:
            return 'usb'
        if self.name.startswith('nvme'):
            return 'nvme'
        if self.name.startswith('mmcblk'):
            return 'mmc'
        return 'sata'

    @property
    def is_internal(self) -> bool:
        """内置盘（不是 U 盘 / 移动硬盘 / SD 卡）"""
        return not (self.removable or self.is_usb)

    @property
    def display_model(self) -> str:
        """型号为空时退回设备名"""
        return self.model or self.name

    @property
    def vendor(self) -> str:
        """从型号里认品牌（展示用，认不出就返回空）"""
        model_upper = self.model.upper()
        if not model_upper:
            return ''
        for keyword, name in _VENDOR_KEYWORDS:
            if keyword in model_upper:
                return name
        return ''

    @property
    def label(self) -> str:
        """一行展示：`sda Samsung SSD 870 EVO 500GB (465.8G)`

        没有型号（sysfs 兜底、eMMC）时不要重复成 `sda sda`。
        """
        parts = [self.name]
        if self.model and self.model != self.name:
            parts.append(self.model)
        if self.size:
            parts.append(f'({self.size})')
        return ' '.join(parts)


def parse(text: str) -> List[DiskInfo]:
    """解析 PROBE_CMD 的输出（lsblk 段 + sysfs 段）"""
    disks: List[DiskInfo] = []
    sysfs: Dict[str, Tuple[bool, bool]] = {}
    in_sysfs = False

    for raw_line in (text or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line == SYSFS_MARK:
            in_sysfs = True
            continue

        if in_sysfs:
            fields = line.split('|')
            if not fields or not fields[0].strip():
                continue
            name = fields[0].strip()
            removable = len(fields) > 1 and fields[1].strip() == '1'
            is_usb = len(fields) > 2 and fields[2].strip() == '1'
            sysfs[name] = (removable, is_usb)
            continue

        if not _DISK_LINE_RE.match(line):
            continue

        name_match = _NAME_RE.search(line)
        model_match = _MODEL_RE.search(line)
        size_match = _SIZE_RE.search(line)
        disks.append(DiskInfo(
            name=name_match.group(1).strip() if name_match else '',
            model=model_match.group(1).strip() if model_match else '',
            size=size_match.group(1).strip() if size_match else '',
        ))

    for disk in disks:
        removable, is_usb = sysfs.get(disk.name, (False, False))
        disk.removable = removable
        disk.is_usb = is_usb

    if not disks:
        # 固件里没有 lsblk（裁剪版 util-linux）时，sysfs 段仍能给出设备名，
        # 至少能把「有没有内置盘、哪个是 U 盘」判对，只是没有型号和容量
        for name in sysfs:
            if _SYSFS_DISK_RE.match(name):
                removable, is_usb = sysfs[name]
                disks.append(DiskInfo(name=name, removable=removable, is_usb=is_usb))

    return disks


def probe(ssh, timeout: int = PROBE_TIMEOUT) -> Tuple[List[DiskInfo], str]:
    """执行探测命令，返回 (磁盘列表, 失败原因)

    ssh 需要提供 `execute_command(cmd, timeout) -> (success, stdout, stderr)`
    （即 SSHClient）。Discovery 用的是裸 paramiko，直接发 PROBE_CMD 后调 parse。
    """
    try:
        success, stdout, stderr = ssh.execute_command(PROBE_CMD, timeout=timeout)
    except Exception as e:  # 连接已断、命令超时等
        return [], f'读取磁盘信息异常: {e}'

    if not success:
        return [], (stderr or '').strip() or 'lsblk 命令执行失败'
    return parse(stdout), ''


def internal_disks(disks: List[DiskInfo]) -> List[DiskInfo]:
    """内置硬盘，按 NVMe > SATA > eMMC 排序"""
    return sorted((d for d in disks if d.is_internal),
                  key=lambda d: _BUS_RANK.get(d.bus, 9))


def movable_disks(disks: List[DiskInfo]) -> List[DiskInfo]:
    """被排除掉的可移动设备（U 盘 / 移动硬盘 / SD 卡）"""
    return [d for d in disks if not d.is_internal]


def pick(disks: List[DiskInfo]) -> Optional[DiskInfo]:
    """挑一块代表本机的硬盘（设备卡片用），没有内置盘时返回 None"""
    internal = internal_disks(disks)
    return internal[0] if internal else None


def ignored_note(disks: List[DiskInfo]) -> str:
    """给详情用的说明行：排除了哪些可移动设备（没有就返回空串）"""
    movable = movable_disks(disks)
    if not movable:
        return ''
    items = '、'.join(d.label for d in movable)
    return f'已忽略 {len(movable)} 个可移动设备（U 盘/移动硬盘，不计入硬盘）: {items}'
