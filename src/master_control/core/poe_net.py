# -*- coding: utf-8 -*-
"""POE 网段自动识别

POE 检测要在**设备自己**的 POE 网口（默认 eth1）所在网段里 ping 扫描，
而不是在 PC 端配置里写死一个网段。310P 与 RK3576 的 eth1 网段随时可能被
改动（22.0.0.1/24、198.19.0.1/24 ……），写死在 config.yaml 里迟早与现场
漂移，检测就会去 ping 一个根本不存在的网段，白白通过 30 秒后判失败。

识别顺序：

1. 设备上 `ip -4 -o addr show dev <iface>` 读 IPv4 地址与掩码
2. 失败时退回 `ifconfig <iface>`（老固件没有 ip 命令）
3. 都拿不到，或只拿到 169.254 链路本地地址 → 回落到 config.yaml 的
   `network.poe_subnet`（保留手工配置作为兜底，不再是主路径）

扫描范围固定为「接口 IP 所在的 /24」：eth1 实测都是 /24（22.0.0.1/24），
掩码更短（如 /16）时也只扫自己那一段 254 个地址，避免一次 ping 几万个 IP。
"""
import ipaddress
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

DEFAULT_IFACE = 'eth1'
DEFAULT_FALLBACK_SUBNET = '198.19.0.0/24'

# 探测接口地址的命令（先 ip 后 ifconfig）
IP_CMD = 'ip -4 -o addr show dev {iface}'
IFCONFIG_CMD = 'ifconfig {iface}'
PROBE_TIMEOUT = 10
# 254 个地址并发 ping，单包等 1s，30s 足够
PING_TIMEOUT = 30

# `ip -4 -o addr show`：inet 22.0.0.1/24 brd 22.0.0.255 scope global eth1
_INET_CIDR_RE = re.compile(r'inet\s+(?:addr:)?\s*(\d+\.\d+\.\d+\.\d+)/(\d+)')
# `ifconfig`：inet 22.0.0.1  netmask 255.255.255.0  broadcast 22.0.0.255
# 也兼容 ifconfig eth1:0 的 `inet addr:22.0.0.1`
_INET_MASK_RE = re.compile(
    r'inet\s+(?:addr:)?\s*(\d+\.\d+\.\d+\.\d+)\s+'
    r'(?:netmask\s+(\S+)|Mask:(\S+))')


@dataclass
class ScanTarget:
    """一次 POE 扫描的目标网段"""
    prefix: str                 # '22.0.0'，用于拼 ping 地址与邻居表过滤
    cidr: str = ''              # '22.0.0.1/24'，设备上的原始写法，仅用于日志
    interface: str = ''         # 实际读到的网口名，空表示没拿到
    source: str = '自动识别'      # 网段来源，写进日志与检测详情
    note: str = ''              # 回落原因等补充说明

    @property
    def label(self) -> str:
        return f'{self.prefix}.x'

    @property
    def known_interface(self) -> bool:
        """是否确认了网口（决定邻居表按设备过滤还是按网段过滤）"""
        return bool(self.interface)


def _is_usable(ip: str) -> bool:
    """能拿来做扫描的地址：排除空地址、回环、链路本地（未拿到 DHCP 的自动地址）"""
    if not ip or ip.startswith('127.') or ip == '0.0.0.0':
        return False
    return not ip.startswith('169.254.')


def ipv4_from_ip_output(text: str) -> List[Tuple[str, int]]:
    """解析 `ip -4 addr show` 输出，返回 [(ip, 前缀长度)]"""
    pairs = []
    for ip, prefix in _INET_CIDR_RE.findall(text or ''):
        try:
            prefix_int = int(prefix)
        except ValueError:
            continue
        if 0 < prefix_int <= 32 and _is_usable(ip):
            pairs.append((ip, prefix_int))
    return pairs


