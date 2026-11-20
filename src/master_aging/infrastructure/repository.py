"""线程间串行提交；每个事务独占连接，禁止跨线程共用 SQLite connection。"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from .config import runtime_dir


class Repository:
    def __init__(self, path=None):
        self.path = path or runtime_dir() / 'aging.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS batch (
                  batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, operator TEXT, station TEXT);
                CREATE TABLE IF NOT EXISTS run (
                  run_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, sn TEXT NOT NULL, ip TEXT NOT NULL,
                  created_at TEXT NOT NULL, state TEXT NOT NULL, result TEXT, snapshot TEXT NOT NULL,
                  status TEXT NOT NULL, output_dir TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_sn ON run(sn, created_at);
                CREATE TABLE IF NOT EXISTS record (
                  run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                  PRIMARY KEY(run_id, seq));
                CREATE INDEX IF NOT EXISTS record_kind ON record(run_id, kind, seq);
                CREATE TABLE IF NOT EXISTS item_run (
                  run_id TEXT NOT NULL, item TEXT NOT NULL, result TEXT, payload TEXT NOT NULL,
                  PRIMARY KEY(run_id, item));
                CREATE TABLE IF NOT EXISTS write_ledger (
                  run_id TEXT NOT NULL, target TEXT NOT NULL, payload TEXT NOT NULL,
                  PRIMARY KEY(run_id, target));
            ''')

    @contextmanager
    def connection(self):
        with self._lock:
            db = sqlite3.connect(str(self.path), timeout=15)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA synchronous=FULL')
                with db:
                    yield db
            finally:
                db.close()

    def create(self, batch_id, run_id, device, plan, directory):
        from ..domain.models import utc_now
        now = utc_now()
        status = {'state': 'PREFLIGHT', 'items': {}, 'result': None}
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO batch (batch_id, created_at) VALUES (?,?)',
                       (batch_id, now))
            db.execute('INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (run_id, batch_id, device['sn'], device['ip'], now, 'PREFLIGHT', None,
                        json.dumps(plan, ensure_ascii=False), json.dumps(status), str(directory)))

    def save_status(self, run_id, status):
        with self.connection() as db:
            db.execute('UPDATE run SET state=?, result=?, status=? WHERE run_id=?',
                       (status['state'], status.get('result'), json.dumps(status, ensure_ascii=False), run_id))
            for key, item in status.get('items', {}).items():
                db.execute('INSERT OR REPLACE INTO item_run VALUES (?,?,?,?)',
                           (run_id, key, item.get('result'), json.dumps(item, ensure_ascii=False)))
            for target, ledger in status.get('write_ledger', {}).items():
                db.execute('INSERT OR REPLACE INTO write_ledger VALUES (?,?,?)',
                           (run_id, target, json.dumps(ledger, ensure_ascii=False)))

    def update_ip(self, run_id, ip):
        with self.connection() as db:
            db.execute('UPDATE run SET ip=? WHERE run_id=?', (ip, run_id))

    def ingest(self, run_id, records):
        from ..domain.models import utc_now
        with self.connection() as db:
            for record in records:
                existing = db.execute('SELECT payload FROM record WHERE run_id=? AND seq=?', (run_id, record['seq'])).fetchone()
                if existing:
                    saved = json.loads(existing[0])
                    saved.pop('received_at', None)
                    if saved != record:
                        raise ValueError('相同运行/序号的设备记录内容发生变化')
                    continue
                payload = dict(record, received_at=utc_now())
                db.execute('INSERT INTO record VALUES (?,?,?,?)',
                           (run_id, record['seq'], record['kind'], json.dumps(payload, ensure_ascii=False)))

    def last_seq(self, run_id):
        with self.connection() as db:
            return db.execute('SELECT COALESCE(MAX(seq),0) FROM record WHERE run_id=?', (run_id,)).fetchone()[0]

    def records(self, run_id, kind=None):
        with self.connection() as db:
            sql = 'SELECT payload FROM record WHERE run_id=?'
            args = [run_id]
            if kind:
                sql += ' AND kind=?'
                args.append(kind)
            rows = db.execute(sql + ' ORDER BY seq', args).fetchall()
            return [json.loads(row[0]) for row in rows]

    def runs(self, unfinished=False):
        with self.connection() as db:
            sql = 'SELECT * FROM run'
            if unfinished:
                sql += " WHERE state NOT IN ('FINISHED', 'BLOCKED')"
            return [dict(r) for r in db.execute(sql + ' ORDER BY created_at DESC').fetchall()]

    def get(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM run WHERE run_id=?', (run_id,)).fetchone()
            return dict(row) if row else None
