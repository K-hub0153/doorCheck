import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

# 独立 wrapper 在 exec 前设置 Linux parent-death 信号，避免 supervisor 意外退出后遗留负载。
# 不使用多线程进程中不安全的 preexec_fn。
PARENT_GUARD = '''import ctypes, os, signal, subprocess, sys
libc = ctypes.CDLL(None, use_errno=True)
signal.signal(signal.SIGUSR1, lambda signum, frame: os.killpg(0, signal.SIGKILL))
child = None
stopping = False
def stop(signum, frame):
    global stopping
    stopping = True
    if child is not None:
        try:
            child.terminate()
        except ProcessLookupError:
            pass
signal.signal(signal.SIGTERM, stop)
if libc.prctl(1, signal.SIGUSR1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_PDEATHSIG failed')
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_CHILD_SUBREAPER failed')
if os.getppid() != int(sys.argv[1]):
    os.killpg(0, signal.SIGKILL)
child = subprocess.Popen(sys.argv[2:])
if stopping:
    stop(signal.SIGTERM, None)
result = child.wait()
while True:
    try:
        os.waitpid(-1, 0)
    except ChildProcessError:
        break
sys.exit(result if result >= 0 else 128 - result)
'''


def vm_regions(smaps, allocation):
    """定位 stress-ng 的匿名测试映射，排除堆栈、工具控制页与文件映射。"""
    regions, current = [], None
    for line in smaps.splitlines():
        if re.match(r'^[0-9a-f]+-[0-9a-f]+ ', line):
            if current:
                regions.append(current)
            parts = line.split(None, 5)
            start, end = (int(value, 16) for value in parts[0].split('-'))
            current = {'address': parts[0], 'mapped_bytes': end - start, 'permissions': parts[1],
                       'anonymous': parts[4] == '0' and (len(parts) == 5 or parts[5].startswith('[anon:')),
                       'rss_bytes': 0, 'swap_bytes': 0, 'locked_bytes': 0}
        elif current and ':' in line:
            name, value = line.split(':', 1)
            key = {'Rss': 'rss_bytes', 'Swap': 'swap_bytes', 'Locked': 'locked_bytes'}.get(name)
            if key:
                current[key] = int(value.split()[0]) * 1024
    if current:
        regions.append(current)
    # 内核可能将相邻匿名区合并；锁定量另行核验，不能仅凭进程总 RSS 推断。
    page_size = os.sysconf('SC_PAGE_SIZE')
    expected = allocation // page_size * page_size
    return [region for region in regions if region['anonymous'] and region['permissions'].startswith('rw-p')
            and region['mapped_bytes'] >= expected]


