# -*- coding: utf-8 -*-
"""电池休眠（shutdown）配置：按电池品牌选择寄存器地址字

背景
----
电量计的休眠配置不走标准 SBS 命令表，而是「先往 0x44 写命令码 0x02 + 2 字节地址字，
再从 0x44 窗口读写」。**同一套流程在不同品牌的电池上，地址字不同**：

=============  ======================  ================
品牌            Auto Ship Bit 地址字     关机时间地址字
=============  ======================  ================
格瑞普 Greenway  0xD0 0x4B               0x04 0x4C
九夷 Sunwoda     0x8B 0x48               0xAF 0x48
=============  ======================  ================

旧实现把格瑞普的地址字写死在代码里，换上九夷电池后：

1. 「读 Auto Ship Bit」打到 0xD0 0x4B —— 九夷那边这个地址没有内容，回读不到期望值
2. 于是判定「未开启」，又往 0xD0 0x4B 写 0x01 0x00 —— 写到了错误地址
3. 「读关机时间」同样打到 0x04 0x4C，回读不对 → 再往错地址写
4. 全程没有任何回读校验，日志只写「已配置」，现场看是成功的，实际什么都没配上

**为什么容易抄错**：格瑞普的地址字恰好就是回读数据的前两个字节
（`0xD0 0x4B` / `0x04 0x4C`），把「回读数据的固定前缀」当成「地址字」写进命令，
在格瑞普上完全不会暴露；九夷不是这样 —— 地址字 `0x8B 0x48`、`0xAF 0x48`，
回读数据仍是 `0x22 0xD0 0x4B 0x01 0x00` / `0x04 0x4C 0xE0 0x10`。换品牌就必错。

判定与校验
----------
每个品牌除了地址字，还有「写完之后回读应该是什么」：

* Auto Ship Bit：回读第 4 字节（下标 3）为 `0x01` 表示已开启，期望
  `0x22 0xD0 0x4B 0x01 0x00`
* 关机时间：回读前 4 字节为 `0x04 0x4C 0xE0 0x10` 表示已配置为 3 天
* 上锁：上锁后再读 0x44 应为 `0x00`

本模块只负责「按品牌选地址字 + 拼命令 + 解析回读」，下发与结论由
``DeviceTester._configure_battery_shutdown_mode`` 负责。
"""
import re
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

# 寄存器窗口：先写命令码 0x02 + 地址字，再读写同一地址
WINDOW_REG = '0x44'
SELECT_CODE = '0x02'          # 选中地址字
WRITE_CODE = '0x04'           # 地址字 + 数据

# 解密序列（两个品牌一致）：写 0x00 寄存器，两组操作各执行两遍
UNLOCK_OPS = ('0x88 0x20', '0x26 0x20', '0xFF 0xFF', '0xFF 0xFF')
UNLOCK_PASSES = 2

# 上锁：写命令码 0x02 + 0x30 0x00，上锁后再读 0x44 应为 0x00
LOCK_OPS = '0x30 0x00'
LOCK_EXPECT_BYTE = 0x00

# 回读格式（两个品牌一致）：
#   第 0 字节固定为 0x22（寄存器窗口头），第 1-2 字节为所选地址字，第 3-4 字节为数据
#   Auto Ship Bit: byte[3] == 0x01 表示已开启
#   关机时间:      byte[3:5] == (0xE0, 0x10) 表示已配置为 3 天
AUTOSHIP_HEADER = 0x22                # 回读固定首字节
AUTOSHIP_ENABLE_INDEX = 3             # 开关位在第 4 字节
AUTOSHIP_ENABLE_VALUE = 0x01
AUTOSHIP_WRITE_PAYLOAD = '0x01 0x00'  # 写入：使能 + 尾字节

SHUTDOWN_TIME_HEADER = 0x22           # 关机时间回读同样以 0x22 开头
SHUTDOWN_TIME_DATA = (0xE0, 0x10)     # 3 天数据（bytes[3:5]），地址字不在这里
SHUTDOWN_TIME_DATA_INDEX = 3          # 数据从第 4 字节开始
SHUTDOWN_TIME_WRITE_PAYLOAD = '0xE0 0x10'

READ_LEN = 5                  # 两个窗口都读 5 字节
READBACK_SETTLE_SECONDS = 0.3  # 写完立刻回读偶发拿旧值，稍等再读一次


