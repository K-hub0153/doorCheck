# -*- coding: utf-8 -*-
"""4G 模组识别与信号换算

不同模组厂商的 AT 命令、响应格式、以及信号编码都不同，但**检测标准统一**
（见 `LTE_THRESHOLDS`）：识别出厂商后，按该厂商的公式把码值换算成真实
dBm/dB，再按同一套阈值判定，最后把厂商名与各字段值一起展示。

| 厂商 | 识别关键字 | AT 命令 | 响应 | 换算 |
|------|-----------|---------|------|------|
| 移远 Quectel | EC800K 等 | `AT+QENG="servingcell"` | `+QENG: "servingcell",...` | 模组直接给真实值，不换算 |
| 龙尚 Longsung | SM-208 等 | `AT+CPSI?` | `+CPSI: LTE,Online,...` | 码值换算，见下表 |

龙尚码值 → 真实值（场测确认）：

    RSRP  60 → 60 - 140        = -80 dBm
    RSRQ  26 → (26 - 40) / 2   =  -7 dB
    SINR  21 → 21              =  21 dB   （模组直接给真实值）
    RSSI  64 → 64 - 110        = -46 dBm

注意：龙尚 `+CPSI` 里 **RSRQ 在 RSRP 之前**（RSRQ=第 11 个字段、RSRP=第 12 个），
与移远 `+QENG` 的先后顺序相反，按字段序号取值时不能想当然。
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

DEFAULT_AT_PORT = '/dev/ttyUSB2'

# 判定标准：与移远时期完全一致，不随模组厂商变化
# RSRP >= -90 dBm、SINR >= 5 dB、RSRQ >= -13 dB
LTE_THRESHOLDS = {'rsrp': -90, 'sinr': 5, 'rsrq': -13}
# 参与判定的字段（缺任一即无法判定）
JUDGE_KEYS = ('rsrp', 'sinr', 'rsrq')
# 展示顺序：判定字段在前，附加字段在后
METRIC_ORDER = ('rsrp', 'rsrq', 'sinr', 'rssi')
METRIC_LABEL = {'rsrp': 'RSRP', 'rsrq': 'RSRQ', 'sinr': 'SINR', 'rssi': 'RSSI'}
METRIC_UNIT = {'rsrp': 'dBm', 'rsrq': 'dB', 'sinr': 'dB', 'rssi': 'dBm'}

# 模组用这些码值表示「没测到」，直接换算会得到 115 dBm 这种假数据
UNKNOWN_CODES = (255, 32768)

# +CPSI 响应字段序号（场测确认为 14 字段格式，SINR 结尾）
# 0=LTE 1=Online 2=460-00 3=0x1145 4=19219514 5=179 6=EUTRAN-BAND3
# 7=1350 8=5(dl_bw) 9=5(ul_bw) 10=RSRQ 11=RSRP 12=RSSI 13=SINR
CPSI_FIELDS = {'rsrq': 10, 'rsrp': 11, 'rssi': 12, 'sinr': 13}
CPSI_MIN_FIELDS = 14
# 龙尚模组会在响应里夹时间戳（如 07:43:57.210），会把字段序号整体错位，取值前先剔除
_TIMESTAMP_RE = re.compile(r'^\d{1,2}:\d{2}:\d{2}(\.\d+)?$')

# 龙尚码值换算：字段 → (公式文字, 换算函数)
LONGSUNG_DECODE: Dict[str, Tuple[str, object]] = {
    'rsrp': ('码值 - 140', lambda c: c - 140),
    'rsrq': ('(码值 - 40) / 2', lambda c: (c - 40) / 2),
    'rssi': ('码值 - 110', lambda c: c - 110),
    'sinr': ('码值', lambda c: c),
}

# 内置模组表：config.yaml 里没有 lte.modules 时使用
BUILTIN_MODULES = (
    {'name': '移远', 'at_command': 'AT+QENG="servingcell"', 'at_port': '/dev/ttyUSB2',
     'decoder': 'qeng', 'match': ('EC800K', 'EC200', 'EC600', 'Quectel')},
    {'name': '龙尚', 'at_command': 'AT+CPSI?', 'at_port': '/dev/ttyACM1',
     'decoder': 'cpsi', 'match': ('SM-208', 'U9300', 'Longsung', '龙尚'),
     'send_mode': 'echo'},
)


@dataclass
class ModuleSpec:
    """一个模组厂商的识别方式与取信号方式"""
    name: str
    at_command: str
    at_port: str = DEFAULT_AT_PORT
    decoder: str = 'qeng'
    match: Tuple[str, ...] = ()
    # printf：`printf 'cmd\r\n'`（转义可控）；echo：`echo -e "cmd\r"`（龙尚场测命令形态）
    send_mode: str = 'printf'

    def matches(self, text: str) -> bool:
        upper = (text or '').upper()
        return any(k.upper() in upper for k in self.match)


@dataclass
class Metric:
    """一个信号字段：码值 + 换算后的真实值"""
    key: str
    value: Optional[float] = None
    code: Optional[int] = None
    formula: str = ''          # 换算说明，空表示模组直出真实值

    @property
    def label(self) -> str:
        return METRIC_LABEL.get(self.key, self.key.upper())

    @property
    def unit(self) -> str:
        return METRIC_UNIT.get(self.key, '')

    @property
    def value_text(self) -> str:
        return fmt_num(self.value)


@dataclass
class Outcome:
    """一次 AT 读取的解析结果"""
    ok: bool = False
    metrics: List[Metric] = field(default_factory=list)
    reason: str = ''


def fmt_num(value) -> str:
    """-7.0 → '-7'，避免面板上出现 '-7.0 dB' 这种别扭写法"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------- 配置

