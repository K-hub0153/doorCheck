"""完全离线的 HTML、CSV/JSON；HTML 中的设备字段统一转义。"""
import base64
import csv
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import sys
import threading
from functools import lru_cache, wraps
from pathlib import Path
from ..domain.models import ITEMS
from ..domain.execution_log import format_record

STYLE = '''body{font:15px system-ui,sans-serif;color:#263449;background:#f3f6fa;margin:0}
main{max-width:1200px;margin:auto;padding:32px}h1{font-size:26px}h2{font-size:20px;margin-top:32px}
.card{background:white;border:1px solid #dde4ed;border-radius:12px;padding:20px;margin:16px 0}
table{border-collapse:collapse;width:100%;background:white}td,th{padding:10px;border:1px solid #dbe2ea;text-align:left;word-break:break-word}
th{background:#eef3f9}.PASS{color:#147d51}.FAIL,.BLOCKED{color:#be2438}.INCOMPLETE,.ABORTED{color:#a46800}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}svg{width:100%;height:180px}small{color:#607087}
a{color:#1a62bf}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(350px,1fr));gap:16px}
.table-scroll{overflow-x:auto}.timeline td{vertical-align:top}.timeline .time{white-space:nowrap;font-variant-numeric:tabular-nums}
.timeline .event{min-width:130px;font-weight:600}.timeline .description{min-width:260px}
.timeline details{margin-top:8px}.timeline summary{cursor:pointer;color:#607087;font-size:12px}
.badge{display:inline-block;border-radius:4px;padding:2px 7px;background:#eef3f9;font-size:12px;margin-left:6px}'''

RESULT_LABELS = {'PASS': '通过', 'FAIL': '失败', 'INCOMPLETE': '未完成', 'ABORTED': '已停止',
                 'BLOCKED': '预检未通过', 'NOT_APPLICABLE': '不适用'}
STATE_LABELS = {'RUNNING': '开始老化', 'PREFLIGHT': '开始预检', 'WAIT_OPERATOR': '等待电源确认',
                'WAIT_CURRENT': '等待充放电电流', 'FINISHED': '老化结束', 'STOPPING': '正在停止',
                'SAFETY_STOP': '保护停止', 'RECONNECTING': '等待设备重连', 'BLOCKED': '预检未通过'}

_REPORT_LOCK = threading.RLock()


def serialized(function):
    @wraps(function)
    def execute(*args, **kwargs):
        with _REPORT_LOCK:
            return function(*args, **kwargs)
    return execute


def text(value):
    return escape(str(value if value is not None else '缺失'))


def local_time(value):
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local = moment.astimezone()
        offset = local.strftime('%z')
        return local.strftime('%Y-%m-%d %H:%M:%S') + ' ' + offset[:3] + ':' + offset[3:]
    except (AttributeError, TypeError, ValueError):
        return value


def event_summary(record):
    data, kind = record['data'], record['kind']
    phase = {'charge': '充电', 'discharge': '放电'}.get(data.get('phase'), '')
    item = ITEMS.get(data.get('item'), data.get('item') or '')
    if kind == 'operator':
        return '电池', phase + '电源已确认', data.get('reason') or '开始检测实际电流，连续达标后计时'
    if kind == 'write_reservation':
        return 'SSD/eMMC', '预留写入预算', '%s · 第 %s 轮 · %s 字节' % (data.get('target', ''), data.get('round', ''), data.get('bytes', ''))
    if kind == 'storage':
        return 'SSD/eMMC', '写入与读取校验完成', '%s · 第 %s 轮 · 写入 %s 字节，校验 %s 字节' % (
            data.get('target', ''), data.get('round', ''), data.get('written_bytes', ''), data.get('verified_bytes', ''))
    if data.get('error'):
        return item or '整机', '执行异常', data['error']
    if data.get('stage') == 'preflight':
        return item, '预检通过' if data.get('result') == 'PASS' else '预检未通过', data.get('reason', '')
    result = data.get('result')
    if result:
        title = '老化结束' if data.get('state') == 'FINISHED' else '项目结束'
        description = data.get('reason') or ('进程清理异常：' + '；'.join(data['cleanup_errors']) if data.get('cleanup_errors') else '本次运行已结束')
        return item or '整机', title, description
    state = data.get('state')
    if phase:
        if state == 'RUNNING':
            return item or '电池', phase + '开始计时', data.get('reason') or '实际电流已连续达标'
        if state == 'WAIT_CURRENT':
            return item or '电池', '等待' + phase + '电流', data.get('reason') or '无需再次人工确认，实际电流连续达标后自动计时'
        if state == 'WAIT_OPERATOR' or (not state and data.get('token') is not None):
            operation = '请接入适配器' if data['phase'] == 'charge' else '请拔除适配器并接好放电负载'
            return item or '电池', '等待' + phase + '电源确认', data.get('reason') or operation
    return item or '整机', STATE_LABELS.get(state, state or '运行事件'), data.get('reason') or ('所选项目已启动' if state == 'RUNNING' else '')


