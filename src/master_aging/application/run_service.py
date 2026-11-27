from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import ipaddress
import queue
import threading
import time

from ..adapters.transport import create_transport, is_vsm
from ..adapters.environment import DeploymentCancelled
from ..domain.models import digest, new_id, utc_now
from ..domain.interruption import finish_interrupted
from ..domain.policies import validate_plan
from ..infrastructure.repository import StorageUnavailable, RunOwned
from ..infrastructure.local_logs import LocalLogs
from ..infrastructure.log_summary import CLOUD_INTERVAL_S, cloud_status, state_signature


class RunService:
    """每个设备一条串行控制通道；跨设备并行，事件通过线程安全队列交给 GUI。"""
    def __init__(self, repository, transport_factory=create_transport, local_logs=None):
        self.repository = repository
        self.local_logs = local_logs if local_logs is not None else LocalLogs()
        self._cloud_status = {}
        self.controller_id = new_id()
        self.transport_factory = transport_factory
        self.events = queue.Queue(maxsize=2048)
        self.pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix='master-aging')
        self._lock = threading.Lock()
        self._recovery_lock = threading.Lock()
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
        with self._recovery_lock:
            return self._start(devices, plan)

    def _start(self, devices, plan):
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
        unfinished = {row['sn'] for row in self.repository.runs(unfinished=True)
                      if row['state'] not in ('FINISHED', 'BLOCKED')}
        if any(device['sn'] in busy | unfinished for device in devices):
            raise ValueError('所选设备仍在运行、等待重连或收尾补采，请结束原运行后再测试')
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
                     'wake': threading.Event(), 'reconnect': threading.Event(), 'future_done': threading.Event()}
            if (initial_status or {}).get('stop_pending'):
                state['stop'].set()
            self.active[run_id] = state
            return state

    def recover_device(self, device):
        """扫描或手动添加后，仅接回该 SN 的原任务，不创建新批次。"""
        if device.get('identity_source') in ('hostname', 'unverified'):
            return None
        row = self.repository.unfinished_for_sn(device['sn'])
        if row:
            self.resume(row['run_id'], device=device)
        return row

    def resume(self, run_id, ip=None, device=None, stop=False):
        # 同一次发现、历史恢复、停止按钮可能同时触发；每个运行只注册一个工作线程。
        with self._recovery_lock:
            return self._resume(run_id, ip, device, stop)

    def _resume(self, run_id, ip, device, stop):
        if self.detaching.is_set():
            raise ValueError('工具正在退出，不能恢复运行')
        row = self.repository.get(run_id)
        if not row or row['sync_complete']:
            raise ValueError('运行已结束或不存在')
        if device is not None:
            device = deepcopy(device)
            if device['sn'] != row['sn']:
                raise ValueError('恢复设备的 SN 与原运行不一致')
        else:
            address = ip or row['ip']
            if is_vsm({'ip': address}):
                from ..adapters.vsm import check_platform, manual_device
                check_platform()
                device = manual_device(row['sn'])
            else:
                device = {'sn': row['sn'], 'ip': str(ipaddress.IPv4Address(address))}
        with self._lock:
            control = self.active.get(run_id)
            if control:
                if any(control['device'].get(key) != device.get(key) for key in ('ip', 'port', 'transport')):
                    control['device'] = device
                    control['reconnect'].set()
                if stop:
                    control['stop'].set()
                control['wake'].set()
                return row
        plan = json.loads(row['snapshot'])
        state = self._register(run_id, device, row['batch_id'], json.loads(row['status']))
        if stop:
            state['stop'].set()
        self.pool.submit(self._run, run_id, device, plan, state, threading.Semaphore(1), True)
        return row

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
        stored = previous.get('stored', status) if previous else status
        now = time.monotonic()
        changed = previous is None or signature != previous['signature']
        if changed or (payload != previous['payload'] and now - previous['at'] >= CLOUD_INTERVAL_S):
            stored = self.repository.save_status(run_id, payload, log_transition=changed)
            if stored.get('stop_pending') and control:
                control['stop'].set()
                control['wake'].set()
            self._cloud_status[run_id] = {'signature': signature, 'payload': payload, 'stored': stored, 'at': now}
        if not status.get('launch_requested') and not status.get('run_id') and status['state'] in ('BLOCKED', 'FINISHED'):
            self.repository.mark_synced(run_id)
        return stored

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

    def _interrupted(self, run_id, previous, remote, plan, current_boot):
        # 以云端/设备中较新的原任务进度收尾，不增加采样序号或有效秒数。
        saved = json.loads(self.repository.get(run_id)['status'])
        if saved.get('state') in ('FINISHED', 'BLOCKED'):
            return saved
        candidates = (previous, saved, remote) if remote['state'] not in ('MISSING', 'CORRUPT') else (previous, saved)
        latest = deepcopy(max(reversed(candidates), key=lambda value: value.get('seq') or 0))
        # 已上传的失败证据不能因设备较新的状态文件缺项而消失。
        for source in candidates:
            for key, item in source.get('items', {}).items():
                if item.get('result') == 'FAIL':
                    target = latest.setdefault('items', {}).setdefault(key, deepcopy(item))
                    target.update(result='FAIL', stop_reason=item.get('stop_reason'))
        old_boot = (previous.get('boot_id') or previous.get('hardware', {}).get('boot_id')
                    or remote.get('boot_id'))
        rebooted = bool(old_boot and current_boot and old_boot != current_boot)
        reason = ('已确认主控重启，原老化运行中断，不能恢复连续时长' if rebooted else
                  '已确认老化执行进程退出，原老化运行中断')
        evidence = {'kind': 'DEVICE_REBOOT' if rebooted else 'AGENT_EXITED',
                    'reason': reason, 'detected_at': utc_now(),
                    'previous_boot_id': old_boot, 'current_boot_id': current_boot,
                    'last_observed_at': latest.get('updated_at'),
                    'device_state': remote['state'], 'device_seq': remote.get('seq'),
                    'agent_alive': remote.get('agent_alive'), 'status_error': remote.get('status_error'),
                    'failure_time_known': False}
        return finish_interrupted(latest, evidence, plan.get('items'))

    def _finish_evidence(self, transport, run_id, status):
        # 先纠正云端的运行状态。文件损坏、丢失或补采失败不能使任务仍显示“老化中”。
        stored = self._store(run_id, status)
        if isinstance(stored, dict):
            status.clear()
            status.update(deepcopy(stored))
        try:
            self._ingest(transport, run_id)
            status.pop('evidence_error', None)
        except (StorageUnavailable, LocalLogUnavailable):
            raise
        except Exception as exc:
            status['evidence_error'] = '日志补采未完成：' + str(exc)
        try:
            transport.collect(run_id, self.local_logs)
            status.pop('attachment_error', None)
        except (StorageUnavailable, LocalLogUnavailable):
            raise
        except Exception as exc:
            status['attachment_error'] = str(exc)
        complete = self.local_logs.cursor(run_id)['seq'] == (status.get('seq') or 0)
        if not complete:
            status['evidence_error'] = '最终日志缺段，不能确认数据完整'
        if status.get('evidence_error') and status.get('result') != 'FAIL':
            status['result'] = 'INCOMPLETE'
        # 缺失的原状态可能有尚未上传的记录，即使已知 seq 为 0 也不能宣称完整。
        interruption = status.get('interruption', {})
        lost_status = (interruption.get('device_state') in ('MISSING', 'CORRUPT')
                       or bool(interruption.get('status_error')))
        if lost_status:
            status['evidence_error'] = '设备原状态丢失或损坏，仅保留已获取证据，无法确认日志完整'
        self._store(run_id, status)
        if complete and not lost_status and not status.get('evidence_error') and not status.get('attachment_error'):
            self.repository.mark_synced(run_id)

    def _run(self, run_id, device, plan, control, gate, recovering):
        transport = None
        stop_sent = False
        last_status = dict(control.get('initial_status') or {'state': 'PREFLIGHT', 'items': {}, 'result': None})
        launched = recovering and bool(last_status.get('launch_requested') or last_status.get('run_id'))
        acquired = False
        failures = 0
        current_boot = None
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
            if recovering and self.detaching.is_set():
                return
            if not acquired or (control['stop'].is_set() and not launched and not recovering):
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
                remote_status = None
                try:
                    if control['reconnect'].is_set():
                        with self._lock:
                            device = deepcopy(control['device'])
                            control['reconnect'].clear()
                        if transport:
                            transport.close()
                            transport = None
                        stop_sent = False
                    if recovering:
                        # 先看原运行是否已启动：已启动的恢复/停止完全不获取控制端租约。
                        current = self.repository.get(run_id)
                        if current['sync_complete']:
                            last_status = json.loads(current['status'])
                            return
                        last_status = json.loads(current['status'])
                        launched = bool(last_status.get('launch_requested') or last_status.get('run_id'))
                        if last_status.get('stop_pending'):
                            control['stop'].set()
                    if not launched:
                        if recovering and control['stop'].is_set():
                            self.repository.request_stop(run_id)
                        if not claimed or time.monotonic() - last_claim >= 60:
                            self.repository.claim(run_id, self.controller_id)
                            claimed = True
                            last_claim = time.monotonic()
                        if recovering:
                            # 获得准备阶段租约后再次读取，防止原工具刚好已提交启动。
                            current = self.repository.get(run_id)
                            last_status = json.loads(current['status'])
                            launched = bool(last_status.get('launch_requested') or last_status.get('run_id'))
                            if current['sync_complete']:
                                return
                            if last_status.get('stop_pending'):
                                control['stop'].set()
                    if recovering:
                        if not launched:
                            # 持有租约且确认未提交启动，恢复操作仅结束原准备阶段。
                            last_status.update(state='FINISHED', result='ABORTED', cleanup_confirmed=True,
                                error='原运行未提交启动，已结束准备阶段；可重新选择设备开始新任务')
                            self._store(run_id, last_status)
                            return
                        recovering = False
                    if control['stop'].is_set() and not last_status.get('stop_pending'):
                        last_status['stop_pending'] = True
                        self._store(run_id, last_status)
                    if transport is None:
                        transport = self.transport_factory(device)
                        def progress(message):
                            nonlocal last_claim
                            # 仅尚未启动的新任务持有准备租约；运行中任务的连接不排他。
                            if not launched and time.monotonic() - last_claim >= 60:
                                self.repository.claim(run_id, self.controller_id)
                                last_claim = time.monotonic()
                            if not launched:
                                last_status.update(state='DEPLOYING', environment_message=message)
                                self._store(run_id, last_status)
                        transport.progress = progress
                        transport.cancelled = lambda: self.detaching.is_set() or (not launched and control['stop'].is_set())
                        progress('正在连接设备并检查运行环境')
                        info = transport.probe(bootstrap=not launched)
                        if info['sn'] != device['sn']:
                            raise ValueError('重连设备 SN 不一致')
                        if last_status.get('hardware', {}).get('fingerprint') and info['fingerprint'] != last_status['hardware']['fingerprint']:
                            raise ValueError('重连设备指纹不一致')
                        current_boot = info.get('boot_id')
                        self.repository.update_ip(run_id, device['ip'])
                        if launched and control['stop'].is_set():
                            remote_status = transport.action('status', run_id)
                            if remote_status.get('run_id') != run_id:
                                raise ValueError('设备状态运行身份不匹配')
                        if not launched:
                            if info['chip'] == 'unknown' or info['chip'] != info['sn_chip']:
                                raise ValueError('真实硬件与 SN 机型无法核验一致，未部署老化依赖')
                            fingerprint = info['fingerprint']
                            last_status['hardware'] = info
                            info = transport.prepare(plan)
                            if info['sn'] != device['sn'] or info['fingerprint'] != fingerprint:
                                raise ValueError('环境部署后设备身份发生变化，未启动老化')
                            pending = self.repository.get(run_id)
                            if json.loads(pending['status']).get('stop_pending'):
                                control['stop'].set()
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
                            if control['stop'].is_set() or self.detaching.is_set():
                                last_status['launch_requested'] = False
                                raise DeploymentCancelled('启动提交前已取消，未启动老化负载')
                            launched = True
                            transport.launch(request)
                    status = remote_status if remote_status is not None else transport.action('status', run_id)
                    if status.get('run_id') != run_id:
                        raise ValueError('设备状态运行身份不匹配')
                    if status.get('config_hash') and status['config_hash'] != digest(plan):
                        raise ValueError('设备配置快照摘要与数据库不一致')
                    current_boot = status.get('current_boot_id') or current_boot
                    original_boot = last_status.get('boot_id') or last_status.get('hardware', {}).get('boot_id') or status.get('boot_id')
                    rebooted = bool(original_boot and current_boot and original_boot != current_boot)
                    terminal = status['state'] in ('FINISHED', 'BLOCKED')
                    if last_status.get('interruption') and last_status['state'] in ('FINISHED', 'BLOCKED'):
                        self._finish_evidence(transport, run_id, last_status)
                        return
                    if not terminal and (rebooted or status.get('agent_alive') is False):
                        last_status = self._interrupted(run_id, last_status, status, plan, current_boot)
                        self._finish_evidence(transport, run_id, last_status)
                        return
                    if status['state'] in ('MISSING', 'CORRUPT'):
                        raise ValueError('设备上未找到原运行或状态损坏，尚不能确认进程退出，不重复发起负载')
                    if (not terminal and original_boot and status.get('boot_id')
                            and status['boot_id'] != original_boot):
                        raise ValueError('状态 boot_id 不一致')
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
                    if last_status.get('hardware') and not status.get('hardware'):
                        status['hardware'] = last_status['hardware']
                    elif last_status.get('hardware', {}).get('environment_deployment'):
                        status['hardware']['environment_deployment'] = last_status['hardware']['environment_deployment']
                    validated_status = status
                    if (status.get('battery', {}).get('confirmed')
                            or status.get('items', {}).get('battery', {}).get('result') is not None
                            or status['state'] in ('FINISHED', 'BLOCKED')):
                        confirmation_poll_until = 0
                    last_status = status
                    if last_status['state'] in ('FINISHED', 'BLOCKED'):
                        self._finish_evidence(transport, run_id, last_status)
                        return
                    else:
                        # 运行状态先交给界面，日志补采较慢时不阻塞确认结果显示。
                        self._store(run_id, last_status)
                        if ingested_seq != status.get('seq'):
                            self._ingest(transport, run_id)
                            ingested_seq = status.get('seq')
                    failures = 0
                except RunOwned as exc:
                    # 仅原任务尚在准备阶段时存在排他启动者；已启动任务不会进入此分支。
                    recovering = True
                    claimed = False
                    if transport:
                        transport.close()
                        transport = None
                    self.notify(run_id, dict(last_status, state='RECONNECTING',
                        storage_error=str(exc), stop_pending=control['stop'].is_set()))
                    control['wake'].wait(5)
                    continue
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
                    if launched and self.detaching.is_set():
                        return  # 退出控制端，保留原设备运行状态供下次恢复。
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

    def device_busy(self, sn):
        with self._lock:
            return any(control['device']['sn'] == sn for control in self.active.values())


class LocalLogUnavailable(RuntimeError):
    """本地磁盘不可写时保留设备日志，重试成功前不宣称日志补采完成。"""
