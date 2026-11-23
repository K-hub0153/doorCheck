"""监测项目共用的诊断文字；根据采样证据描述异常，不推断硬件根因。"""

LTE_SCOPE = '周期检测 4G 信号：RSRP ≥ -90 dBm、RSRQ ≥ -13 dB、SINR ≥ 5 dB'
GENERIC_MONITOR_REASON = '连续监测发现异常，详见原始样本；归因待复核'


def monitor_reason(key, sample, config=None, baseline=None):
    config = config or {}
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
    if key == 'usb':
        devices = sample.get('devices', [])
        if baseline is not None and baseline != devices:
            changes = []
            for device in baseline:
                current = next((d for d in devices if d.get('path') == device.get('path')), None)
                if current is None:
                    changes.append('%s 已断开或速率低于要求' % device.get('path'))
                elif current != device:
                    fields = '，'.join('%s %s → %s' % (field, device.get(field), current.get(field))
                                       for field in sorted(set(device) | set(current))
                                       if device.get(field) != current.get(field))
                    changes.append('%s 设备信息改变（%s）' % (device.get('path'), fields))
            for device in devices:
                if not any(d.get('path') == device.get('path') for d in baseline):
                    changes.append('新增设备 %s（devnum %s）' % (device.get('path'), device.get('devnum')))
            detail = '；'.join(changes) or 'USB 设备信息与基线不一致'
            if devices:
                observed = sample.get('observed_devices', [])
                speeds = '、'.join('%s=%s Mbps' % (d.get('path'), d.get('speed_mbps')) for d in observed)
                return 'USB 夹具发生变化: ' + detail + ('；当前速率 ' + speeds if speeds else '')
        else:
            detail = ''
        observed = sample.get('observed_devices', devices)
        speeds = '、'.join('%s=%s Mbps' % (d.get('path'), d.get('speed_mbps')) for d in observed)
        limit = config.get('min_speed_mbps')
        requirement = '要求 ≥ %s Mbps' % limit if limit is not None else '未发现达到配置最低速率的设备'
        return 'USB 检测失败: ' + (detail + '；' if detail else '') + (
            '当前设备 ' + speeds if speeds else '未发现可读取速率的外接 USB 设备') + '；' + requirement
    if key == 'hdmi':
        connectors = sample.get('connectors', {})
        if not connectors:
            return 'HDMI 检测失败: 未发现 HDMI 接口状态节点'
        return 'HDMI 未连接: ' + '；'.join('%s=%s' % (path, value.get('status') or '状态不可读')
                                              for path, value in connectors.items())
    return sample.get('reason') or '未取得可判定的采样数据'


def item_reason(key, item, records=(), config=None):
    """旧报告可用已有失败样本补充诊断；不改变历史结论和原始记录。"""
    reason = item.get('stop_reason') or ''
    if reason != GENERIC_MONITOR_REASON or key not in ('lte', 'usb', 'hdmi'):
        return reason
    sample = item.get('failure_sample') or item.get('last_sample')
    if not sample:
        sample = next((record['data'][key] for record in sorted(records, key=lambda r: r['seq'])
                       if record['kind'] == 'metric' and record['data'].get(key, {}).get('ok') is False), None)
    if not sample:
        return '监测异常；未保存失败样本，无法确认具体原因'
    return monitor_reason(key, sample, config, item.get('usb_baseline') if key == 'usb' else None)


def item_scope(key, item):
    return LTE_SCOPE if key == 'lte' else item.get('scope', '')
