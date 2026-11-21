from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import ipaddress
from pathlib import Path
import queue
import threading

from ..adapters.ssh_transport import SSHTransport
from ..adapters.environment import DeploymentCancelled
from ..domain.models import conclusion, digest, new_id, safe_sn, utc_now
from ..domain.policies import validate_plan
from ..infrastructure.report import export_report, export_batch
from ..infrastructure.config import runtime_dir


class RunService:
    """每个设备一条串行控制通道；跨设备并行，事件通过线程安全队列交给 GUI。"""
    def __init__(self, repository, root=None, transport_factory=SSHTransport):
        self.repository = repository
        self.root = Path(root) if root else runtime_dir()
        self.transport_factory = transport_factory
        self.events = queue.Queue(maxsize=2048)
        self.pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix='master-aging')
        self._lock = threading.Lock()
        self.active = {}
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
        ids = []
        for device in devices:
            run_id = new_id()
            directory = self.root / batch_id / safe_sn(device['sn']) / run_id
            directory.mkdir(parents=True)
            snapshot = deepcopy(plan)
            snapshot['software_version'] = '1.0.0'
            snapshot['config_hash'] = digest(plan)
            (directory / 'config_snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
            self.repository.create(batch_id, run_id, device, snapshot, directory)
            state = self._register(run_id, device)
            self.pool.submit(self._run, run_id, device, snapshot, directory, state, gate, False)
            ids.append(run_id)
        return ids

    def _register(self, run_id, device):
        with self._lock:
            if run_id in self.active:
                raise ValueError('该运行正在连接或执行')
            if any(s['device']['sn'] == device['sn'] for s in self.active.values()):
                raise ValueError('同一 SN 已有本地运行，先停止或恢复原运行')
            state = {'device': device, 'stop': threading.Event(), 'commands': queue.Queue(), 'future_done': threading.Event()}
            self.active[run_id] = state
            return state

    def resume(self, run_id, ip=None):
        row = self.repository.get(run_id)
        if not row or row['state'] in ('FINISHED', 'BLOCKED'):
            raise ValueError('运行已结束或不存在')
        device = {'sn': row['sn'], 'ip': str(ipaddress.IPv4Address(ip or row['ip']))}
        plan = json.loads(row['snapshot'])
        state = self._register(run_id, device)
        self.pool.submit(self._run, run_id, device, plan, Path(row['output_dir']), state, threading.Semaphore(1), True)

    def stop(self, run_id):
        with self._lock:
            if run_id in self.active:
                self.active[run_id]['stop'].set()

    def confirm(self, run_id, phase, token, operator):
        with self._lock:
            if run_id in self.active:
                self.active[run_id]['commands'].put({'phase': phase, 'token': token, 'operator': operator})

    def _store(self, run_id, status, directory):
        self.repository.save_status(run_id, status)
        (directory / 'summary.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
        with (directory / 'controller.log').open('a', encoding='utf-8') as stream:
            stream.write('%s %s result=%s seq=%s %s\n' % (utc_now(), status['state'], status.get('result'),
                status.get('seq'), status.get('error') or status.get('connection_error') or status.get('environment_message') or ''))
        self.notify(run_id, status)

    def _ingest(self, transport, run_id, directory):
        offset_file = directory / 'record_offset.json'
        try:
            offset = json.loads(offset_file.read_text()) if offset_file.exists() else 0
            if not isinstance(offset, int) or offset < 0:
                offset = 0
        except (OSError, ValueError):
            offset = 0
        while True:
            records, offset, more = transport.records(run_id, offset)
            if records:
                last = self.repository.last_seq(run_id)
                for record in records:
                    if record.get('run_id') != run_id:
                        raise ValueError('记录运行身份不匹配')
                    if record['seq'] > last:
                        if record['seq'] != last + 1:
                            raise ValueError('设备日志序号缺段')
                        last = record['seq']
                self.repository.ingest(run_id, records)
                # DB 提交先于游标。PC 在任意位置退出都可重读，主键负责幂等。
                offset_file.write_text(json.dumps(offset))
            if not more:
                break

    def _run(self, run_id, device, plan, directory, control, gate, recovering):
        transport = None
        launched = recovering
        stop_sent = False
        last_status = json.loads(self.repository.get(run_id)['status'])
        acquired = False
        failures = 0
        confirmed_reboot = False
        try:
            while not gate.acquire(timeout=0.2):
                if self.detaching.is_set() or control['stop'].is_set():
                    break
            else:
                acquired = True
            if not acquired or (control['stop'].is_set() and not launched):
                last_status.update(state='FINISHED', result='ABORTED', error='运行前取消')
                self._store(run_id, last_status, directory)
                return
            if self.detaching.is_set() and not launched:
                last_status.update(state='FINISHED', result='ABORTED', error='退出前尚未启动设备任务')
                self._store(run_id, last_status, directory)
                return
            while not self.detaching.is_set():
                validated_status = None
                try:
                    if transport is None:
                        transport = self.transport_factory(device)
                        def progress(message):
                            if not launched:
                                last_status.update(state='DEPLOYING', environment_message=message)
                                self._store(run_id, last_status, directory)
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
                            (directory / 'manifest.json').write_text(json.dumps({'run_id': run_id, 'device': info,
                                'created_at': utc_now(), 'config_hash': digest(plan), 'agent_sha256': transport.agent.rsplit('-', 1)[-1][:-4]},
                                ensure_ascii=False, indent=2), encoding='utf-8')
                            # 在提交启动之前持久化意图。命令响应丢失后只查询原 run_id。
                            last_status.update(state='READY', hardware=info, launch_requested=True,
                                               environment_message='环境准备完成，正在启动老化预检')
                            self._store(run_id, last_status, directory)
                            launched = True
                            transport.launch(request)
                    if control['stop'].is_set() and not stop_sent:
                        transport.action('stop', run_id)
                        stop_sent = True
                    while not control['commands'].empty():
                        confirmation = control['commands'].get_nowait()
                        transport.action('confirm', run_id, **confirmation)
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
                            self._store(run_id, last_status, directory)
                            return
                        raise ValueError('设备上未找到原运行，保留启动不确定性，不重复发起负载')
                    if status.get('config_hash') and status['config_hash'] != digest(plan):
                        raise ValueError('设备配置快照摘要与本地不一致')
                    if last_status.get('boot_id') and status.get('boot_id') != last_status['boot_id']:
                        raise ValueError('状态 boot_id 不一致')
                    validated_status = status
                    self._ingest(transport, run_id, directory)
                    last_status = status
                    if status.get('agent_alive') is False:
                        last_status.update(state='FINISHED', result='INCOMPLETE', error='设备重启或 supervisor 已退出，禁止重复启动')
                        for item in last_status.get('items', {}).values():
                            if item.get('result') is None:
                                item.update(result='INCOMPLETE', stop_reason='设备执行中断')
                        last_status['cleanup_confirmed'] = False
                        if conclusion(last_status.get('items', {})) == 'FAIL':
                            last_status['result'] = 'FAIL'
                    self._store(run_id, last_status, directory)
                    failures = 0
                    if last_status['state'] in ('FINISHED', 'BLOCKED'):
                        self._ingest(transport, run_id, directory)
                        try:
                            transport.collect(run_id, directory / 'device')
                        except Exception as exc:
                            last_status['attachment_error'] = str(exc)
                            self._store(run_id, last_status, directory)
                        if self.repository.last_seq(run_id) != status.get('seq', 0):
                            last_status.update(result='FAIL' if last_status.get('result') == 'FAIL' else 'INCOMPLETE',
                                               error='最终日志缺段，不能确认数据完整')
                            self._store(run_id, last_status, directory)
                        return
                except DeploymentCancelled as exc:
                    last_status.update(state='FINISHED', result='ABORTED', error=str(exc), cleanup_confirmed=True)
                    self._store(run_id, last_status, directory)
                    return
                except ValueError as exc:
                    if validated_status and validated_status['state'] in ('FINISHED', 'BLOCKED'):
                        last_status = validated_status
                        last_status.update(result='FAIL' if last_status.get('result') == 'FAIL' else 'INCOMPLETE', error=str(exc))
                        self._store(run_id, last_status, directory)
                        return
                    # 证据不一致时不能宣称远端已停止；保留可恢复的运行和明确结论。
                    last_status.update(state='RECONNECTING' if launched else 'BLOCKED',
                                       result='INCOMPLETE' if launched else 'BLOCKED',
                                       cleanup_confirmed=False, error=str(exc))
                    self._store(run_id, last_status, directory)
                    return
                except Exception as exc:
                    failures += 1
                    if not launched:
                        last_status.update(state='BLOCKED', result='BLOCKED', error='连接/部署失败: ' + str(exc))
                        self._store(run_id, last_status, directory)
                        return
                    last_status.update(state='RECONNECTING', connection_error=str(exc), stop_pending=control['stop'].is_set())
                    self._store(run_id, last_status, directory)
                    if transport:
                        transport.close()
                        transport = None
                self.detaching.wait(min(10, 2 + failures))
        except Exception as exc:
            last_status.update(state='RECONNECTING' if launched else 'BLOCKED', result=None if launched else 'BLOCKED', error=str(exc))
            self._store(run_id, last_status, directory)
        finally:
            if transport:
                transport.close()
            if acquired:
                gate.release()
            try:
                export_report(self.repository, run_id)
                export_batch(self.repository, self.repository.get(run_id)['batch_id'])
            except Exception as exc:
                last_status['report_error'] = str(exc)
                self._store(run_id, last_status, directory)
            with self._lock:
                self.active.pop(run_id, None)
            control['future_done'].set()
            self.notify(run_id, last_status)

    def shutdown(self, detach=False):
        if detach:
            self.detaching.set()
        else:
            with self._lock:
                for control in self.active.values():
                    control['stop'].set()
        if detach and not self.busy():
            self.pool.shutdown(wait=False)

    def busy(self):
        with self._lock:
            return bool(self.active)