class Process:
    """只持有本次创建的进程组；停止时回收后才允许最终结论。"""
    def __init__(self, args, directory, name):
        self.out = (Path(directory) / (name + '.stdout.log')).open('ab', buffering=0)
        self.err = (Path(directory) / (name + '.stderr.log')).open('ab', buffering=0)
        try:
            if not sys.platform.startswith('linux'):
                raise RuntimeError('设备端负载执行仅支持 Linux')
            guarded = [sys.executable, '-c', PARENT_GUARD, str(os.getpid())] + args
            self.process = subprocess.Popen(guarded, stdout=self.out, stderr=self.err,
                                            stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(directory))
        except Exception:
            self.out.close()
            self.err.close()
            raise
        self.args = args

    def poll(self):
        return self.process.poll()

    def memory_snapshot(self, vm_bytes=None):
        """读取本进程组的内存证据，区分控制进程与实际执行校验的 worker。"""
        proc = Path('/proc')
        if not proc.is_dir():
            raise OSError('无法读取 /proc 进程内存信息')
        members = []
        for path in proc.glob('[0-9]*/stat'):
            try:
                fields = path.read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) != self.process.pid or fields[0] in ('Z', 'X'):
                    continue
                status = dict(line.split(':', 1) for line in
                              (path.parent / 'status').read_text().splitlines() if ':' in line)
                if 'VmSwap' not in status or 'VmRSS' not in status:
                    # worker 退出过程中可能已释放地址空间，不计入运行工作集。
                    current = path.read_text().rsplit(')', 1)[1].split()
                    if current[0] in ('Z', 'X'):
                        continue
                    raise ValueError('进程 %s 缺少 VmSwap/VmRSS' % path.parent.name)
                member = {'pid': int(path.parent.name), 'ppid': int(fields[1]),
                                'name': status.get('Name', '').strip(),
                                'rss_bytes': int(status['VmRSS'].split()[0]) * 1024,
                                'swap_bytes': int(status['VmSwap'].split()[0]) * 1024}
                if (vm_bytes is not None and member['pid'] != self.process.pid
                        and (member['ppid'] != self.process.pid or member['name'].startswith('stress-ng-vm'))):
                    member['vm_regions'] = vm_regions((path.parent / 'smaps').read_text(), vm_bytes)
                members.append(member)
            except (FileNotFoundError, ProcessLookupError):
                continue  # 采样时自然退出的进程。
        regions = [region for member in members for region in member.get('vm_regions', [])]
        return {'pgid': self.process.pid, 'processes': members,
                'rss_bytes': sum(p['rss_bytes'] for p in members),
                'swap_bytes': sum(p['swap_bytes'] for p in members),
                'vm_regions': regions,
                'vm_swap_bytes': sum(region['swap_bytes'] for region in regions),
                'vm_locked_bytes': sum(region['locked_bytes'] for region in regions),
                'vm_rss_bytes': sum(region['rss_bytes'] for region in regions)}

    def request_stop(self):
        # 先由 wrapper 通知负载主进程正常收尾，避免直接终止 stress-ng worker。
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    def group_running(self):
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # 进程组存在但当前用户无权探测时，不能把它当成已停止。
            return True
        # SIGKILL 后孤儿可能短暂成为僵尸；僵尸不再执行负载，不能据此判残留压测。
        proc = Path('/proc')
        if proc.is_dir():
            for path in proc.glob('[0-9]*/stat'):
                try:
                    fields = path.read_text().rsplit(')', 1)[1].split()
                    if int(fields[2]) == self.process.pid and fields[0] not in ('Z', 'X'):
                        return True
                except FileNotFoundError:
                    continue
            return False
        return True

    def stop(self):
        try:
            self.request_stop()
            deadline = time.monotonic() + 5
            while self.group_running() and time.monotonic() < deadline:
                self.process.poll()
                time.sleep(0.05)
            # 即使 wrapper 已退出，也要回收同组残留的 worker/fio 子进程。
            if self.group_running():
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while self.group_running():
                if time.monotonic() >= deadline:
                    raise RuntimeError('压测进程组 %s 仍有运行进程，停止尚未确认' % self.process.pid)
                time.sleep(0.05)
            return self.process.returncode
        finally:
            self.out.close()
            self.err.close()


def _recovery_processes():
    proc = Path('/proc')
    if not proc.is_dir():
        raise RuntimeError('无法读取 /proc，不能核验历史负载')
    members = []
    for entry in proc.glob('[0-9]*/stat'):
        try:
            fields = entry.read_text().rsplit(')', 1)[1].split()
            if fields[0] in ('Z', 'X'):
                continue
            cwd = os.readlink(str(entry.parent / 'cwd'))
            if cwd.endswith(' (deleted)'):
                cwd = cwd[:-10]
            members.append({'pid': int(entry.parent.name), 'pgid': int(fields[2]), 'session': int(fields[3]),
                            'start': fields[19],
                            'argv': (entry.parent / 'cmdline').read_bytes().split(b'\x00'),
                            'cwd': cwd})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise RuntimeError('权限不足，无法核验设备进程残留') from exc
    return members


