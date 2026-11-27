"""设备中断的结论只使用已有证据，不将离线时间计入有效时长。"""
from copy import deepcopy


def finish_interrupted(status, evidence, planned_items=None):
    result = deepcopy(status)
    items = result.setdefault('items', {})
    for key, config in (planned_items or {}).items():
        if config.get('enabled') and key not in items:
            items[key] = {'effective_s': 0, 'planned_s': config.get('duration_min', 0) * 60}
    reason = evidence['reason']
    for item in items.values():
        if item.get('result') is None:
            item.update(result='INCOMPLETE', stop_reason=reason,
                        interruption_detected_at=evidence['detected_at'])
    result.update(state='FINISHED', result='FAIL' if status.get('result') == 'FAIL'
                  or any(item.get('result') == 'FAIL' for item in items.values()) else 'INCOMPLETE',
                  interruption=deepcopy(evidence), error=reason, cleanup_confirmed=False,
                  finished_at=evidence['detected_at'], finish_time_source='interruption_detection')
    result.pop('connection_error', None)
    result.pop('stop_pending', None)
    return result