def load_modules(cfg=None, at_port: Optional[str] = None) -> List[ModuleSpec]:
    """从 config.yaml 的 lte.modules 读取模组表，缺失或非法时回落到内置表

    at_port：`lte.at_port` 显式指定的 AT 口，覆盖模组表里的端口。
    """
    raw = cfg.get('lte.modules') if cfg is not None else None
    if not raw:
        raw = BUILTIN_MODULES

    specs = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name, at_command = item.get('name'), item.get('at_command')
        if not name or not at_command:
            continue
        match = item.get('match') or ()
        if isinstance(match, str):
            match = (match,)
        specs.append(ModuleSpec(
            name=str(name),
            at_command=str(at_command),
            at_port=str(at_port or item.get('at_port') or DEFAULT_AT_PORT),
            decoder=str(item.get('decoder') or 'qeng'),
            match=tuple(str(m) for m in match),
            send_mode=str(item.get('send_mode') or 'printf'),
        ))
    return specs or [ModuleSpec(**m) for m in BUILTIN_MODULES]


def configured_at_port(cfg=None) -> Optional[str]:
    """config.yaml 的 lte.at_port：显式指定 AT 口

    设置后同时作用于识别用的 ATI 与模组自己的信号读取命令（整批模组换了 AT 口
    时只改一处配置），未设置时识别用 DEFAULT_AT_PORT、读取用模组表里的端口。
    """
    if cfg is None:
        return None
    value = cfg.get('lte.at_port')
    return str(value) if value else None


def forced_module_name(cfg=None) -> Optional[str]:
    """config.yaml 的 lte.force_module：强制指定厂商，跳过 ATI 识别"""
    if cfg is None:
        return None
    value = cfg.get('lte.force_module')
    return str(value) if value else None


# ---------------------------------------------------------------- 命令

def ati_spec(at_port: str = DEFAULT_AT_PORT) -> ModuleSpec:
    """识别模组型号用的 ATI 命令"""
    return ModuleSpec(name='ATI', at_command='ATI', at_port=at_port, decoder='ati')


def build_at_command(spec: ModuleSpec) -> str:
    """拼 AT 读写命令：后台 cat 读 → 发送 → 结束 kill

    - `cat ... & CATPID=$!` + `kill $CATPID` 保证读到的响应能回吐给 SSH，
      且不会残留进程（`|| true` 是必须的：cat 读完自行退出时 kill 返回非零，
      会被上层误判为命令失败）
    - 移远用 printf 发 `\\r\\n`；龙尚沿用场测验证过的 `echo -e "cmd\\r"`
    """
    if spec.send_mode == 'echo':
        send = 'echo -e "%s\\r" > %s' % (spec.at_command, spec.at_port)
    else:
        send = "printf '%s\\r\\n' > %s" % (spec.at_command, spec.at_port)
    return ('cat %s & CATPID=$!; sleep 1; %s; sleep 1; kill $CATPID 2>/dev/null || true'
            % (spec.at_port, send))


# ---------------------------------------------------------------- 识别