def recover_run_processes(directories):
    """按工作目录与负载参数共同识别旧负载，禁止只凭历史 PID 或名称杀进程。"""
    known = {str(path.resolve()): path.name for path in directories}
    initial = _recovery_processes()
    groups = {}
    for member in initial:
        run_id = known.get(member['cwd'])
        if not run_id:
            continue
        argv = member['argv']
        executable = Path(os.fsdecode(argv[0])).name if argv else ''
        wrapper = PARENT_GUARD.encode() in argv
        fio = executable == 'fio' and any(
            arg.startswith(b'--filename=') and arg.endswith(('.master-aging-' + run_id + '.bin').encode())
            for arg in argv)
        if not (wrapper or fio or executable == 'stress-ng' or executable.startswith('stress-ng-')):
            continue
        pgid = member['pgid']
        if member['session'] != pgid:
            # 本工具用 start_new_session=True 创建负载，普通 shell 下的同名工具不属于本次。
            continue
        if pgid <= 1 or pgid == os.getpgrp():
            raise RuntimeError('旧负载进程组身份无效，未发送停止信号')
        groups.setdefault(pgid, {'run_id': run_id, 'members': []})['members'].append(member)
    evidence = {}

    def verified_live_groups():
        snapshot = _recovery_processes()
        live = {}
        for pgid, group in groups.items():
            members = [p for p in snapshot if p['pgid'] == pgid]
            if not members:
                continue
            cookies = {(p['pid'], p['start']) for p in group['members']}
            if not any((p['pid'], p['start']) in cookies for p in members):
                raise RuntimeError('旧负载 PID 已变化，未发送停止信号')
            directory = next(path for path, run_id in known.items() if run_id == group['run_id'])
            if any(p['cwd'] != directory or p['session'] != pgid for p in members):
                raise RuntimeError('旧负载进程组成员身份不一致，未发送停止信号')
            group['members'] = members
            live[pgid] = members
        return live

    # 先向全部已核验进程组发送 TERM，再等待、强制清理与核验。
    for pgid, group in groups.items():
        if pgid not in verified_live_groups():
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    pending = set(groups)
    while pending:
        live = set(verified_live_groups())
        pending.intersection_update(live)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    for pgid in pending:
        if pgid not in verified_live_groups():
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while pending:
        pending.intersection_update(verified_live_groups())
        if pending and time.monotonic() >= deadline:
            raise RuntimeError('自动回收旧负载失败，仍有运行进程组: %s' % sorted(pending))
        if pending:
            time.sleep(0.05)
    for pgid, group in groups.items():
        evidence.setdefault(group['run_id'], []).append({'pgid': pgid, 'confirmed': True, 'utc': time.time()})
    return evidence


def fio_args(path, size, output):
    return ['fio', '--name=aging-verify', '--filename=' + str(path), '--rw=write',
            '--size=' + str(size), '--io_size=' + str(size), '--bs=1M', '--numjobs=1',
            '--iodepth=1', '--ioengine=sync', '--direct=1', '--verify=crc32c',
            '--do_verify=1', '--verify_fatal=1', '--verify_dump=1', '--refill_buffers=1',
            '--fallocate=none', '--end_fsync=1', '--output-format=json', '--output=' + str(output)]


def fio_evidence(path, expected):
    """保留到时/停止后的部分结果；有字节数不代表本轮校验完成。"""
    evidence = {'expected_bytes': expected, 'complete': False}
    try:
        data = json.loads(Path(path).read_text())
        jobs = data['jobs']
        if len(jobs) != 1:
            raise ValueError('fio job 数量不符合预期')
        job = jobs[0]
        written = job['write']['io_bytes']
        verified = job['read']['io_bytes']
        error = job['error']
        if any(type(value) is not int or value < 0 for value in (written, verified, error)):
            raise ValueError('fio 字节数或错误码无效')
        evidence.update(written_bytes=written, verified_bytes=verified, fio_error=error)
        evidence['complete'] = error == 0 and written == expected and verified >= expected
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        evidence['evidence_error'] = str(exc)
    return evidence


def process_diagnostics(directory, name):
    """有界读取本次负载日志，保留工具输出的错误，避免猜测退出根因。"""
    messages = []
    for suffix in ('stderr', 'stdout'):
        path = Path(directory) / ('%s.%s.log' % (name, suffix))
        try:
            with path.open('rb') as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 8192))
                lines = stream.read().decode('utf-8', errors='replace').splitlines()
        except OSError:
            continue
        errors = [line for line in lines if re.search(r'error|fail|errno|fatal|cannot|no space', line, re.I)]
        selected = errors[-3:] if errors else lines[-3:] if suffix == 'stderr' else []
        if selected:
            messages.append(path.name + ': ' + '；'.join(selected)[-1200:])
    return '；'.join(messages)


def fio_result(path, expected):
    data = json.loads(Path(path).read_text())
    jobs = data['jobs']
    if len(jobs) != 1:
        raise RuntimeError('fio job 数量 %s，要求 1' % len(jobs))
    job = jobs[0]
    if job.get('error'):
        error = job['error']
        raise RuntimeError('fio I/O 或数据校验失败: %s（errno=%s）' % (os.strerror(error), error))
    written, verified = job['write']['io_bytes'], job['read']['io_bytes']
    if written != expected or verified < expected:
        raise RuntimeError('fio 实际写入 %s 字节、读取校验 %s 字节；要求写入 %s 字节、读取校验 ≥ %s 字节' %
                           (written, verified, expected, expected))
    return {'written_bytes': written, 'verified_bytes': verified,
            'write_bw_kib_s': job['write'].get('bw'), 'read_bw_kib_s': job['read'].get('bw'),
            'write_latency_ns': job['write'].get('clat_ns'), 'read_latency_ns': job['read'].get('clat_ns')}