@dataclass
class ShutdownProfile:
    """一个电池品牌的休眠配置档案"""
    name: str                                      # '九夷 Sunwoda'
    match: Tuple[str, ...] = ()                    # 厂商名关键字（大写比较）
    autoship_word: Tuple[str, str] = ('0xD0', '0x4B')   # Auto Ship Bit 地址字
    shutdown_word: Tuple[str, str] = ('0x04', '0x4C')   # 关机时间地址字

    @property
    def autoship_word_text(self) -> str:
        return ' '.join(self.autoship_word)

    @property
    def shutdown_word_text(self) -> str:
        return ' '.join(self.shutdown_word)

    @property
    def label(self) -> str:
        """详情里的一行： `九夷 Sunwoda | Auto Ship 0x8B 0x48 | 关机时间 0xAF 0x48`"""
        return (f'{self.name} | Auto Ship {self.autoship_word_text}'
                f' | 关机时间 {self.shutdown_word_text}')


# 内置品牌表。配置 `battery.shutdown_profiles` 存在时整表以配置为准（与 lte_signal 一致）
BUILTIN_PROFILES: Tuple[ShutdownProfile, ...] = (
    ShutdownProfile(
        name='格瑞普 Greenway',
        match=('GREENWAY', 'GREPOW', '格瑞普', '格瑞'),
        autoship_word=('0xD0', '0x4B'),
        shutdown_word=('0x04', '0x4C'),
    ),
    ShutdownProfile(
        name='九夷 Sunwoda',
        match=('SUNWODA', 'JIUYI', '九夷', '欣旺达'),
        autoship_word=('0x8B', '0x48'),
        shutdown_word=('0xAF', '0x48'),
    ),
)

_HEX_BYTE_RE = re.compile(r'0x([0-9a-fA-F]{1,2})')


# ------------------------------------------------------------------ 档案选择

def _as_word(value) -> Optional[Tuple[str, str]]:
    """把配置里的地址字解析成两个 '0xNN'

    接受三种写法：
        0x8B, 0x48                （YAML 列表，值可能是整数 139 / 72）
        ['0x8B', '0x48']
        '0x8B 0x48'
    非法时返回 None，由调用方决定是否用内置值兜底。
    """
    if isinstance(value, str):
        tokens = _HEX_BYTE_RE.findall(value)
    elif isinstance(value, (list, tuple)):
        tokens = []
        for item in value:
            text = str(item).strip()
            if text.lower().startswith('0x'):
                tokens.append(text[2:])
            elif text.isdigit():
                tokens.append(f'{int(text):x}')
            else:
                return None
    else:
        return None

    if len(tokens) != 2:
        return None
    try:
        return tuple(f'0x{int(t, 16):02X}' for t in tokens)
    except ValueError:
        return None


def profile_from_dict(data: Dict) -> Optional[ShutdownProfile]:
    """由配置字典构造档案；缺名字或地址字非法则返回 None（该条被忽略）"""
    if not isinstance(data, dict):
        return None
    name = str(data.get('name') or '').strip()
    if not name:
        return None

    autoship = _as_word(data.get('autoship_word'))
    shutdown = _as_word(data.get('shutdown_word'))
    if not autoship or not shutdown:
        return None

    raw_match = data.get('match') or []
    if isinstance(raw_match, str):
        raw_match = [raw_match]
    matches = tuple(str(m).strip() for m in raw_match if str(m).strip())

    return ShutdownProfile(name=name, match=matches,
                           autoship_word=autoship, shutdown_word=shutdown)


def load_profiles(config) -> List[ShutdownProfile]:
    """读取品牌表：配置里有 battery.shutdown_profiles 就用配置，否则用内置表"""
    raw = config.get('battery.shutdown_profiles') if config else None
    if raw:
        profiles = [p for p in map(profile_from_dict, raw) if p]
        if profiles:
            return profiles
    return [replace(p) for p in BUILTIN_PROFILES]


def forced_profile_name(config) -> str:
    """配置里 battery.force_profile 指定的品牌名（已知批次可跳过厂商名识别）"""
    if not config:
        return ''
    return str(config.get('battery.force_profile') or '').strip()