def event_timeline(records):
    # 沿用设备序号保证因果顺序，设备时钟校准不应打乱先后关系。
    ordered = sorted(records, key=lambda r: r['seq'])
    origins = {}
    for record in ordered:
        origins.setdefault(record.get('boot_id'), record.get('monotonic'))
    events = [record for record in ordered if record['kind'] != 'metric']
    body = '<h2>事件时间线</h2><p><small>时间按导出电脑的本地时区显示，并标注 UTC 偏移；累计用时从同次设备启动的首条记录起算，不等于项目有效时长。事件按设备序号排列。</small></p>'
    if not events:
        return body + '<div class="card">暂无事件记录</div>'
    body += '<div class="table-scroll"><table class="timeline"><thead><tr><th>时间 / 累计用时</th><th>项目</th><th>事件 / 结果</th><th>说明</th></tr></thead><tbody>'
    for record in events:
        item, title, description = event_summary(record)
        start, current = origins[record.get('boot_id')], record.get('monotonic')
        elapsed = '用时缺失'
        if isinstance(start, (int, float)) and isinstance(current, (int, float)):
            seconds = max(0, int(current - start))
            elapsed = '+%02d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60)
        result = record['data'].get('result')
        badge = '<span class="badge ' + text(result) + '">' + text(RESULT_LABELS.get(result, result)) + '</span>' if result else ''
        body += ('<tr><td class="time">' + text(local_time(record.get('utc'))) + '<br><small>' + elapsed
                 + ' · #' + text(record['seq']) + '</small></td><td>' + text(item) + '</td><td class="event">' + text(title) + badge
                 + '</td><td class="description">' + text(description) + '<details><summary>原始事件</summary><pre>'
                 + text(json.dumps(record['data'], ensure_ascii=False, indent=2)) + '</pre></details></td></tr>')
    return body + '</tbody></table></div>'


@lru_cache(maxsize=1)
def favicon_url():
    if getattr(sys, 'frozen', False):
        path = Path(sys._MEIPASS) / 'vdian.ico'
    else:
        path = Path(__file__).resolve().parents[2] / 'ui' / 'icon' / 'vdian.ico'
    return 'data:image/x-icon;base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def document(title, body):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width">'
            '<link rel="icon" type="image/x-icon" href="' + favicon_url() + '">'
            '<title>' + text(title) + '</title><style>' + STYLE
            + '</style></head><body><main>' + body + '</main></body></html>')