def ati_text(stdout: str) -> str:
    """取 ATI 响应里可用于识别厂商的文字（去掉命令回显与结果码）"""
    lines = []
    for line in (stdout or '').splitlines():
        text = line.strip()
        if not text or text in ('ATI', 'OK', 'ERROR', 'ATE0'):
            continue
        lines.append(text)
    return '\n'.join(lines)


def is_usable_ati(stdout: str) -> bool:
    """ATI 是否返回了可读内容

    实测有模组的 AT 口在未握手时会回一串不可读字节，这种情况算「没识别出」，
    不能当成厂商信息去匹配关键字。
    """
    text = ati_text(stdout)
    if not text:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in '\r\n\t')
    return printable / len(text) >= 0.9


def vendor_line(text: str) -> str:
    """取 ATI 里的**厂商**行

    多行 ATI 里最靠谱的厂商行是最短的那一行：`Quectel` / `Longsung`（厂商名），
    而 `EC800K-CN` / `SM-208-A-RGB LiteOS HID`（型号）、`Revision: ...`（版本）
    都更长。取「型号行」会把版本/模块名当型号显示，太啰嗦。
    """
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
    if not lines:
        return ''
    return min(lines, key=len)


def identify_module(stdout: str, modules: List[ModuleSpec]) -> Tuple[Optional[ModuleSpec], str]:
    """按关键字识别模组厂商，返回 (spec, 厂商/型号文字)；识别不出返回 (None, '')"""
    text = ati_text(stdout)
    if not text or not is_usable_ati(stdout):
        return None, ''
    for spec in modules:
        if spec.matches(text):
            return spec, vendor_line(text)
    return None, ''


# ---------------------------------------------------------------- 解析

def decode(spec: ModuleSpec, stdout: str) -> Outcome:
    """按模组的响应格式解析信号字段"""
    if spec.decoder == 'cpsi':
        return decode_cpsi(stdout)
    return decode_qeng(stdout)


def decode_qeng(stdout: str) -> Outcome:
    """移远 AT+QENG="servingcell"：字段 13/14/15/16 = RSRP/RSRQ/RSSI/SINR

    这些值移远已经换算好了（单位 dBm/dB），**不能再套任何公式**。
    """
    line = next((ln.strip() for ln in (stdout or '').splitlines()
                 if '+QENG:' in ln and 'servingcell' in ln), None)
    if not line:
        return Outcome(reason='未能解析 AT+QENG 输出')

    head = re.search(r'\+QENG:\s*"servingcell"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"', line)
    state = head.group(1) if head else ''
    network_mode = head.group(2) if head else ''
    if network_mode and network_mode != 'LTE':
        return Outcome(reason=f'当前网络模式 {network_mode} 不支持信号质量判定')
    if state and state not in ('NOCONN', 'CONNECT', 'CAMPED'):
        return Outcome(reason=f'未注册到网络, state={state}')

    fields = [f.strip().strip('"') for f in line.split('+QENG:', 1)[1].split(',')]
    if len(fields) < 17:
        return Outcome(reason=f'LTE 响应字段数不足: {len(fields)}')
    try:
        codes = {'rsrp': int(fields[13]), 'rsrq': int(fields[14]),
                 'rssi': int(fields[15]), 'sinr': int(fields[16])}
    except (ValueError, IndexError) as e:
        return Outcome(reason=f'LTE 信号字段解析失败: {e}')

    unknown = [f'{METRIC_LABEL[k]}={codes[k]}' for k in METRIC_ORDER
               if codes[k] in UNKNOWN_CODES]
    if unknown:
        return Outcome(reason='模组返回未知值: ' + ', '.join(unknown))

    # 移远直出真实值：code == value，formula 留空
    metrics = [Metric(key=k, value=codes[k], code=codes[k]) for k in METRIC_ORDER]
    return Outcome(ok=True, metrics=metrics)


