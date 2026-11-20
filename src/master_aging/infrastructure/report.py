"""完全离线的 HTML、CSV/JSON；HTML 中的设备字段统一转义。"""
import csv
import hashlib
from html import escape
import json
import threading
from functools import wraps
from pathlib import Path
from ..domain.models import ITEMS

STYLE = '''body{font:15px system-ui,sans-serif;color:#263449;background:#f3f6fa;margin:0}
main{max-width:1200px;margin:auto;padding:32px}h1{font-size:26px}h2{font-size:20px;margin-top:32px}
.card{background:white;border:1px solid #dde4ed;border-radius:12px;padding:20px;margin:16px 0}
table{border-collapse:collapse;width:100%;background:white}td,th{padding:10px;border:1px solid #dbe2ea;text-align:left;word-break:break-word}
th{background:#eef3f9}.PASS{color:#147d51}.FAIL,.BLOCKED{color:#be2438}.INCOMPLETE,.ABORTED{color:#a46800}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}svg{width:100%;height:180px}small{color:#607087}
a{color:#1a62bf}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(350px,1fr));gap:16px}'''

_REPORT_LOCK = threading.RLock()


def serialized(function):
    @wraps(function)
    def execute(*args, **kwargs):
        with _REPORT_LOCK:
            return function(*args, **kwargs)
    return execute


def text(value):
    return escape(str(value if value is not None else '缺失'))


def document(title, body):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + text(title) + '</title><style>' + STYLE + '</style><main>' + body + '</main></html>'


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


@serialized
def export_report(repository, run_id):
    row = repository.get(run_id)
    if not row:
        raise ValueError('运行不存在')
    directory = Path(row['output_dir'])
    directory.mkdir(parents=True, exist_ok=True)
    status, snapshot = json.loads(row['status']), json.loads(row['snapshot'])
    records = repository.records(run_id)
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
    body += '<br>创建：' + text(row['created_at']) + '<br>结束：' + text(status.get('finished_at')) + '<br>状态：' + text(row['state']) + '<br>墙钟运行秒数：' + text(status.get('wall_s')) + '<br>数据上报：仅本地保存</div>'
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
    body += '<h2>事件时间线</h2><table><tr><th>时间</th><th>事件</th></tr>'
    for record in records:
        if record['kind'] != 'metric':
            body += '<tr><td>' + text(record['utc']) + '</td><td><pre>' + text(json.dumps(record['data'], ensure_ascii=False)) + '</pre></td></tr>'
    body += '</table>'
    for title, value in (('硬件与固件', status.get('hardware')), ('环境记录', snapshot.get('environment')),
                         ('电池阶段与积分', status.get('battery')), ('存储写入账本', status.get('write_ledger')),
                         ('运行配置快照', snapshot), ('清理结果', {'confirmed': status.get('cleanup_confirmed'), 'errors': status.get('cleanup_errors')})):
        body += '<details class="card"><summary>' + text(title) + '</summary><pre>' + text(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre></details>'
    body += '<p><a href="metrics.csv">原始指标 CSV</a> · <a href="records.json">完整事件与采样 JSON</a> · <a href="summary.json">结论 JSON</a></p>'
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
