from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import ipaddress
import queue
import threading
import time

from ..adapters.ssh_transport import SSHTransport
from ..adapters.environment import DeploymentCancelled
from ..domain.models import conclusion, digest, new_id, utc_now
from ..domain.policies import validate_plan
from ..infrastructure.repository import StorageUnavailable, RunOwned
from ..infrastructure.local_logs import LocalLogs
from ..infrastructure.log_summary import CLOUD_INTERVAL_S, cloud_status, state_signature


class RunService:
    """每个设备一条串行控制通道；跨设备并行，事件通过线程安全队列交给 GUI。"""
    def __init__(self, repository, transport_factory=SSHTransport, local_logs=None):
        self.repository = repository
        self.local_logs = local_logs if local_logs is not None else LocalLogs()
        self._cloud_status = {}
        self.controller_id = new_id()
        self.transport_factory = transport_factory
        self.events = queue.Queue(maxsize=2048)
        self.pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix='master-aging')
        self._lock = threading.Lock()
        self.active = {}
        self.power_confirmed_batches = set()
        self.detaching = threading.Event()

    def notify(self, run_id, status):
        try:
            self.events.put_nowait((run_id, deepcopy(status)))
        except queue.Full:
            # UI 可以从 DB 重读，持久化绝不依赖界面消费速度。
            pass

    def start(self, devices, plan):
        plan = deepcopy(validate_plan(plan))
        plan.pop('max_parallel_devices', None)
        unverified = [device['ip'] for device in devices
                      if device.get('identity_source') in ('hostname', 'unverified')]
        if unverified:
            raise ValueError('以下设备尚未确认 SN，请先读取设备信息，确认 SSH 可连接及 /etc/serial-id 有效：'
                             + ', '.join(unverified))
        if not devices or len({d['sn'] for d in devices}) != len(devices):
            raise ValueError('请选择设备，且同批 SN 不得重复')
        if len({d['ip'] for d in devices}) != len(devices):
            raise ValueError('不同 SN 不能指向同一个 IP')
        if self.detaching.is_set():
            raise ValueError('工具正在退出，不能开始新运行')
        with self._lock:
            busy = {s['device']['sn'] for s in self.active.values()}
            if busy:
                raise ValueError('当前批次仍在运行或补采，请结束后再开始下一批')
        unfinished = {row['sn'] for row in self.repository.runs(unfinished=True)}
        if any(device['sn'] in busy | unfinished for device in devices):
            raise ValueError('所选设备存在未结束运行，请先恢复补采并停止原运行')
        # 本批勾选多少台就同时执行多少台，不再通过计划字段限制或排队。
        previous_pool = self.pool
        self.pool = ThreadPoolExecutor(max_workers=len(devices), thread_name_prefix='master-aging')
        previous_pool.shutdown(wait=False)
        batch_id = new_id()
        gate = threading.Semaphore(len(devices))
        entries = []
        for device in devices:
            snapshot = deepcopy(plan)
            snapshot['software_version'] = '1.0.0'
            snapshot['config_hash'] = digest(plan)
            entries.append((new_id(), device, snapshot))
        # 数据库中整批提交成功后才允许部署设备。
        self.repository.create_batch(batch_id, entries)
        for run_id, device, snapshot in entries:
            state = self._register(run_id, device, batch_id,
                                   {'state': 'PREFLIGHT', 'items': {}, 'result': None})
            self.pool.submit(self._run, run_id, device, snapshot, state, gate, False)
        return [entry[0] for entry in entries]

    def _register(self, run_id, device, batch_id, initial_status=None):
        with self._lock:
            if run_id in self.active:
                raise ValueError('该运行正在连接或执行')
            if any(s['device']['sn'] == device['sn'] for s in self.active.values()):
                raise ValueError('同一 SN 已有本地运行，先停止或恢复原运行')
            self.local_logs.metadata(run_id, 'context.json', {'device': device, 'batch_id': batch_id})
            state = {'device': device, 'batch_id': batch_id, 'status': {}, 'confirmation_queued': False,
                     'initial_status': deepcopy(initial_status or {}),
                     'stop': threading.Event(), 'commands': queue.Queue(),
                     'wake': threading.Event(), 'future_done': threading.Event()}
            self.active[run_id] = state
            return state

    def resume(self, run_id, ip=None):
        row = self.repository.get(run_id)
        if not row or row['sync_complete']:
            raise ValueError('运行已结束或不存在')
        device = {'sn': row['sn'], 'ip': str(ipaddress.IPv4Address(ip or row['ip']))}
        plan = json.loads(row['snapshot'])
        state = self._register(run_id, device, row['batch_id'], json.loads(row['status']))
        self.pool.submit(self._run, run_id, device, plan, state, threading.Semaphore(1), True)

    def stop(self, run_id):
        with self._lock:
            if run_id in self.active:
                self.active[run_id]['stop'].set()
                self.active[run_id]['wake'].set()

    def confirm_batch(self, run_id):
        """人工确认作用于整批；尚在部署的设备待首次等待电源时自动提交。"""
        with self._lock:
            if run_id not in self.active:
                return []
            batch_id = self.active[run_id]['batch_id']
            self.power_confirmed_batches.add(batch_id)
            ids = []
            for member_id, control in self.active.items():
                if control['batch_id'] == batch_id:
                    ids.append(member_id)
                    self._queue_power_confirmation(control)
            return ids

    def _queue_power_confirmation(self, control):
        # 调用方持有 _lock；各设备仍使用自己的阶段/序号，保留设备端核验与重试。
        status = control['status']
        battery = status.get('battery', {})
        item = status.get('items', {}).get('battery')
        if (control['batch_id'] not in self.power_confirmed_batches or control['confirmation_queued']
                or control['stop'].is_set() or self.detaching.is_set()
                or status.get('state') not in ('RUNNING', 'WAIT_OPERATOR')
                or item is None or item.get('result') is not None or battery.get('confirmed')
                or battery.get('state') != 'WAIT_OPERATOR' or battery.get('phase') not in ('charge', 'discharge')
                or not isinstance(battery.get('index'), int)):
            return
        control['commands'].put({'phase': battery['phase'], 'token': battery['index'], 'operator': ''})
        control['confirmation_queued'] = True
        control['wake'].set()

    def _store(self, run_id, status):
        with self._lock:
            control = self.active.get(run_id)
            if control:
                control['status'] = deepcopy(status)
                self._queue_power_confirmation(control)
        try:
            local_changed = self.local_logs.status(run_id, status)
        except OSError as exc:
            raise LocalLogUnavailable('本地详细日志写入失败：' + str(exc)) from exc
        if local_changed:
            self.notify(run_id, status)
        signature = state_signature(status)
        payload = cloud_status(status)
        previous = self._cloud_status.get(run_id)
        now = time.monotonic()
        changed = previous is None or signature != previous['signature']
        if changed or (payload != previous['payload'] and now - previous['at'] >= CLOUD_INTERVAL_S):
            self.repository.save_status(run_id, payload, log_transition=changed)
            self._cloud_status[run_id] = {'signature': signature, 'payload': payload, 'at': now}
        if not status.get('launch_requested') and not status.get('run_id') and status['state'] in ('BLOCKED', 'FINISHED'):
            self.repository.mark_synced(run_id)

    def _ingest(self, transport, run_id, cloud=True):
        # 数据库失败后从较早的游标重读；本地 append 幂等，不丢失待上传摘要。
        try:
            offset = self.local_logs.cursor(run_id)['offset']
            if cloud:
                offset = min(offset, self.repository.get(run_id)['record_offset'])
        except OSError as exc:
            raise LocalLogUnavailable('本地日志游标读取失败：' + str(exc)) from exc
        while True:
            records, offset, more = transport.records(run_id, offset)
            if records:
                try:
                    self.local_logs.append(run_id, records, offset)
                except OSError as exc:
                    raise LocalLogUnavailable('本地详细日志写入失败：' + str(exc)) from exc
                if cloud:
                    self.repository.ingest(run_id, records, offset)
            if not more:
                break

    def _run(self, run_id, device, plan, control, gate, recovering):
        transport = None
        stop_sent = False
        last_status = dict(control.get('initial_status') or {'state': 'PREFLIGHT', 'items': {}, 'result': None})
        launched = recovering and bool(last_status.get('launch_requested') or last_status.get('run_id'))
        acquired = False
        failures = 0
        confirmed_reboot = False
        confirmation_poll_until = 0
        claimed = False
        last_claim = 0
        ingested_seq = None
        try:
            while not gate.acquire(timeout=0.2):
                if self.detaching.is_set() or control['stop'].is_set():
                    break
            else:
                acquired = True
            if not acquired or (control['stop'].is_set() and not launched):
                last_status.update(state='FINISHED', result='ABORTED', error='运行前取消')
                self._store(run_id, last_status)
                return
            if self.detaching.is_set() and not launched:
                last_status.update(state='FINISHED', result='ABORTED', error='退出前尚未启动设备任务')
                self._store(run_id, last_status)
                return
            while not self.detaching.is_set():
                # 先清除唤醒，再处理命令；处理期间到达的新命令不会丢失唤醒。
                control['wake'].clear()
                validated_status = None
                try:
                    # 已持有控制权时，人工停止优先，不因数据库短暂断线延迟发往设备。
                    if claimed and launched and transport and control['stop'].is_set() and not stop_sent:
                        transport.action('stop', run_id)
                        stop_sent = True
                    if not claimed or time.monotonic() - last_claim >= 60:
                        self.repository.claim(run_id, self.controller_id)
                        claimed = True
                        last_claim = time.monotonic()
                    if transport is None:
                        transport = self.transport_factory(device)
                        def progress(message):
                            if not launched:
                                last_status.update(state='DEPLOYING', environment_message=message)
                                self._store(run_id, last_status)
                        transport.progress = progress
                        transport.cancelled = lambda: not launched and (control['stop'].is_set() or self.detaching.is_set())
                        progress('正在连接设备并检查运行环境')
                        info = transport.probe(bootstrap=not launched)
                        if info['sn'] != device['sn']:
                            raise ValueError('重连设备 SN 不一致')
                        if last_status.get('hardware', {}).get('fingerprint') and info['fingerprint'] != last_status['hardware']['fingerprint']:
                            raise ValueError('重连设备指纹不一致')
                        confirmed_reboot = bool(last_status.get('boot_id') and info.get('boot_id')
                                                and last_status['boot_id'] != info['boot_id'])
                        self.repository.update_ip(run_id, device['ip'])
                        if not launched:
                            if info['chip'] == 'unknown' or info['chip'] != info['sn_chip']:
                                raise ValueError('真实硬件与 SN 机型无法核验一致，未部署老化依赖')
                            fingerprint = info['fingerprint']
                            last_status['hardware'] = info
                            info = transport.prepare(plan)
                            if info['sn'] != device['sn'] or info['fingerprint'] != fingerprint:
                                raise ValueError('环境部署后设备身份发生变化，未启动老化')
                            if control['stop'].is_set() or self.detaching.is_set():
                                raise DeploymentCancelled('环境准备期间已取消，未启动老化负载')
                            request = {'run_id': run_id, 'device': dict(device, fingerprint=info['fingerprint']), 'plan': plan}
                            manifest = {'run_id': run_id, 'device': info,
                                'created_at': utc_now(), 'config_hash': digest(plan), 'agent_sha256': transport.agent.rsplit('-', 1)[-1][:-4]}
                            self.local_logs.metadata(run_id, 'manifest.json', manifest)
                            self.local_logs.metadata(run_id, 'config_snapshot.json', plan)
                            self.repository.save_manifest(run_id, manifest)
                            # 在提交启动之前持久化意图。命令响应丢失后只查询原 run_id。
                            last_status.update(state='READY', hardware=info, launch_requested=True,
                                               environment_message='环境准备完成，正在启动老化预检')
                            self._store(run_id, last_status)
                            launched = True
                            transport.launch(request)
                    if control['stop'].is_set() and not stop_sent:
                        transport.action('stop', run_id)
                        stop_sent = True
                    while not control['stop'].is_set() and not control['commands'].empty():
                        confirmation = control['commands'].get_nowait()
                        try:
                            transport.action('confirm', run_id, **confirmation)
                            confirmation_poll_until = time.monotonic() + 5
                        except Exception:
                            # 人工每次运行只确认一次，发送失败必须保留命令待重连补发。
                            control['commands'].put(confirmation)
                            raise
                    status = transport.action('status', run_id)
                    if last_status.get('hardware') and not status.get('hardware'):
                        status['hardware'] = last_status['hardware']
                    elif last_status.get('hardware', {}).get('environment_deployment'):
                        status['hardware']['environment_deployment'] = last_status['hardware']['environment_deployment']
                    if status.get('run_id') != run_id:
                        raise ValueError('设备状态运行身份不匹配')
                    if status['state'] == 'MISSING':
                        if confirmed_reboot:
                            last_status.update(state='FINISHED', result='FAIL' if conclusion(last_status.get('items', {})) == 'FAIL' else 'INCOMPLETE',
                                               cleanup_confirmed=False, error='已确认设备重启，原运行目录丢失，不能恢复连续时长')
                            self._store(run_id, last_status)
                            self.repository.mark_synced(run_id)
                            return
                        raise ValueError('设备上未找到原运行，保留启动不确定性，不重复发起负载')
                    if status.get('config_hash') and status['config_hash'] != digest(plan):
                        raise ValueError('设备配置快照摘要与数据库不一致')
                    if last_status.get('boot_id') and status.get('boot_id') != last_status['boot_id']:
                        raise ValueError('状态 boot_id 不一致')
                    validated_status = status
                    if (status.get('battery', {}).get('confirmed')
                            or status.get('items', {}).get('battery', {}).get('result') is not None
                            or status['state'] in ('FINISHED', 'BLOCKED')):
                        confirmation_poll_until = 0
                    last_status = status
                    if status.get('agent_alive') is False:
                        last_status.update(state='FINISHED', result='INCOMPLETE', error='设备重启或 supervisor 已退出，禁止重复启动')
                        for item in last_status.get('items', {}).values():
                            if item.get('result') is None:
                                item.update(result='INCOMPLETE', stop_reason='设备执行中断')
                        last_status['cleanup_confirmed'] = False
                        if conclusion(last_status.get('items', {})) == 'FAIL':
                            last_status['result'] = 'FAIL'
                    if last_status['state'] in ('FINISHED', 'BLOCKED'):
                        self._ingest(transport, run_id)
                        self._store(run_id, last_status)
                    else:
                        # 运行状态先交给界面，日志补采较慢时不阻塞确认结果显示。
                        self._store(run_id, last_status)
                        if ingested_seq != status.get('seq'):
                            self._ingest(transport, run_id)
                            ingested_seq = status.get('seq')
                    failures = 0
                    if last_status['state'] in ('FINISHED', 'BLOCKED'):
                        try:
                            transport.collect(run_id, self.local_logs)
                            last_status.pop('attachment_error', None)
                            self._store(run_id, last_status)
                        except (StorageUnavailable, LocalLogUnavailable):
                            raise
                        except Exception as exc:
                            last_status['attachment_error'] = str(exc)
                            self._store(run_id, last_status)
                        if self.local_logs.cursor(run_id)['seq'] != status.get('seq', 0):
                            last_status.update(result='FAIL' if last_status.get('result') == 'FAIL' else 'INCOMPLETE',
                                               error='最终日志缺段，不能确认数据完整')
                            self._store(run_id, last_status)
                        if not last_status.get('attachment_error') and self.local_logs.cursor(run_id)['seq'] == status.get('seq', 0):
                            self.repository.mark_synced(run_id)
                        return
                except RunOwned:
                    raise
                except StorageUnavailable as exc:
                    # 本地完整日志可领先云端；云端游标仍须事务提交后才能推进。
                    failures += 1
                    self.notify(run_id, dict(last_status, storage_error=str(exc)))
                    if launched and transport:
                        try:
                            self._ingest(transport, run_id, cloud=False)
                        except Exception as local_exc:
                            self.notify(run_id, dict(last_status, storage_error=str(exc),
                                                     local_log_error=str(local_exc)))
                    if not launched and transport:
                        transport.close()
                        transport = None
                except LocalLogUnavailable as exc:
                    failures += 1
                    self.notify(run_id, dict(last_status, local_log_error=str(exc)))
                except DeploymentCancelled as exc:
                    last_status.update(state='FINISHED', result='ABORTED', error=str(exc), cleanup_confirmed=True)
                    self._store(run_id, last_status)
                    return
                except ValueError as exc:
                    if validated_status and validated_status['state'] in ('FINISHED', 'BLOCKED'):
                        last_status = validated_status
                        last_status.update(result='FAIL' if last_status.get('result') == 'FAIL' else 'INCOMPLETE', error=str(exc))
                        self._store(run_id, last_status)
                        return
                    # 证据不一致时不能宣称远端已停止；保留可恢复的运行和明确结论。
                    last_status.update(state='RECONNECTING' if launched else 'BLOCKED',
                                       result='INCOMPLETE' if launched else 'BLOCKED',
                                       cleanup_confirmed=False, error=str(exc))
                    self._store(run_id, last_status)
                    return
                except Exception as exc:
                    failures += 1
                    if not launched:
                        last_status.update(state='BLOCKED', result='BLOCKED', error='连接/部署失败: ' + str(exc))
                        self._store(run_id, last_status)
                        return
                    last_status.update(state='RECONNECTING', connection_error=str(exc), stop_pending=control['stop'].is_set())
                    self._store(run_id, last_status)
                    if transport:
                        transport.close()
                        transport = None
                # 确认落盘和执行器消费异步发生；短时加快查询，读取异常时避免持续高频轮询。
                delay = 0.2 if time.monotonic() < confirmation_poll_until and not failures else min(10, 2 + failures)
                control['wake'].wait(delay)
        except RunOwned as exc:
            last_status['storage_error'] = str(exc)
            self.notify(run_id, last_status)
        except Exception as exc:
            last_status.update(state='RECONNECTING' if launched else 'BLOCKED', result=None if launched else 'BLOCKED', error=str(exc))
            try:
                self._store(run_id, last_status)
            except StorageUnavailable as storage_error:
                last_status['storage_error'] = str(storage_error)
                self.notify(run_id, last_status)
            except LocalLogUnavailable as local_error:
                last_status['local_log_error'] = str(local_error)
                self.notify(run_id, last_status)
        finally:
            if transport:
                transport.close()
            if acquired:
                gate.release()
            if claimed:
                try:
                    self.repository.release(run_id, self.controller_id)
                except StorageUnavailable:
                    pass  # 连接中断时租约自然过期。
            with self._lock:
                self.active.pop(run_id, None)
            control['future_done'].set()
            self.notify(run_id, last_status)

    def shutdown(self, detach=False):
        if detach:
            self.detaching.set()
        with self._lock:
            for control in self.active.values():
                if not detach:
                    control['stop'].set()
                control['wake'].set()
        if detach and not self.busy():
            self.pool.shutdown(wait=False)

    def busy(self):
        with self._lock:
            return bool(self.active)


class LocalLogUnavailable(RuntimeError):
    """本地磁盘不可写时保留设备日志，重试成功前不宣称日志补采完成。"""
