"""云端只保存报告所需指标，原始采样与工具输出保留在本地日志。"""
from copy import deepcopy

CLOUD_INTERVAL_S = 300


def compact(value):
    if isinstance(value, list):
        return [compact(child) for child in value]
    if not isinstance(value, dict):
        return value
    return {key: compact(child) for key, child in value.items()
            if key not in {'raw', 'vmstat', 'diskstats', 'kernel_new', 'processes',
                           'vm_regions', 'firmware', 'environment_deployment'}}


def cloud_record(record):
    result = deepcopy(record)
    data = result['data']
    if record['kind'] == 'metric':
        data = {key: data[key] for key in ('sample_monotonic', 'sample_utc', 'cpu',
                'temperature_c', 'battery', 'lte', 'ddr_process_memory', 'kernel_errors', 'kernel_log_gap')
                if key in data}
        memory = result['data'].get('memory', {})
        data['memory'] = {key: memory[key] for key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree')
                          if key in memory}
    result['data'] = compact(data)
    return result


def important_metric(record):
    data = record['data']
    return bool(data.get('kernel_errors') or data.get('kernel_log_gap')
                or data.get('lte', {}).get('ok') is False
                or data.get('battery', {}).get('valid') is False
                or data.get('ddr_process_memory', {}).get('vm_swap_bytes', 0) > 0)


def cloud_status(status):
    result = compact(status)
    if status.get('latest'):
        result['latest'] = cloud_record({'kind': 'metric', 'data': status['latest']})['data']
    result['logging'] = {'cloud_metric_interval_s': CLOUD_INTERVAL_S,
                         'details': 'local', 'cloud_records': 'summary'}
    return result


def state_signature(status):
    """状态/异常变化即时上传；有效秒数、采样序号变化不产生重复控制日志。"""
    return (tuple((key, status.get(key)) for key in ('state', 'result', 'error',
            'connection_error', 'environment_message', 'safety_stop', 'attachment_error',
            'launch_requested', 'cleanup_confirmed', 'stop_pending')),
            tuple((key, item.get('result'), item.get('stop_reason'))
                  for key, item in sorted(status.get('items', {}).items())),
            tuple((key, status.get('battery', {}).get(key))
                  for key in ('state', 'phase', 'index', 'confirmed')))