def ipv4_from_ifconfig_output(text: str, iface: str = '') -> List[Tuple[str, int]]:
    """解析 ifconfig 输出，返回 [(ip, 前缀长度)]；掩码写法兼容点分与 0x 十六进制

    iface 非空时**只取该接口那一段**：接口不存在时 busybox 的 ifconfig 会把
    整机网卡全打印出来，不按名字分段就会拿到 eth0（192.168.x）或 docker0 的地址。
    """
    text = text or ''
    if iface:
        blocks, current, keep, headers = [], [], False, 0
        for line in text.splitlines():
            if re.match(r'^\S[^\s:]*[: ]', line) and not line[:1].isspace():
                if current:
                    blocks.append((keep, current))
                keep = line.split(':')[0].split()[0] == iface
                current = [line]
                headers += 1
            else:
                current.append(line)
        if current:
            blocks.append((keep, current))
        if headers:
            # 有分段标题就只认名字匹配的那段；没有匹配说明该网口不存在，
            # 返回空让上层去回落，绝不能拿别的网卡的地址顶替
            matched = [blk for keep, blk in blocks if keep]
            if not matched:
                return []
            text = '\n'.join('\n'.join(blk) for blk in matched)

    pairs = []
    for ip, mask_dotted, mask_colon in _INET_MASK_RE.findall(text):
        if not _is_usable(ip):
            continue
        mask = (mask_dotted or mask_colon or '').strip()
        try:
            if mask.startswith('0x'):
                prefix_int = bin(int(mask, 16)).count('1')
            else:
                prefix_int = ipaddress.IPv4Network(f'0.0.0.0/{mask}').prefixlen
        except ValueError:
            continue
        if prefix_int:
            pairs.append((ip, prefix_int))
    return pairs


def _prefix_of(ip: str) -> str:
    """取 IP 的前三段，作为 /24 扫描段的过滤前缀"""
    return '.'.join(ip.split('.')[:3])


def target_from_ipv4(ip: str, prefixlen: Optional[int] = None,
                     interface: str = '', source: str = '自动识别') -> ScanTarget:
    """由接口地址构造扫描目标（无论掩码多长，都只扫这个地址所在的 /24）"""
    cidr = f'{ip}/{prefixlen}' if prefixlen else ip
    return ScanTarget(prefix=_prefix_of(ip), cidr=cidr,
                      interface=interface, source=source)


def target_from_subnet(subnet: str, interface: str = '',
                      reason: str = '') -> ScanTarget:
    """由 config.yaml 的 poe_subnet（如 198.19.0.0/24）构造兜底扫描目标"""
    text = str(subnet or '').strip() or DEFAULT_FALLBACK_SUBNET
    try:
        network = ipaddress.IPv4Network(text, strict=False)
    except ValueError:
        network = ipaddress.IPv4Network(DEFAULT_FALLBACK_SUBNET, strict=False)
        reason = (reason + '；' if reason else '') + f'配置的网段 {text!r} 非法，已用默认值'
    # 掩码比 /24 短时只取网段起始地址那一段，与自动识别保持同样的扫描规模
    base = str(network.network_address)
    return ScanTarget(prefix=_prefix_of(base), cidr=str(network),
                      interface=interface, source='配置回落', note=reason)


def probe_interface(ssh, iface: str = DEFAULT_IFACE) -> Tuple[Optional[str], Optional[int], str]:
    """在设备上读 POE 网口的 IPv4 地址，返回 (ip, 前缀长度, 失败原因)"""
    ok, stdout, stderr = ssh.execute_command(IP_CMD.format(iface=iface),
                                            timeout=PROBE_TIMEOUT)
    if ok:
        pairs = ipv4_from_ip_output(stdout)
        if pairs:
            return pairs[0][0], pairs[0][1], ''
        reason = f'{iface} 未配置 IPv4 地址'
    else:
        reason = f'{iface} 读取失败: {(stderr or "").strip() or "命令非零退出"}'

    # 老固件可能没有 ip 命令，退回 ifconfig
    ok, stdout, stderr = ssh.execute_command(IFCONFIG_CMD.format(iface=iface),
                                            timeout=PROBE_TIMEOUT)
    if ok:
        pairs = ipv4_from_ifconfig_output(stdout, iface)
        if pairs:
            return pairs[0][0], pairs[0][1], ''
        if 'not found' in (stdout or '').lower() or 'error' in (stderr or '').lower():
            return None, None, f'{iface} 不存在: {(stderr or stdout).strip()[:80]}'
        return None, None, f'{iface} 未配置 IPv4 地址'
    return None, None, reason


