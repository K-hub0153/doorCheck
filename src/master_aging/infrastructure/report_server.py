"""浏览器查看入口：仅绑定回环地址，使用随机会话路径，不生成报告文件。"""
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

from .report import render_report, render_batch, execution_log, render_metrics
from .repository import StorageUnavailable


class ReportServer:
    def __init__(self, repository):
        self.repository = repository
        self.token = secrets.token_urlsafe(24)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond(False)

            def do_HEAD(self):
                self.respond(True)

            def respond(self, head):
                parts = urlsplit(self.path).path.strip('/').split('/')
                if len(parts) < 4 or not secrets.compare_digest(parts[0], owner.token):
                    self.send_error(404)
                    return
                try:
                    content, content_type, download = owner.content(parts[1:])
                    if isinstance(content, str):
                        content = content.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(content)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; frame-ancestors 'none'")
                    self.send_header('Referrer-Policy', 'no-referrer')
                    if download:
                        self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(download, safe=''))
                    self.end_headers()
                    if not head:
                        self.wfile.write(content)
                except ValueError:
                    self.send_error(404, 'Report or attachment not found')
                except StorageUnavailable:
                    self.send_error(503, 'MySQL unavailable; retry after reconnection')
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    self.send_error(500, 'Unable to render report')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='aging-reports', daemon=True)
        self.thread.start()

    def url(self, kind, identity, resource='report.html'):
        return 'http://127.0.0.1:%s/%s/%s/%s/%s' % (
            self.server.server_port, self.token, kind, quote(identity, safe=''), resource)

    def content(self, parts):
        kind, identity, *resource = parts
        identity = unquote(identity)
        name = '/'.join(resource)
        if kind == 'batch' and name == 'report.html':
            return render_batch(self.repository, identity), 'text/html; charset=utf-8', None
        if kind != 'run':
            raise ValueError('未知报告')
        row = self.repository.get(identity)
        if not row:
            raise ValueError('运行不存在')
        if name == 'report.html':
            return render_report(self.repository, identity), 'text/html; charset=utf-8', None
        if name == 'execution.log':
            return execution_log(self.repository, identity), 'text/plain; charset=utf-8', None
        if name == 'controller.log':
            logs = self.repository.controller_logs(identity)
            return ''.join('%s %s\n' % (r['created_at'], r['message']) for r in logs), 'text/plain; charset=utf-8', None
        if name == 'summary.json':
            return row['status'], 'application/json; charset=utf-8', 'summary.json'
        if name == 'config_snapshot.json':
            return row['snapshot'], 'application/json; charset=utf-8', 'config_snapshot.json'
        if name == 'manifest.json':
            return row['manifest'] or '{}', 'application/json; charset=utf-8', 'manifest.json'
        if name == 'records.json':
            return json.dumps(self.repository.records(identity), ensure_ascii=False), 'application/json; charset=utf-8', 'records.json'
        if name == 'metrics.csv':
            return render_metrics(self.repository.records(identity)), 'text/csv; charset=utf-8', 'metrics.csv'
        if len(resource) == 2 and resource[0] == 'device':
            filename = unquote(resource[1])
            # 原始附件作为下载，禁止将设备提供的 HTML/脚本当报告执行。
            return self.repository.artifact(identity, filename), 'application/octet-stream', filename
        raise ValueError('未知报告资源')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
