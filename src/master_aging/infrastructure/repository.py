"""MySQL 保存老化结论、关键事件与精简指标；详细日志由 PC 单独落盘。"""
from contextlib import contextmanager
import hashlib
import base64
import json
from copy import deepcopy

import pymysql

from .config import database_settings
from .log_summary import CLOUD_INTERVAL_S, cloud_record, cloud_status, important_metric, compact
from ..domain.models import utc_now
from ..domain.interruption import finish_interrupted


class StorageUnavailable(RuntimeError):
    """数据库事务未完成，设备日志和采集游标必须保持可重试。"""


class RunOwned(RuntimeError):
    """另一台控制端仍在准备和提交新任务启动。"""


REQUIRED_TABLES = (
    'master_aging_batch',
    'master_aging_run',
    'master_aging_device',
    'master_aging_record',
    'master_aging_item_run',
    'master_aging_ledger',
    'master_aging_ctrl_log',
    'master_aging_artifact',
    'master_aging_file_chunk',
)


def encode(value):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(payload.encode('utf-8')) > 65535:
        raise ValueError('单条结构化记录超过 TEXT 的 65535 字节上限，已拒绝写入，原始数据未截断')
    return payload


def merge_run_status(previous, incoming):
    """多个工具可补采同一任务；较旧样本和局部断线不能覆盖设备新状态。"""
    terminal = ('FINISHED', 'BLOCKED')
    if incoming.get('interruption') and incoming['state'] == 'FINISHED':
        if previous.get('state') in terminal and not previous.get('interruption'):
            return deepcopy(previous)  # 已落盘的正常终态优先于其他工具的事后中断推断。
        if (previous.get('seq') or 0) > (incoming.get('seq') or 0):
            # 断电前的云端采样可能比设备落盘更新；保留这些进度后再结束未完成项目。
            merged = finish_interrupted(previous, incoming['interruption'])
            for key, item in incoming.get('items', {}).items():
                merged['items'].setdefault(key, deepcopy(item))
            for key in ('attachment_error', 'evidence_error'):
                if incoming.get(key):
                    merged[key] = incoming[key]
            return merged
    older = (incoming.get('seq') or 0) < (previous.get('seq') or 0)
    regresses_final = previous.get('state') in terminal and incoming['state'] not in terminal
    local_disconnect = (bool(previous.get('run_id')) and incoming['state'] == 'RECONNECTING'
                        and previous.get('state') != 'RECONNECTING')
    merged = deepcopy(previous if older or regresses_final or local_disconnect else incoming)
    if merged.get('state') not in terminal and (previous.get('stop_pending') or incoming.get('stop_pending')):
        merged['stop_pending'] = True
    return merged


