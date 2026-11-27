"""macOS 本地 VSM 交互通道；复用 SSHTransport 的部署和日志采集流程。"""
import base64
import errno
import hashlib
import json
import os
import platform
import re
import select
import shlex
import shutil
import subprocess
import time
import uuid
from types import SimpleNamespace

from .ssh_transport import SSHTransport
from ..infrastructure.config import vsm_settings


class VSMUnavailable(RuntimeError):
    pass


def check_platform():
    if platform.system() != 'Darwin':
        raise VSMUnavailable('手动添加主控仅支持 macOS；Windows 和其他系统请使用子网扫描。')


def local_command(settings=None):
    """Finder 启动没有终端 PATH；从用户登录 shell 获取已有别名。"""
    check_platform()
    settings = settings if settings is not None else vsm_settings()
    command = settings['command']
    if not command:
        script = ('if (( $+aliases[vsm] )); then '
                  'printf "\\0AGING_VSM\\0%s\\0" "$aliases[vsm]"; '
                  'elif (( $+commands[vsm] )); then '
                  'printf "\\0AGING_VSM\\0%s\\0" "$commands[vsm]"; fi')
        try:
            result = subprocess.run(['/bin/zsh', '-lic', script], capture_output=True,
                                    timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise VSMUnavailable('无法读取本地 VSM 配置，请检查登录 shell 或配置 vsm.yaml。') from None
        _, separator, value = result.stdout.partition(b'\0AGING_VSM\0')
        if separator:
            command = value.split(b'\0', 1)[0].decode('utf-8')
    try:
        args = shlex.split(command)
    except ValueError:
        raise VSMUnavailable('VSM 命令格式错误，请检查引号和登录参数。') from None
    if not args:
        raise VSMUnavailable('当前环境未配置 VSM。请先安装客户端并配置 vsm 命令或别名，'
                             '也可在 config/master_aging/vsm.yaml 填写完整命令。')
    executable = shutil.which(os.path.expanduser(args[0]))
    if not executable:
        raise VSMUnavailable('找不到可执行的本地 VSM 客户端，请检查安装路径和执行权限。')
    args[0] = executable
    return args


def check_environment():
    args = local_command()
    try:
        result = subprocess.run(args + ['--help'], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise VSMUnavailable('本地 VSM 无法运行，请检查客户端架构、执行权限和登录配置。') from None
    output = (result.stdout + result.stderr).decode('utf-8', 'replace').lower()
    if result.returncode or 'username or password is empty' in output or 'usage' not in output:
        raise VSMUnavailable('本地 VSM 环境检查失败，请先在终端确认 vsm <SN> 可用且登录参数完整。')
    return True


def manual_device(sn):
    # 兼容现有 varchar(45) 地址列；完整 SN 独立保存，长 SN 使用稳定地址标识。
    address = sn if len(sn) <= 41 else hashlib.sha256(sn.encode()).hexdigest()[:32]
    return {'ip': 'vsm:' + address, 'sn': sn, 'transport': 'vsm', 'identity_source': 'serial_id',
            'discovery_note': 'VSM 已连接 · root 权限已确认', 'model': 'MASTER'}


class VSMTransport(SSHTransport):
    def __init__(self, device, settings=None, progress=None):
        super().__init__(device, settings=settings, progress=progress)
        self.vsm_settings = vsm_settings()
        self.process = None
        self.master_fd = None

    def connect(self):
        self.close()
        args = local_command(self.vsm_settings)
        import pty
        import tty
        import fcntl
        import struct
        import termios
        master, slave = pty.openpty()
        tty.setraw(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        self.master_fd = master
        try:
            self.process = subprocess.Popen(args + [self.device['sn']], stdin=slave,
                                            stdout=slave, stderr=slave, close_fds=True)
        except OSError:
            self.close()
            raise VSMUnavailable('本地 VSM 客户端启动失败，请检查客户端架构和执行权限。') from None
        finally:
            os.close(slave)
        try:
            marker = 'AGING_READY_' + uuid.uuid4().hex
            deadline = time.monotonic() + self.vsm_settings['timeout']
            next_send = 0
            output = bytearray()
            while time.monotonic() < deadline:
                if time.monotonic() >= next_send:
                    script = ("stty -echo -icanon -onlcr rows 40 cols 120; PS1=''; PROMPT_COMMAND=''; "
                              "export TERM=dumb COLUMNS=120 LINES=40; printf '\\n%s\\n' %s\n")
                    self._write((script % ('%s', shlex.quote(marker))).encode(), deadline)
                    next_send = time.monotonic() + 2
                output.extend(self._read(deadline))
                if ('\n' + marker + '\n').encode() in output.replace(b'\r', b''):
                    break
                if len(output) > 65536:
                    del output[:-32768]
            else:
                raise VSMUnavailable('VSM 访问主控超时，请检查网络、VSM 账号权限和设备在线状态。')
            if self.command(['id', '-u']).strip() != '0':
                raise ValueError('VSM 已连接，但当前用户不是 root；无法执行主控老化，请配置 root 访问权限。')
            sn = self.command(['cat', '/etc/serial-id']).strip()
            if sn != self.device['sn']:
                raise ValueError('VSM 连接的主控 SN 与输入 SN 不一致，已拒绝添加或执行。')
            self.command(['sh', '-c', 'command -v base64 >/dev/null && command -v dd >/dev/null'])
            self.client = VSMFiles(self)
        except Exception:
            self.close()
            raise

    def _write(self, data, deadline):
        while data:
            if time.monotonic() >= deadline:
                raise TimeoutError('VSM 输入超时')
            _, ready, _ = select.select([], [self.master_fd], [], 0.2)
            if ready:
                sent = os.write(self.master_fd, data[:4096])
                data = data[sent:]

    def _read(self, deadline):
        if self.process.poll() is not None:
            raise VSMUnavailable('本地 VSM 已退出，无法访问主控，请检查网络、账号权限和设备在线状态。')
        ready, _, _ = select.select([self.master_fd], [], [], max(0, min(0.2, deadline - time.monotonic())))
        if not ready:
            return b''
        try:
            data = os.read(self.master_fd, 65536)
        except OSError:
            raise VSMUnavailable('VSM 与主控的连接已断开。') from None
        if not data:
            raise VSMUnavailable('VSM 与主控的连接已断开。')
        return data

    def command(self, args, timeout=25):
        if self.master_fd is None:
            raise VSMUnavailable('VSM 尚未连接')
        token = uuid.uuid4().hex
        start, end = 'AGING_START_' + token, 'AGING_END_' + token
        command = ' '.join(shlex.quote(str(a)) for a in args)
        if self.runtime:
            command = 'export PATH=%s/bin:$PATH; %s' % (shlex.quote(self.runtime), command)
        script = "printf '\\n%s\\n' %s; ( %s ) 2>&1; printf '\\n%s:%%s\\n' \"$?\"\n" % (
            '%s', shlex.quote(start), command, end)
        deadline = time.monotonic() + timeout
        data = bytearray()
        try:
            self._write(script.encode(), deadline)
            pattern = re.compile(rb'\n' + end.encode() + rb':(\d+)\n')
            while time.monotonic() < deadline:
                data.extend(self._read(deadline))
                if len(data) > 4 * 2**20:
                    raise VSMUnavailable('VSM 控制响应超过限制')
                normalized = bytes(data).replace(b'\r\n', b'\n')
                _, separator, body = normalized.partition(('\n' + start + '\n').encode())
                match = pattern.search(body) if separator else None
                if match:
                    output = body[:match.start()].decode('utf-8')
                    if int(match.group(1)):
                        raise RuntimeError(output.strip()[-1500:] or 'VSM 远端命令失败')
                    return output
            raise TimeoutError('VSM 设备控制命令超时，请检查设备连接。')
        except (OSError, VSMUnavailable, TimeoutError):
            self.close()
            raise

    def close(self):
        self.client = None
        process, self.process = self.process, None
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        if self.master_fd is not None:
            os.close(self.master_fd)
            self.master_fd = None


class VSMFiles:
    """既有上传/增量日志流程需要的 SFTP 子集，不另启设备服务。"""
    def __init__(self, transport):
        self.transport = transport

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def open_sftp(self):
        return self

    def get_channel(self):
        return self

    def settimeout(self, timeout):
        pass  # 每个控制命令自身有超时。

    def stat(self, path):
        result = self.transport.command(['sh', '-c',
            'if [ -f "$1" ]; then wc -c < "$1"; else printf MISSING; fi', 'aging-file', path])
        if result.strip() == 'MISSING':
            raise FileNotFoundError(errno.ENOENT, 'VSM 文件不存在', path)
        return SimpleNamespace(st_size=int(result.strip()))

    def open(self, path, mode):
        return VSMFile(self, path, mode)

    def rename(self, source, target):
        self.transport.command(['mv', source, target])

    def remove(self, path):
        self.transport.command(['rm', '-f', path])

    def listdir_attr(self, path):
        script = ('import os,json,sys; p=sys.argv[1]; '
                  'print(json.dumps([{ "filename": n, "st_mode": s.st_mode, "st_size": s.st_size } '
                  'for n in os.listdir(p) for s in [os.lstat(os.path.join(p,n))]]))')
        return [SimpleNamespace(**entry) for entry in json.loads(
            self.transport.command(['python3', '-c', script, path]))]


class VSMFile:
    CHUNK = 24 * 1024

    def __init__(self, files, path, mode):
        self.files, self.path, self.mode = files, path, mode
        self.offset = 0
        if mode == 'wb':
            files.transport.command(['sh', '-c', ': > "$1"', 'aging-file', path])
            self.size = 0
        else:
            self.size = files.stat(path).st_size

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def set_pipelined(self, value):
        pass

    def seek(self, offset):
        self.offset = offset

    def write(self, data):
        for index in range(0, len(data), self.CHUNK):
            if self.files.transport.cancelled():
                from .environment import DeploymentCancelled
                raise DeploymentCancelled('VSM 文件上传已取消，未启动老化负载')
            encoded = base64.b64encode(data[index:index + self.CHUNK]).decode('ascii')
            self.files.transport.command(['sh', '-c',
                'printf %s "$1" | base64 -d >> "$2"', 'aging-file', encoded, self.path], timeout=30)

    def read(self, size=-1):
        count = min(size if size >= 0 else self.size, max(0, self.size - self.offset), 512 * 1024)
        if not count:
            return b''
        block, skip = divmod(self.offset, 65536)
        blocks = (skip + count + 65535) // 65536
        script = ('set -e; dd if="$1" bs=65536 skip="$2" count="$3" 2>/dev/null | '
                  'dd bs=1 skip="$4" count="$5" 2>/dev/null | base64')
        encoded = self.files.transport.command(['sh', '-c', script, 'aging-file',
                                                self.path, block, blocks, skip, count], timeout=60)
        data = base64.b64decode(''.join(encoded.split()), validate=True)
        self.offset += len(data)
        return data
