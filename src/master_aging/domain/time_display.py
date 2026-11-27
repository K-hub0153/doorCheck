"""展示时固定使用北京时间；持久化记录和原始证据继续保留 UTC。"""
from datetime import datetime, timedelta, timezone

CHINA_TIME = timezone(timedelta(hours=8))


def china_time(value):
    if value in (None, ''):
        return value
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(CHINA_TIME).strftime('%Y-%m-%d %H:%M:%S')
    except (AttributeError, TypeError, ValueError):
        return value