class Repository:
    def __init__(self, settings=None):
        self.settings = dict(settings if settings is not None else database_settings())
        if not self.settings.get('database'):
            raise ValueError('请在 config/master_aging/database.yaml 中填写 MySQL 库名 database')
        with self.connection() as db:
            db.execute('SHOW TABLES')
            tables = {value for row in db.fetchall() for value in row.values()}
            required = set(REQUIRED_TABLES)
            missing = required - tables
            if missing:
                raise StorageUnavailable('老化数据表尚未初始化：' + '、'.join(sorted(missing)) +
                    '。请联系数据库管理员按审批流程补齐老化表后重新启动。')

    @property
    def description(self):
        return '%s:%s/%s' % (self.settings['host'], self.settings.get('port', 3306), self.settings['database'])

    @contextmanager
    def connection(self):
        db = None
        try:
            db = pymysql.connect(**self.settings, charset='utf8mb4', autocommit=False,
                                 cursorclass=pymysql.cursors.DictCursor)
            with db.cursor() as cursor:
                yield cursor
            db.commit()
        except pymysql.MySQLError as exc:
            if db is not None:
                db.rollback()
            # 不包含 DSN、密码或 SQL 参数。
            raise StorageUnavailable('MySQL 数据读写失败（错误码 %s），请检查数据库连接或权限' %
                                     (exc.args[0] if exc.args else '未知')) from exc
        except Exception:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def create_batch(self, batch_id, entries):
        """整批原子创建，并锁定 SN，防止多台 PC 并发启动同一设备。"""
        now = utc_now()
        with self.connection() as db:
            for run_id, device, plan in sorted(entries, key=lambda entry: entry[1]['sn']):
                db.execute('INSERT IGNORE INTO master_aging_device (sn) VALUES (%s)', (device['sn'],))
                db.execute('SELECT run_id FROM master_aging_device WHERE sn=%s FOR UPDATE', (device['sn'],))
                previous = db.fetchone()['run_id']
                if previous:
                    db.execute('SELECT state,sync_complete FROM master_aging_run WHERE run_id=%s', (previous,))
                    row = db.fetchone()
                    if row and row['state'] not in ('FINISHED', 'BLOCKED'):
                        raise ValueError('设备 %s 存在未结束的运行，请先恢复并结束原运行' % device['sn'])
            db.execute('INSERT INTO master_aging_batch (batch_id,created_at) VALUES (%s,%s)', (batch_id, now))
            for run_id, device, plan in entries:
                status = {'state': 'PREFLIGHT', 'items': {}, 'result': None}
                db.execute('''INSERT INTO master_aging_run
                    (run_id,batch_id,sn,ip,created_at,state,result,snapshot,run_status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (run_id, batch_id, device['sn'], device['ip'], now, 'PREFLIGHT', None, encode(plan), encode(status)))
                db.execute('UPDATE master_aging_device SET run_id=%s WHERE sn=%s', (run_id, device['sn']))

    def save_status(self, run_id, status, log_transition=True):
        status = cloud_status(status)
        with self.connection() as db:
            db.execute('SELECT run_status FROM master_aging_run WHERE run_id=%s FOR UPDATE', (run_id,))
            saved = db.fetchone()
            if not saved:
                raise ValueError('运行不存在')
            status = merge_run_status(json.loads(saved['run_status']), status)
            db.execute('UPDATE master_aging_run SET state=%s,result=%s,run_status=%s WHERE run_id=%s',
                       (status['state'], status.get('result'), encode(status), run_id))
            for key, item in status.get('items', {}).items():
                db.execute('''INSERT INTO master_aging_item_run (run_id,item,result,payload) VALUES (%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE result=VALUES(result),payload=VALUES(payload)''',
                    (run_id, key, item.get('result'), encode(item)))
            for target, ledger in status.get('write_ledger', {}).items():
                db.execute('''INSERT INTO master_aging_ledger (run_id,target,payload) VALUES (%s,%s,%s)
                    ON DUPLICATE KEY UPDATE payload=VALUES(payload)''', (run_id, target, encode(ledger)))
            message = '%s result=%s seq=%s %s' % (status['state'], status.get('result'), status.get('seq'),
                status.get('error') or status.get('connection_error') or status.get('environment_message') or '')
            if log_transition:
                db.execute('INSERT INTO master_aging_ctrl_log (run_id,created_at,message) VALUES (%s,%s,%s)',
                           (run_id, utc_now(), message))
        return status

    def request_stop(self, run_id):
        """准备阶段也可随时登记停止，实际启动者在启动前消费此请求。"""
        with self.connection() as db:
            db.execute('SELECT run_status FROM master_aging_run WHERE run_id=%s FOR UPDATE', (run_id,))
            saved = db.fetchone()
            if not saved:
                raise ValueError('运行不存在')
            status = json.loads(saved['run_status'])
            if status['state'] not in ('FINISHED', 'BLOCKED') and not status.get('stop_pending'):
                status['stop_pending'] = True
                db.execute('UPDATE master_aging_run SET run_status=%s WHERE run_id=%s', (encode(status), run_id))

    def update_ip(self, run_id, ip):
        with self.connection() as db:
            db.execute('UPDATE master_aging_run SET ip=%s WHERE run_id=%s', (ip, run_id))

    def save_manifest(self, run_id, manifest):
        with self.connection() as db:
            db.execute('UPDATE master_aging_run SET manifest=%s WHERE run_id=%s', (encode(compact(manifest)), run_id))

    def mark_synced(self, run_id):
        with self.connection() as db:
            db.execute('UPDATE master_aging_run SET sync_complete=1 WHERE run_id=%s', (run_id,))

    def claim(self, run_id, controller_id):
        with self.connection() as db:
            db.execute('''UPDATE master_aging_run
                SET controller_id=%s,lease_until=DATE_ADD(UTC_TIMESTAMP(),INTERVAL 5 MINUTE)
                WHERE run_id=%s AND (controller_id IS NULL OR controller_id=%s OR lease_until<UTC_TIMESTAMP())''',
                (controller_id, run_id, controller_id))
            # MySQL 可把同秒内的续租认作未改变，不能只看 affected_rows。
            db.execute('SELECT controller_id FROM master_aging_run WHERE run_id=%s', (run_id,))
            row = db.fetchone()
            if not row or row['controller_id'] != controller_id:
                raise RunOwned('原任务仍在另一台工具上准备启动，正在等待其提交状态；停止请求会交给原准备流程')

    def release(self, run_id, controller_id):
        with self.connection() as db:
            db.execute('UPDATE master_aging_run SET controller_id=NULL,lease_until=NULL WHERE run_id=%s AND controller_id=%s',
                       (run_id, controller_id))

    def ingest(self, run_id, records, offset=None):
        with self.connection() as db:
            # 串行化同一运行的补采，记录和字节游标在一个事务中提交。
            db.execute('SELECT record_offset FROM master_aging_run WHERE run_id=%s FOR UPDATE', (run_id,))
            row = db.fetchone()
            if row is None:
                raise ValueError('运行不存在')
            if offset is not None and offset <= row['record_offset']:
                return  # 本机详细日志从头补采时，已提交的云端部分无需再次写入。
            db.execute("SELECT payload FROM master_aging_record WHERE run_id=%s AND kind='metric' ORDER BY seq DESC LIMIT 1", (run_id,))
            previous = db.fetchone()
            previous = json.loads(previous['payload']) if previous else None
            existing_records = {}
            if records:
                db.execute('SELECT seq,payload FROM master_aging_record WHERE run_id=%s AND seq BETWEEN %s AND %s',
                           (run_id, records[0]['seq'], records[-1]['seq']))
                existing_records = {entry['seq']: entry['payload'] for entry in db.fetchall()}
            inserts = []
            for record in records:
                summary = cloud_record(record)
                existing = existing_records.get(record['seq'])
                if existing:
                    saved = json.loads(existing)
                    saved.pop('received_at', None)
                    if cloud_record(saved) != summary:
                        raise ValueError('相同运行/序号的设备记录内容发生变化')
                    continue
                if record['kind'] == 'metric':
                    if (previous and previous.get('boot_id') == record.get('boot_id')
                            and (record['seq'] < previous['seq']
                                 or (0 <= record['monotonic'] - previous['monotonic'] < CLOUD_INTERVAL_S
                                     and not important_metric(record)))):
                        continue
                    previous = record
                payload = dict(summary, received_at=utc_now())
                inserts.append((run_id, record['seq'], record['kind'], encode(payload)))
            if inserts:
                db.executemany('INSERT INTO master_aging_record (run_id,seq,kind,payload) VALUES (%s,%s,%s,%s)', inserts)
            if offset is not None:
                db.execute('UPDATE master_aging_run SET record_offset=GREATEST(record_offset,%s) WHERE run_id=%s', (offset, run_id))

    def last_seq(self, run_id):
        with self.connection() as db:
            db.execute('SELECT COALESCE(MAX(seq),0) AS seq FROM master_aging_record WHERE run_id=%s', (run_id,))
            return db.fetchone()['seq']

    def records(self, run_id, kind=None, after_seq=0, limit=None):
        with self.connection() as db:
            sql = 'SELECT payload FROM master_aging_record WHERE run_id=%s AND seq>%s'
            args = [run_id, after_seq]
            if kind:
                sql += ' AND kind=%s'
                args.append(kind)
            sql += ' ORDER BY seq'
            if limit is not None:
                sql += ' LIMIT %s'
                args.append(limit)
            db.execute(sql, args)
            return [json.loads(row['payload']) for row in db.fetchall()]

    def runs(self, unfinished=False, batch_id=None):
        with self.connection() as db:
            conditions, args = [], []
            if unfinished:
                conditions.append('sync_complete=0')
            if batch_id:
                conditions.append('batch_id=%s')
                args.append(batch_id)
            sql = 'SELECT * FROM master_aging_run'
            if conditions:
                sql += ' WHERE ' + ' AND '.join(conditions)
            db.execute(sql + ' ORDER BY created_at DESC,run_id', args)
            return [self._run_row(row) for row in db.fetchall()]

    def get(self, run_id):
        with self.connection() as db:
            db.execute('SELECT * FROM master_aging_run WHERE run_id=%s', (run_id,))
            return self._run_row(db.fetchone())

    def unfinished_for_sn(self, sn):
        """优先恢复活动任务；已结束的任务只自动恢复设备当前关联的一次。"""
        with self.connection() as db:
            db.execute('''SELECT * FROM master_aging_run WHERE sn=%s AND sync_complete=0
                          AND (state NOT IN ('FINISHED','BLOCKED') OR run_id=(
                              SELECT run_id FROM master_aging_device WHERE sn=%s))
                          ORDER BY created_at DESC,run_id LIMIT 2''', (sn, sn))
            rows = db.fetchall()
            if len(rows) > 1:
                raise ValueError('设备 %s 存在多个未同步运行，请在历史记录中选择原运行恢复' % sn)
            return self._run_row(rows[0]) if rows else None

    @staticmethod
    def _run_row(row):
        if row is not None:
            row['status'] = row.pop('run_status')
        return row

    def controller_logs(self, run_id, after_id=0):
        with self.connection() as db:
            db.execute('SELECT id,created_at,message FROM master_aging_ctrl_log WHERE run_id=%s AND id>%s ORDER BY id',
                       (run_id, after_id))
            return list(db.fetchall())

    def save_artifact(self, run_id, name, stream):
        """从 SFTP 分块写入，单个附件原子提交，不经过 PC 临时文件。"""
        checksum, size, part = hashlib.sha256(), 0, 0
        with self.connection() as db:
            db.execute('SELECT run_id FROM master_aging_run WHERE run_id=%s FOR UPDATE', (run_id,))
            if not db.fetchone():
                raise ValueError('运行不存在')
            db.execute('DELETE FROM master_aging_file_chunk WHERE run_id=%s AND name=%s', (run_id, name))
            while True:
                chunk = stream.read(32 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 128 * 2**20:
                    raise ValueError('设备附件超过 128 MiB：' + name)
                checksum.update(chunk)
                db.execute('INSERT INTO master_aging_file_chunk (run_id,name,part,content) VALUES (%s,%s,%s,%s)', (run_id, name, part, base64.b64encode(chunk).decode('ascii')))
                part += 1
            db.execute('''INSERT INTO master_aging_artifact (run_id,name,size,sha256) VALUES (%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE size=VALUES(size),sha256=VALUES(sha256)''', (run_id, name, size, checksum.hexdigest()))

    def artifacts(self, run_id):
        with self.connection() as db:
            db.execute('SELECT name,size,sha256 FROM master_aging_artifact WHERE run_id=%s ORDER BY name', (run_id,))
            return list(db.fetchall())

    def artifact(self, run_id, name):
        with self.connection() as db:
            db.execute('SELECT size,sha256 FROM master_aging_artifact WHERE run_id=%s AND name=%s', (run_id, name))
            metadata = db.fetchone()
            if metadata is None:
                raise ValueError('附件不存在')
            db.execute('SELECT content FROM master_aging_file_chunk WHERE run_id=%s AND name=%s ORDER BY part', (run_id, name))
            content = b''.join(base64.b64decode(row['content'], validate=True) for row in db.fetchall())
            if len(content) != metadata['size'] or hashlib.sha256(content).hexdigest() != metadata['sha256']:
                raise ValueError('附件校验失败')
            return content
