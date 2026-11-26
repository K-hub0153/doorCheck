"""PC 详细诊断日志；结果与报告仍由 MySQL 提供。"""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import tempfile
import threading

from .config import app_dir
from .log_summary import state_signature
from ..domain.execution_log import format_record
from ..domain.models import utc_now


def log_root():
    return app_dir() / 'logs' / 'master_aging'


def application_logger():
    logger = logging.getLogger('master_aging')
    if not logger.handlers:
        log_root().mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(log_root() / 'application.log'),
                                      maxBytes=10 * 2**20, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def atomic_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class LocalLogs:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else log_root()
        self._lock = threading.RLock()
        self._cursors = {}
        self._signatures = {}

    def directory(self, run_id, create=True):
        if not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise ValueError('非法日志运行编号')
        directory = self.root / run_id
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def cursor(self, run_id):
        with self._lock:
            if run_id not in self._cursors:
                directory = self.directory(run_id)
                index = directory / 'cursor.json'
                cursor = json.loads(index.read_text(encoding='utf-8')) if index.exists() else {
                    'offset': 0, 'seq': 0, 'records_bytes': 0, 'execution_bytes': 0}
                # 崩溃时仅回滚未提交的日志尾部，再从设备补采。
                for name, size in (('records.jsonl', cursor['records_bytes']),
                                   ('execution.log', cursor['execution_bytes'])):
                    path = directory / name
                    if size and (not path.exists() or path.stat().st_size < size):
                        raise OSError('本地日志不完整：' + str(path))
                    if path.exists():
                        with path.open('r+b') as stream:
                            stream.truncate(size)
                self._cursors[run_id] = cursor
            return dict(self._cursors[run_id])

    def append(self, run_id, records, offset):
        with self._lock:
            cursor = self.cursor(run_id)
            new = []
            for record in records:
                if record.get('run_id') != run_id:
                    raise ValueError('本地日志运行身份不匹配')
                if record['seq'] <= cursor['seq']:
                    continue
                if record['seq'] != cursor['seq'] + 1:
                    raise ValueError('设备详细日志序号缺段')
                cursor['seq'] = record['seq']
                new.append(record)
            directory = self.directory(run_id)
            try:
                for name, payload, size_key in (
                    ('records.jsonl', ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in new), 'records_bytes'),
                    ('execution.log', ''.join(format_record(r) for r in new), 'execution_bytes')):
                    with (directory / name).open('ab') as stream:
                        stream.write(payload.encode('utf-8'))
                        stream.flush()
                        os.fsync(stream.fileno())
                        cursor[size_key] = stream.tell()
                cursor['offset'] = max(cursor['offset'], offset)
                atomic_json(directory / 'cursor.json', cursor)
            except Exception:
                self._cursors.pop(run_id, None)
                raise
            self._cursors[run_id] = cursor

    def status(self, run_id, status):
        with self._lock:
            signature = (state_signature(status), status.get('seq'), status.get('updated_at'))
            if self._signatures.get(run_id) == signature:
                return False
            directory = self.directory(run_id)
            atomic_json(directory / 'status.json', status)
            message = {key: status.get(key) for key in ('state', 'result', 'seq', 'error',
                       'connection_error', 'environment_message', 'safety_stop', 'attachment_error')}
            with (directory / 'controller.log').open('a', encoding='utf-8') as stream:
                stream.write(utc_now() + ' ' + json.dumps(message, ensure_ascii=False) + '\n')
            self._signatures[run_id] = signature
            return True

    def save_artifact(self, run_id, name, stream):
        if Path(name).name != name or name in ('.', '..', 'cursor.json'):
            raise ValueError('非法日志文件名')
        directory = self.directory(run_id) / 'device'
        directory.mkdir(exist_ok=True)
        path = directory / name
        temporary = path.with_suffix(path.suffix + '.tmp')
        try:
            with temporary.open('wb') as target:
                size = 0
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 128 * 2**20:
                        raise ValueError('设备日志附件超过 128 MiB')
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def metadata(self, run_id, name, value):
        if name not in ('context.json', 'manifest.json', 'config_snapshot.json'):
            raise ValueError('未知日志元数据')
        atomic_json(self.directory(run_id) / name, value)

    def tail(self, run_id, name, after_seq=0):
        if name not in ('records.jsonl', 'controller.log'):
            raise ValueError('未知日志')
        path = self.directory(run_id, create=False) / name
        if not path.exists():
            return [] if name == 'records.jsonl' else ''
        with self._lock, path.open('rb') as stream:
            size = path.stat().st_size
            stream.seek(max(0, size - 2**20))
            if stream.tell():
                stream.readline()
            data = stream.read().decode('utf-8', errors='replace')
        if name == 'controller.log':
            return data
        result = []
        for line in data.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record['seq'] > after_seq:
                result.append(record)
        return result