def chart(label, points):
    valid = [(x, y) for x, y in points if isinstance(y, (int, float))]
    if len(valid) < 2:
        return '<div class="card">' + text(label) + '：有效数据不足</div>'
    # 每桶保留极大/极小，保证温度或电流尖峰不会被平均隐藏。
    if len(valid) > 600:
        width = max(1, len(valid) // 300)
        reduced = []
        for start in range(0, len(valid), width):
            bucket = valid[start:start + width]
            reduced.extend(sorted({min(bucket, key=lambda p: p[1]), max(bucket, key=lambda p: p[1])}))
        valid = reduced
    xmin, xmax = valid[0][0], valid[-1][0]
    ymin, ymax = min(y for _, y in valid), max(y for _, y in valid)
    # 分段绘制：时间缺口不连线，原始数据始终在 CSV/JSON 中保留。
    deltas = sorted(b[0] - a[0] for a, b in zip(valid, valid[1:]) if b[0] > a[0])
    gap_limit = deltas[len(deltas) // 2] * 3 if deltas else 0
    segments, current, previous = [], [], None
    for x, y in valid:
        if previous is not None and x - previous > gap_limit:
            segments.append(current)
            current = []
        current.append('%.2f,%.2f' % (20 + (x - xmin) / max(1, xmax - xmin) * 940,
                                    150 - (y - ymin) / max(1, ymax - ymin) * 130))
        previous = x
    segments.append(current)
    polylines = ''.join('<polyline fill="none" stroke="#286aca" stroke-width="2" points="' + ' '.join(segment) + '"/>' for segment in segments)
    return ('<div class="card"><b>' + text(label) + '</b><svg viewBox="0 0 980 180" role="img" aria-label="' + text(label) + '">'
            + polylines + '</svg><small>最小 %.3f · 最大 %.3f · 时间跨度 %.1f 分钟</small></div>' % (ymin, ymax, (xmax - xmin) / 60))


def write_execution_log(row, records):
    directory = Path(row['output_dir'])
    directory.mkdir(parents=True, exist_ok=True)
    status = json.loads(row['status'])
    with (directory / 'execution.log').open('w', encoding='utf-8') as stream:
        stream.write('主控老化执行日志\nSN：%s  IP：%s\n运行：%s\n状态：%s  结果：%s\n' %
                     (row['sn'], row['ip'], row['run_id'], row['state'], row['result'] or '未结束'))
        for key in ('error', 'connection_error', 'safety_stop', 'attachment_error'):
            if status.get(key):
                stream.write('%s：%s\n' % (key, status[key]))
        for key, item in status.get('items', {}).items():
            stream.write('%s：%s；%s\n' % (ITEMS.get(key, key), item.get('result') or '进行中',
                                          item.get('stop_reason') or ''))
            if item.get('argv'):
                stream.write('执行参数：' + json.dumps(item['argv'], ensure_ascii=False) + '\n')
        stream.write('以下为设备记录，时间为 UTC，按事件序号排列。连接与部署过程见 controller.log。\n\n')
        for record in sorted(records, key=lambda r: r['seq']):
            stream.write(format_record(record))
    return directory / 'execution.log'


@serialized
def export_execution_log(repository, run_id):
    row = repository.get(run_id)
    if not row:
        raise ValueError('运行不存在')
    return write_execution_log(row, repository.records(run_id))


@serialized
def export_report(repository, run_id):
    row = repository.get(run_id)
    if not row:
        raise ValueError('运行不存在')
    directory = Path(row['output_dir'])
    directory.mkdir(parents=True, exist_ok=True)
    status, snapshot = json.loads(row['status']), json.loads(row['snapshot'])
    records = repository.records(run_id)
    write_execution_log(row, records)
    (directory / 'records.json').write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    with (directory / 'metrics.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['seq', 'utc', 'received_at', 'boot_id', 'monotonic_s', 'metric', 'value'])
        def flatten(value, prefix=''):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield from flatten(child, prefix + '.' + key if prefix else key)
            else:
                yield prefix, json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
        for record in records:
            if record['kind'] == 'metric':
                for key, value in flatten(record['data']):
                    # 防止设备字符串在电子表格内被当作公式执行。
                    if isinstance(value, str) and value.startswith(('=', '+', '-', '@')):
                        value = "'" + value
                    writer.writerow([record['seq'], record['utc'], record.get('received_at'), record['boot_id'], record['monotonic'], key, value])
    items = status.get('items', {})
    result = row['result'] or 'INCOMPLETE'
    body = '<h1>主控设备老化报告</h1><p>SN：' + text(row['sn']) + ' · <b class="' + text(result) + '">' + text(result) + '</b></p>'
    body += '<div class="card">批次：' + text(row['batch_id']) + '<br>运行：' + text(run_id)
    body += '<br>创建：' + text(local_time(row['created_at'])) + '<br>结束：' + text(local_time(status.get('finished_at'))) + '<br>状态：' + text(row['state']) + '<br>墙钟运行秒数：' + text(status.get('wall_s')) + '<br>数据上报：仅本地保存</div>'
    for key in ('error', 'connection_error', 'safety_stop', 'attachment_error', 'report_error'):
        if status.get(key):
            body += '<p><b>' + text(key) + '：</b>' + text(status[key]) + '</p>'
    body += '<h2>项目判定</h2><table><tr><th>项目</th><th>计划秒数</th><th>有效秒数</th><th>结果</th><th>停止原因 / 范围</th></tr>'
    for key, item in items.items():
        item_result = item.get('result') or 'INCOMPLETE'
        body += '<tr><td>' + text(ITEMS.get(key, key)) + '</td><td>' + text(item.get('planned_s')) + '</td><td>' + text(round(item.get('effective_s', 0), 2)) + '</td><td class="' + text(item_result) + '">' + text(item_result) + '</td><td>' + text(item.get('stop_reason')) + '<br>' + text(item.get('scope', '')) + '</td></tr>'
    body += '</table><p><small>CPU/DDR/存储等为合成负载。HDMI connected 仅代表连接检测，显示确认由操作者记录。日志或时长不足不能判通过。</small></p>'
    metrics = [r for r in records if r['kind'] == 'metric']
    curves = {k: [] for k in ('CPU 忙碌率 %', '最高 CPU 温度 ℃', '电池 SOC %', '电池净电流 mA', '电池电压 mV', '电池温度 ℃')}
    for record in metrics:
        t, data = record['monotonic'], record['data']
        b = data.get('battery', {})
        temps = list(data.get('temperature_c', {}).values())
        values = [data.get('cpu', {}).get('busy_pct'), max(temps) if temps else None,
                  b.get('soc_pct'), b.get('current_ma'), b.get('voltage_mv'), b.get('temperature_c')]
        for key, value in zip(curves, values):
            curves[key].append((t, value))
    body += '<h2>采样曲线</h2><div class="grid">' + ''.join(chart(key, points) for key, points in curves.items()) + '</div>'
    body += event_timeline(records)
    for title, value in (('硬件与固件', status.get('hardware')), ('环境记录', snapshot.get('environment')),
                         ('电池阶段与积分', status.get('battery')), ('存储写入账本', status.get('write_ledger')),
                         ('运行配置快照', snapshot), ('清理结果', {'confirmed': status.get('cleanup_confirmed'), 'errors': status.get('cleanup_errors')})):
        body += '<details class="card"><summary>' + text(title) + '</summary><pre>' + text(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre></details>'
    body += '<p><a href="metrics.csv">原始指标 CSV</a> · <a href="records.json">完整事件与采样 JSON</a> · <a href="summary.json">结论 JSON</a></p>'
    body += '<h2>执行日志</h2><p><a href="execution.log">查看执行日志（含 4G 原始响应）</a>'
    if (directory / 'controller.log').is_file():
        body += ' · <a href="controller.log">连接与部署日志</a>'
    body += '</p><ul>'
    for log in sorted((directory / 'device').glob('*.log')):
        body += '<li><a href="device/' + text(log.name) + '">' + text(log.name) + '</a></li>'
    body += '</ul>'
    path = directory / 'report.html'
    path.write_text(document('老化报告 ' + row['sn'], body), encoding='utf-8')
    checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file() and p.name != 'checksums.json'}
    (directory / 'checksums.json').write_text(json.dumps(checksums, indent=2), encoding='utf-8')
    return path


@serialized
def export_batch(repository, batch_id):
    rows = [r for r in repository.runs() if r['batch_id'] == batch_id]
    if not rows:
        return
    directory = Path(rows[0]['output_dir']).parents[1]
    body = '<h1>主控老化批次报告</h1><p>' + text(batch_id) + '</p><table><tr><th>SN</th><th>IP</th><th>状态</th><th>结论</th><th>报告</th></tr>'
    totals = {}
    for row in rows:
        result = row['result'] or 'INCOMPLETE'
        totals[result] = totals.get(result, 0) + 1
        path = Path(row['output_dir']) / 'report.html'
        relative = path.relative_to(directory).as_posix()
        body += '<tr><td>' + text(row['sn']) + '</td><td>' + text(row['ip']) + '</td><td>' + text(row['state']) + '</td><td class="' + text(result) + '">' + text(result) + '</td><td><a href="' + text(relative) + '">逐设备报告</a></td></tr>'
    body += '</table><p>' + text(json.dumps(totals, ensure_ascii=False)) + '</p>'
    path = directory / 'batch_report.html'
    path.write_text(document('批次报告', body), encoding='utf-8')
    return path