def identify_profile(manufacturer: str,
                     profiles: Sequence[ShutdownProfile]) -> Tuple[Optional[ShutdownProfile], str]:
    """按厂商名关键字匹配品牌档案，返回 (档案, 说明)

    厂商名来自 i2ctransfer 读的 SBS ManufacturerName（如 'Greenway'）。
    认不出来时返回 (None, 原因)，**调用方必须停止写入**：
    往不匹配的地址字写数据会把电量计配置写坏。
    """
    name = (manufacturer or '').strip()
    if not name:
        return None, '未读到电池厂商名，无法确定地址字（可在 config.yaml 配置 battery.force_profile）'

    upper = name.upper()
    for profile in profiles:
        for keyword in profile.match:
            if keyword.upper() in upper:
                return profile, f'按厂商名「{name}」匹配到 {profile.name}'

    known = '、'.join(p.name for p in profiles)
    return None, (f'电池厂商「{name}」不在品牌表中（已知: {known}），'
                  f'为避免写错地址已跳过休眠配置；可在 config.yaml 增加 '
                  f'battery.shutdown_profiles 条目或指定 battery.force_profile')


def resolve_profile(config, manufacturer: str) -> Tuple[Optional[ShutdownProfile], str]:
    """确定用哪个品牌档案：force_profile 优先，其次按厂商名识别"""
    profiles = load_profiles(config)

    forced = forced_profile_name(config)
    if forced:
        for profile in profiles:
            if profile.name == forced:
                return profile, f'battery.force_profile 指定为 {profile.name}'
        for profile in profiles:
            if forced.upper() in profile.name.upper():
                return profile, f'battery.force_profile「{forced}」匹配到 {profile.name}'
        return None, (f'battery.force_profile「{forced}」不在品牌表中'
                      f'（已知: {"、".join(p.name for p in profiles)}）')

    return identify_profile(manufacturer, profiles)


# ------------------------------------------------------------------ 命令拼装

def _word_text(word: Tuple[str, str]) -> str:
    return ' '.join(word)


def build_unlock(bus: int, addr: str) -> str:
    """解密（两组操作各执行两遍，共 8 条 i2cset）"""
    ops = ' && '.join(f'i2cset -y {bus} {addr} 0x00 {op} i' for op in UNLOCK_OPS)
    return ' && '.join([ops] * UNLOCK_PASSES)


def build_read_autoship(bus: int, addr: str, profile: ShutdownProfile) -> str:
    """读 Auto Ship Bit：先选地址字，再从 0x44 读 5 字节"""
    return (f'i2cset -y {bus} {addr} {WINDOW_REG} {SELECT_CODE} '
            f'{_word_text(profile.autoship_word)} i && '
            f'i2cget -y {bus} {addr} {WINDOW_REG} i {READ_LEN}')


def build_enable_autoship(bus: int, addr: str, profile: ShutdownProfile) -> str:
    """开启 Auto Ship Bit：直接写，不重复选地址字（读步骤已选过）"""
    return (f'i2cset -y {bus} {addr} {WINDOW_REG} {WRITE_CODE} '
            f'{_word_text(profile.autoship_word)} {AUTOSHIP_WRITE_PAYLOAD} i')


def build_read_shutdown_time(bus: int, addr: str, profile: ShutdownProfile) -> str:
    """读关机时间：选关机时间地址字，再读 5 字节\n\n    回读格式：0x22 <word[0]> <word[1]> <data[0]> <data[1]>\n    3 天对应 data = 0xE0 0x10\n    """
    return (f'i2cset -y {bus} {addr} {WINDOW_REG} {SELECT_CODE} '
            f'{_word_text(profile.shutdown_word)} i && '
            f'i2cget -y {bus} {addr} {WINDOW_REG} i {READ_LEN}')


def build_set_shutdown_time(bus: int, addr: str, profile: ShutdownProfile) -> str:
    """配置关机时间 3 天：直接写，不重复选地址字（读步骤已选过）"""
    return (f'i2cset -y {bus} {addr} {WINDOW_REG} {WRITE_CODE} '
            f'{_word_text(profile.shutdown_word)} {SHUTDOWN_TIME_WRITE_PAYLOAD} i')


def build_lock(bus: int, addr: str) -> str:
    """上锁：写 0x44 0x02 0x30 0x00，退出码 0 即成功，不做回读校验"""
    return f'i2cset -y {bus} {addr} {WINDOW_REG} {SELECT_CODE} {LOCK_OPS} i'


def build_read_lock_state(bus: int, addr: str) -> str:
    """上锁后读 0x44（仅用于调试日志，不用于判定）

    实测上锁后 0x44 回读为 0x16，不是 0x00；
    上锁成功与否以命令退出码为准，此函数只供现场排查调用。
    """
    return f'i2cget -y {bus} {addr} {WINDOW_REG}'


