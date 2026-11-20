import json
import os
from pathlib import Path
import signal
import subprocess
import sys

# 独立 wrapper 在 exec 前设置 Linux parent-death 信号，避免 supervisor 意外退出后遗留负载。
# 不使用多线程进程中不安全的 preexec_fn。
PARENT_GUARD = '''import ctypes, os, signal, subprocess, sys
libc = ctypes.CDLL(None, use_errno=True)
signal.signal(signal.SIGUSR1, lambda signum, frame: os.killpg(0, signal.SIGKILL))
signal.signal(signal.SIGTERM, lambda signum, frame: None)
if libc.prctl(1, signal.SIGUSR1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_PDEATHSIG failed')
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_CHILD_SUBREAPER failed')
if os.getppid() != int(sys.argv[1]):
    os.killpg(0, signal.SIGKILL)
child = subprocess.Popen(sys.argv[2:])
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

    def stop(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        self.out.close()
        self.err.close()
        return self.process.returncode


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
