import json
import os
from pathlib import Path
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


def fio_args(path, size, output):
    return ['fio', '--name=aging-verify', '--filename=' + str(path), '--rw=write',
            '--size=' + str(size), '--io_size=' + str(size), '--bs=1M', '--numjobs=1',
            '--iodepth=1', '--ioengine=sync', '--direct=1', '--verify=crc32c',
            '--do_verify=1', '--verify_fatal=1', '--verify_dump=1', '--refill_buffers=1',
            '--fallocate=none', '--end_fsync=1', '--output-format=json', '--output=' + str(output)]


def fio_result(path, expected):
    data = json.loads(Path(path).read_text())
    jobs = data['jobs']
    if len(jobs) != 1 or any(j.get('error') for j in jobs):
        raise RuntimeError('fio I/O 或数据校验失败')
    job = jobs[0]
    written, verified = job['write']['io_bytes'], job['read']['io_bytes']
    if written != expected or verified < expected:
        raise RuntimeError('fio 实际写入/校验字节数与计划不符')
    return {'written_bytes': written, 'verified_bytes': verified,
            'write_bw_kib_s': job['write'].get('bw'), 'read_bw_kib_s': job['read'].get('bw'),
            'write_latency_ns': job['write'].get('clat_ns'), 'read_latency_ns': job['read'].get('clat_ns')}
