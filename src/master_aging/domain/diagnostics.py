"""监测项目共用的诊断文字；根据采样证据描述异常，不推断硬件根因。"""

LTE_DEFAULT_THRESHOLDS = {'rsrp': -90, 'rsrq': -13, 'sinr': 5}


def lte_thresholds(config=None):
    return dict(LTE_DEFAULT_THRESHOLDS, **((config or {}).get('thresholds') or {}))


def lte_scope(config=None):
    limits = lte_thresholds(config)
    return '周期检测 4G 信号：RSRP ≥ %g dBm、RSRQ ≥ %g dB、SINR ≥ %g dB' % (
        limits['rsrp'], limits['rsrq'], limits['sinr'])


LTE_SCOPE = lte_scope()
GENERIC_MONITOR_REASON = '连续监测发现异常，详见原始样本；归因待复核'


def monitor_reason(key, sample):
    if sample.get('error'):
        return sample['error']
    if key == 'lte':
        thresholds = sample.get('thresholds', {'rsrp': -90, 'rsrq': -13, 'sinr': 5})
        failed = []
        for metric, limit in thresholds.items():
            value = sample.get('signal', {}).get(metric)
            if value is not None and value < limit:
                unit = 'dBm' if metric == 'rsrp' else 'dB'
                failed.append('%s=%g %s，要求 ≥ %g %s' % (metric.upper(), value, unit, limit, unit))
        if failed:
            return '4G 信号不达标: ' + '；'.join(failed)
        return sample.get('reason') or '4G 未获取完整信号数据，无法判定 RSRP/RSRQ/SINR'
    return sample.get('reason') or '未取得可判定的采样数据'


def item_reason(key, item, records=(), config=None):
    """旧报告可用已有失败样本补充诊断；不改变历史结论和原始记录。"""
    reason = item.get('stop_reason') or ''
    if reason != GENERIC_MONITOR_REASON or key != 'lte':
        return reason
    sample = item.get('failure_sample') or item.get('last_sample')
    if not sample:
        sample = next((record['data'][key] for record in sorted(records, key=lambda r: r['seq'])
                       if record['kind'] == 'metric' and record['data'].get(key, {}).get('ok') is False), None)
    if not sample:
        return '监测异常；未保存失败样本，无法确认具体原因'
    return monitor_reason(key, sample)


def item_scope(key, item):
    if key == 'lte' and (item.get('scope') or '').startswith('沿用产测'):
        return LTE_SCOPE
    return item.get('scope') or (LTE_SCOPE if key == 'lte' else '')
