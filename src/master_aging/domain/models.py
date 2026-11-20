import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

ITEMS = {'cpu': 'CPU', 'ddr': 'DDR', 'storage': 'SSD/eMMC', 'lte': '4G',
         'battery': '电池', 'usb': 'USB', 'hdmi': 'HDMI'}
FINAL = {'PASS', 'FAIL', 'ABORTED', 'INCOMPLETE', 'NOT_APPLICABLE', 'BLOCKED'}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return uuid4().hex


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def safe_sn(sn):
    return re.sub(r'[^a-zA-Z0-9_.-]', '_', sn)[:80].strip('.') or 'unknown'


def conclusion(items):
    values = [i.get('result') for i in items.values() if i.get('result') != 'NOT_APPLICABLE']
    for result in ('FAIL', 'ABORTED', 'BLOCKED'):
        if result in values:
            return result
    return 'PASS' if values and all(v == 'PASS' for v in values) else 'INCOMPLETE'