def decode_cpsi(stdout: str) -> Outcome:
    """龙尚 AT+CPSI?：码值换算成真实值

    +CPSI: LTE,Online,460-00,0x1145,19219514,179,EUTRAN-BAND3,1350,5,5,26,60,64,21
                                                                    ↑  ↑  ↑  ↑
                                                                  RSRQ RSRP RSSI SINR
    """
    line = next((ln.strip() for ln in (stdout or '').splitlines() if '+CPSI:' in ln), None)
    if not line:
        return Outcome(reason='未能解析 AT+CPSI 输出')

    fields = [f.strip().strip('"') for f in line.split('+CPSI:', 1)[1].split(',')]
    # 时间戳混进字段会整体错位，先剔除
    fields = [f for f in fields if not _TIMESTAMP_RE.match(f)]
    if not fields or not fields[0]:
        return Outcome(reason='AT+CPSI 响应字段为空')

    mode = fields[0].upper()
    if not mode.startswith('LTE'):
        return Outcome(reason=f'当前网络模式 {fields[0]} 不支持信号质量判定')
    if len(fields) < CPSI_MIN_FIELDS:
        return Outcome(reason=f'AT+CPSI 响应字段数不足: {len(fields)}')

    codes = {}
    for key, idx in CPSI_FIELDS.items():
        try:
            codes[key] = int(fields[idx])
        except (ValueError, IndexError) as e:
            return Outcome(reason=f'AT+CPSI {METRIC_LABEL[key]} 字段解析失败: {e}')

    unknown = [f'{METRIC_LABEL[k]}={codes[k]}' for k in METRIC_ORDER
               if codes[k] in UNKNOWN_CODES]
    if unknown:
        return Outcome(reason='模组返回未知值: ' + ', '.join(unknown))

    metrics = []
    for key in METRIC_ORDER:
        formula, convert = LONGSUNG_DECODE[key]
        metrics.append(Metric(key=key, value=convert(codes[key]),
                              code=codes[key], formula=formula))
    return Outcome(ok=True, metrics=metrics)


# ---------------------------------------------------------------- 判定与展示

def judge(metrics: List[Metric]) -> Tuple[bool, Dict[str, bool], str]:
    """按 LTE_THRESHOLDS 判定，返回 (是否通过, 各字段达标情况, 无法判定的原因)"""
    values = {m.key: m.value for m in metrics if m.value is not None}
    missing = [METRIC_LABEL[k] for k in JUDGE_KEYS if k not in values]
    if missing:
        return False, {}, '缺少判定字段: ' + ', '.join(missing)

    checks = {k: values[k] >= LTE_THRESHOLDS[k] for k in JUDGE_KEYS}
    return all(checks.values()), checks, ''


def vendor_text(spec: ModuleSpec, model: str = '') -> str:
    """厂商展示文字，如 `龙尚 SM-208-A-RGB`"""
    model = (model or '').strip()
    if model and model.upper() not in spec.name.upper():
        return f'{spec.name} {model}'
    return spec.name


def summary_tokens(metrics: List[Metric],
                   checks: Optional[Dict[str, bool]] = None) -> List[str]:
    """面板括号里的字段值，如 ['RSRP -80dBm', 'RSRQ -7dB', 'SINR 21dB', 'RSSI -46dBm']

    必须带字段名和单位：括号里只给一串裸数字时，现场无法判断哪一个是 RSRP、
    哪一个是 SINR，而这两个值的判定阈值完全不同。判定不达标的字段追加 ✗，
    让「哪一项拖累了判定」一眼可见。
    """
    tokens = []
    for metric in metrics:
        text = f'{metric.label} {metric.value_text}{metric.unit}'
        if checks and checks.get(metric.key) is False:
            text += '✗'
        tokens.append(text)
    return tokens


def format_metric_line(metric: Metric) -> str:
    """单个字段的详情行：值 + 单位 +（码值换算过程）"""
    text = f'{metric.label}: {metric.value_text} {metric.unit}'.rstrip()
    if metric.formula and metric.code is not None:
        return f'{text} (码值 {metric.code}, {metric.formula})'
    if metric.code is not None:
        return f'{text} (模组直出真实值)'
    return text


def format_detail(spec: ModuleSpec, model: str = '',
                  metrics: Optional[List[Metric]] = None,
                  checks: Optional[Dict[str, bool]] = None,
                  identify_note: str = '',
                  reason: str = '') -> str:
    """检测详情：首行是模组厂商，其后是识别方式、读取命令、各字段换算、判定结论"""
    lines = [f'模组厂商: {vendor_text(spec, model)}']
    if identify_note:
        lines.append('识别方式: ' + identify_note)
    lines.append('读取命令: ' + build_at_command(spec))
    lines.extend(format_metric_line(m) for m in (metrics or []))
    if reason:
        lines.append('判定: ' + reason)
    elif checks:
        marks = []
        for key in JUDGE_KEYS:
            label = METRIC_LABEL[key]
            ok = checks.get(key, False)
            marks.append(f'{label}>={LTE_THRESHOLDS[key]} ' + ('✓' if ok else '✗'))
        lines.append('判定: ' + '  '.join(marks))
    return '\n'.join(lines)
