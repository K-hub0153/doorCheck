"""设备端与电脑端共用的可读执行记录；原始证据仍保留在 JSONL 中。"""
import json

from .models import ITEMS
from .diagnostics import monitor_reason


def lte_lines(result):
    lines = ['4G %s：%s' % ('通过' if result.get('ok') else '未通过',
                          '信号符合设定阈值' if result.get('ok') else monitor_reason('lte', result))]
    if result.get('module'):
        lines.append('模组：%s；AT 口：%s' % (result['module'], result.get('at_port', '')))
    if result.get('signal'):
        lines.append('信号：' + json.dumps(result['signal'], ensure_ascii=False))
    raw = result.get('raw', {})
    if raw.get('ati_port'):
        lines.append('ATI 识别端口：' + raw['ati_port'])
    if raw.get('ati'):
        lines.append('ATI 响应：\n' + raw['ati'])
    if raw.get('ati_error'):
        lines.append('ATI 读取异常：' + raw['ati_error'])
    for attempt in raw.get('attempts', []):
        lines.append('%s %s %s' % (attempt.get('module', ''), attempt.get('at_port', ''),
                                   attempt.get('command', '')))
        for key in ('signal', 'error', 'reason'):
            if attempt.get(key):
                lines.append(str(attempt[key]))
    return lines


def format_record(record):
    data = record['data']
    prefix = '%s #%s' % (record.get('utc', ''), record['seq'])
    lines = []
    if record['kind'] == 'metric':
        memory = data.get('memory', {})
        lines.append('%s 采样 CPU=%s%% 可用内存=%.1f MiB swap=%.1f MiB' % (
            prefix, data.get('cpu', {}).get('busy_pct'), memory.get('MemAvailable', 0) / 2**20,
            max(0, memory.get('SwapTotal', 0) - memory.get('SwapFree', 0)) / 2**20))
        for key, label in (('battery', '电池'), ('ddr_process_memory', 'DDR 进程内存'),
                           ('kernel_errors', '内核异常')):
            if data.get(key):
                lines.append(label + '：' + json.dumps(data[key], ensure_ascii=False))
    else:
        item = ITEMS.get(data.get('item'), data.get('item') or '整机')
        details = {key: value for key, value in data.items() if key not in ('hardware', 'lte')}
        lines.append('%s %s %s %s' % (prefix, item, record['kind'], json.dumps(details, ensure_ascii=False)))
        initial = data.get('hardware', {}).get('lte_initial')
        if initial:
            lines.extend(lte_lines(initial))
    if data.get('lte'):
        lines.extend(lte_lines(data['lte']))
    return '\n'.join(lines) + '\n'