def resolve(ssh, iface: str = DEFAULT_IFACE,
           fallback_subnet: str = DEFAULT_FALLBACK_SUBNET) -> ScanTarget:
    """确定 POE 扫描网段：优先设备网口自动识别，失败回落配置"""
    ip, prefixlen, reason = probe_interface(ssh, iface)
    if ip:
        return target_from_ipv4(ip, prefixlen, interface=iface)
    return target_from_subnet(fallback_subnet, interface='', reason=reason)


# ---------------------------------------------------------------- 命令

def build_ping_command(target: ScanTarget) -> str:
    """并发 ping 该网段 2..254，不等待应答（靠 ARP 表统计在线设备）

    跳过 .1 与 .255：设备自己在 .1、.255 是广播地址，两者都不会进邻居表。
    """
    return (f"for i in {{2..254}}; do "
            f"ping -c 1 -W 1 {target.prefix}.$i > /dev/null 2>&1 & done; wait")


def build_flush_command(target: ScanTarget) -> str:
    """清空 ARP 表，避免上一台设备的残留邻居被误计入

    确认了网口就只清这个网口，别动 eth0（SSH 走的链路）。
    """
    if target.known_interface:
        return f'ip neigh flush dev {target.interface}'
    return 'ip neigh flush all'


def build_neigh_detail_command(target: ScanTarget) -> str:
    """列出本次扫描网段的邻居表，写进日志方便现场排查"""
    if target.known_interface:
        return f'ip neigh show dev {target.interface}'
    return f"ip neigh show | grep '{target.prefix}'"


def build_neigh_count_command(target: ScanTarget) -> str:
    """统计在线（REACHABLE）设备数"""
    return build_neigh_detail_command(target) + " | grep 'REACHABLE' | wc -l"


def count_online(ssh, target: ScanTarget, flush: bool = True) -> Tuple[Optional[int], str, str]:
    """清邻居表 → 并发 ping 整段 → 统计 REACHABLE 数量

    供「打印前 POE 必须已拔除」这种只关心**当前有几路在线**的场景使用，
    与 `test_poe_ports()` 的判定（够不够 expected 路）分开。

    `flush=True` 是必须的：拔掉 POE 设备后 ARP 表里会残留 REACHABLE/STALE 条目，
    不清表直接数会把刚拔掉的设备算进去，校验永远不通过。清表后重新 ping，
    不回应答的地址不会重新进表，数量才是真实的。

    Returns:
        (在线数, 邻居表文本, 失败原因)
        在线数为 None 表示统计失败，此时第三条是失败原因
    """
    if flush:
        ssh.execute_command(build_flush_command(target), timeout=PROBE_TIMEOUT)

    ok, _, stderr = ssh.execute_command(build_ping_command(target), timeout=PING_TIMEOUT)
    if not ok:
        return None, '', f'ping 扫描失败: {(stderr or "").strip() or "命令非零退出"}'

    _, neighbours, _ = ssh.execute_command(build_neigh_detail_command(target), timeout=PROBE_TIMEOUT)

    ok, stdout, stderr = ssh.execute_command(build_neigh_count_command(target),
                                             timeout=PROBE_TIMEOUT)
    text = (stdout or '').strip()
    if not ok or not text.isdigit():
        return None, neighbours, (
            f'邻居表统计失败: {(stderr or "").strip() or text or "无输出"}')
    return int(text), neighbours, ''