# ------------------------------------------------------------------ 回读解析

def parse_bytes(text: str) -> List[int]:
    """把 `0x22 0xd0 0x4b 0x01 0x00` 解析成整数列表；解析不出返回空列表"""
    return [int(t, 16) for t in _HEX_BYTE_RE.findall(text or '')]


def format_bytes(values: Sequence[int]) -> str:
    return ' '.join(f'0x{v:02x}' for v in values)


def autoship_state(text: str, profile: ShutdownProfile) -> Tuple[bool, str]:
    """判定 Auto Ship Bit 是否已开启，返回 (是否已开启, 说明)

    回读格式：0x22 <addr_word[0]> <addr_word[1]> <enable_byte> <trailing>
    - byte[0] 固定为 0x22
    - byte[1:3] 为所选地址字（按品牌区分），可用来确认读的是正确的配置项
    - byte[3] == 0x01 表示已开启
    """
    values = parse_bytes(text)
    if not values:
        return False, '回读为空或无法解析'
    if len(values) <= AUTOSHIP_ENABLE_INDEX:
        return False, f'回读只有 {len(values)} 字节，不足 {AUTOSHIP_ENABLE_INDEX + 1} 字节'

    if values[0] != AUTOSHIP_HEADER:
        return False, (f'回读首字节异常: 0x{values[0]:02x}，期望 0x22 '
                       f'({format_bytes(values)})')

    # 第 1-2 字节应是本品牌的地址字，验证后才有意义
    word_ints = tuple(int(b, 16) for b in profile.autoship_word)
    if tuple(values[1:3]) != word_ints:
        return False, (f'回读地址字节 {format_bytes(values[1:3])} '
                       f'与品牌 {profile.name} 期望地址字 {profile.autoship_word_text} 不符，'
                       f'完整回读: {format_bytes(values)}')

    enabled = values[AUTOSHIP_ENABLE_INDEX] == AUTOSHIP_ENABLE_VALUE
    if enabled:
        return True, f'已开启 ({format_bytes(values)})'
    return False, f'未开启 ({format_bytes(values)})'


def shutdown_time_state(text: str, profile: ShutdownProfile) -> Tuple[bool, str]:
    """判定关机时间是否已配置为 3 天，返回 (是否已配置, 说明)

    回读格式：0x22 <addr_word[0]> <addr_word[1]> <data[0]> <data[1]>
    - byte[0] 固定为 0x22
    - byte[1:3] 为所选地址字（按品牌区分）
    - byte[3:5] 为数据部分，3 天固定为 0xE0 0x10
    """
    values = parse_bytes(text)
    if not values:
        return False, '回读为空或无法解析'

    need = SHUTDOWN_TIME_DATA_INDEX + len(SHUTDOWN_TIME_DATA)
    if len(values) < need:
        return False, f'回读只有 {len(values)} 字节，不足 {need} 字节'

    if values[0] != SHUTDOWN_TIME_HEADER:
        return False, (f'回读首字节异常: 0x{values[0]:02x}，期望 0x22 '
                       f'({format_bytes(values)})')

    word_ints = tuple(int(b, 16) for b in profile.shutdown_word)
    if tuple(values[1:3]) != word_ints:
        return False, (f'回读地址字节 {format_bytes(values[1:3])} '
                       f'与品牌 {profile.name} 期望地址字 {profile.shutdown_word_text} 不符，'
                       f'完整回读: {format_bytes(values)}')

    data_slice = tuple(values[SHUTDOWN_TIME_DATA_INDEX:
                               SHUTDOWN_TIME_DATA_INDEX + len(SHUTDOWN_TIME_DATA)])
    if data_slice == SHUTDOWN_TIME_DATA:
        return True, f'已配置为 3 天 ({format_bytes(values)})'
    return False, (f'不是 3 天 ({format_bytes(values)}，'
                   f'数据位 {format_bytes(data_slice)}，期望 {format_bytes(SHUTDOWN_TIME_DATA)})')


def lock_state(text: str) -> Tuple[bool, str]:
    """判定是否已上锁：上锁后回读应为 0x00"""
    values = parse_bytes(text)
    if not values:
        return False, '回读为空或无法解析'
    if all(v == LOCK_EXPECT_BYTE for v in values):
        return True, f'已上锁 (回读 {format_bytes(values)})'
    return False, f'回读非 0x00 ({format_bytes(values)})，可能未上锁'
