import hashlib
import io
import json
from pathlib import Path
import shlex
import time
import zipfile
import errno

import paramiko
from .environment import DeploymentCancelled, EnvironmentDeployer, workload_tools
from .offline_runtime import archive_path
from ..infrastructure.config import ssh_settings

REMOTE_ROOT = '/var/tmp/master-aging'


def agent_bundle():
    """按实际 Python 源码构建标准库 zipapp；冻结程序使用随包发布的同份资源。"""
    import sys
    if getattr(sys, 'frozen', False):
        return (Path(sys._MEIPASS) / 'master_aging_agent.pyz').read_bytes()
    root = Path(__file__).resolve().parents[1]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        def write(name, data):
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
        write('__main__.py', 'from src.master_aging.device_agent.supervisor import main\nmain()\n')
        for namespace in ('src', 'src/master_aging'):
            write(namespace + '/__init__.py', '')
        for folder in ('domain', 'device_agent'):
            for path in sorted((root / folder).glob('*.py')):
                write('src/master_aging/' + folder + '/' + path.name, path.read_bytes())
        write('lte_decode.py', (root.parent / 'master_control/core/lte_signal.py').read_bytes())
    return buffer.getvalue()


class SSHTransport:
    """只执行有超时的短控制命令，SFTP 增量获取日志；不调用原产测初始化。"""
    def __init__(self, device, settings=None, progress=None):
        self.device = device
        self.settings = settings if settings is not None else ssh_settings()
        self.progress = progress or (lambda message: None)
        self.cancelled = lambda: False
        self.deployment = []
        self.client = None
        self.agent = None
        self.runtime = None

    def report(self, message):
        self.progress(message)

    def connect(self):
        self.close()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            timeout = self.settings.get('timeout', 10)
            client.connect(hostname=self.device['ip'], port=self.device.get('port', 22),
                           username=self.settings.get('username', 'root'), password=self.settings.get('password'),
                           timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                           look_for_keys=False, allow_agent=False)
            client.get_transport().set_keepalive(15)
            self.client = client
        except Exception:
            client.close()
            raise

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    def command(self, args, timeout=25):
        command = ' '.join(shlex.quote(str(a)) for a in args)
        if self.runtime:
            command = 'export PATH=%s/bin:$PATH; %s' % (shlex.quote(self.runtime), command)
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        channel = stdout.channel
        data, errors = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            while True:
                if channel.recv_ready():
                    data.extend(channel.recv(65536))
                if channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(65536))
                if len(data) + len(errors) > 4 * 2**20:
                    raise RuntimeError('远端控制响应超过限制')
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('设备控制命令超时')
                time.sleep(0.01)
            code = channel.recv_exit_status()
            if code:
                detail = (errors or data).decode('utf-8', errors='replace')[-1500:]
                raise RuntimeError(detail.strip() or '远端控制命令失败（退出码 %s）' % code)
            return data.decode('utf-8')
        finally:
            stdin.close()
            channel.close()

    def environment(self):
        return EnvironmentDeployer(self.command, self.report, self.cancelled, self.install_runtime)

    def install_runtime(self, bundle):
        """SFTP 上传内嵌运行包；设备摘要校验、原子解包后才启用。"""
        path = archive_path(bundle)
        signature = bundle['sha256']
        remote = REMOTE_ROOT + '/runtime-' + signature
        marker = remote + '/.complete'
        if self.runtime == remote:
            return
        self.command(['mkdir', '-p', REMOTE_ROOT])
        try:
            complete = self.command(['cat', marker]).strip() == signature
        except RuntimeError:
            complete = False
        if not complete:
            import uuid
            temporary = remote + '.' + uuid.uuid4().hex
            archive = temporary + '.tar.gz'
            self.report('上传离线运行环境（%.1f MiB），设备无需下载' % (path.stat().st_size / 2**20))
            with self.client.open_sftp() as sftp:
                sftp.get_channel().settimeout(30)
                try:
                    with path.open('rb') as stream, sftp.open(archive, 'wb') as output:
                        output.set_pipelined(True)
                        while True:
                            if self.cancelled():
                                raise DeploymentCancelled('离线环境上传已取消，未启动老化负载')
                            chunk = stream.read(256 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                except Exception:
                    try:
                        sftp.remove(archive)
                    except OSError:
                        pass
                    raise
            # 不执行安装脚本、不替换系统库；仅写入本工具专属目录。
            script = '''set -eu
archive=$1; staging=$2; target=$3; expected=$4
trap 'rm -f -- "$archive"; rm -rf -- "$staging"' EXIT
actual=$(sha256sum "$archive"); actual=${actual%% *}
[ "$actual" = "$expected" ] || { echo '离线包摘要校验失败' >&2; exit 1; }
mkdir "$staging"
tar -xzf "$archive" -C "$staging"
printf '%s' "$expected" > "$staging/.complete"
if [ -d "$target" ]; then
  [ "$(cat "$target/.complete" 2>/dev/null)" = "$expected" ] || exit 1
else
  mv -T "$staging" "$target"
fi
'''
            self.command(['sh', '-c', script, 'aging-runtime', archive, temporary, remote, signature], timeout=120)
        self.runtime = remote
        self.report('内嵌离线运行环境已就绪：' + bundle['id'])

    def prepare(self, plan):
        self.report('检查所选老化项目的运行环境')
        self.deployment.append(self.environment().ensure(workload_tools(plan)))
        info = self.action('probe')
        info['environment_deployment'] = list(self.deployment)
        return info

    def deploy(self):
        self.report('正在部署老化执行器')
        bundle = agent_bundle()
        signature = hashlib.sha256(bundle).hexdigest()
        self.agent = REMOTE_ROOT + '/agent-' + signature + '.pyz'
        self.command(['mkdir', '-p', REMOTE_ROOT])
        with self.client.open_sftp() as sftp:
            sftp.get_channel().settimeout(15)
            try:
                sftp.stat(self.agent)
            except OSError:
                temporary = self.agent + '.' + __import__('uuid').uuid4().hex + '.tmp'
                with sftp.open(temporary, 'wb') as stream:
                    stream.write(bundle)
                sftp.rename(temporary, self.agent)
        # 不依赖 sha256sum 工具；每次启动前校验上传内容摘要。
        script = 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())'
        if self.command(['python3', '-c', script, self.agent]).strip() != signature:
            raise RuntimeError('设备端执行器摘要校验失败')
        return signature

    def action(self, action, run_id=None, **kwargs):
        args = ['python3', self.agent, action]
        if run_id:
            args += ['--run-id', run_id]
        for key, value in kwargs.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return json.loads(self.command(args))

    def probe(self, bootstrap=True):
        self.connect()
        # 先核验身份，再启用内嵌依赖；恢复时也复用同一版本运行环境。
        sn = self.command(['cat', '/etc/serial-id']).strip()
        expected_sn = (self.device.get('sn')
                       if self.device.get('identity_source') not in ('hostname', 'unverified') else None)
        if not sn or (expected_sn and sn != expected_sn):
            raise ValueError('设备 SN 与所选身份不一致，未部署环境')
        self.report('启用设备离线 Python 运行环境')
        self.deployment.append(self.environment().ensure(['python3']))
        self.deploy()
        return self.action('probe')

    def launch(self, request):
        remote = REMOTE_ROOT + '/request-' + request['run_id'] + '.json'
        with self.client.open_sftp() as sftp:
            sftp.get_channel().settimeout(15)
            with sftp.open(remote, 'wb') as stream:
                stream.write(json.dumps(request, ensure_ascii=False).encode('utf-8'))
        return self.action('launch', request['run_id'], request=remote)

    def records(self, run_id, offset):
        path = REMOTE_ROOT + '/' + run_id + '/records.jsonl'
        with self.client.open_sftp() as sftp:
            sftp.get_channel().settimeout(15)
            try:
                stream = sftp.open(path, 'rb')
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return [], offset, False
                raise
            with stream:
                stream.seek(offset)
                chunk = stream.read(512 * 1024)
            end = chunk.rfind(b'\n') + 1
            if not end:
                return [], offset, False
            records = [json.loads(line) for line in chunk[:end].decode('utf-8').splitlines()]
            return records, offset + end, len(chunk) == 512 * 1024

    def collect(self, run_id, local_logs):
        remote = REMOTE_ROOT + '/' + run_id
        with self.client.open_sftp() as sftp:
            sftp.get_channel().settimeout(15)
            for attr in sftp.listdir_attr(remote):
                import stat
                if not stat.S_ISREG(attr.st_mode) or '/' in attr.filename:
                    continue
                if attr.filename in ('records.jsonl', 'execution.log'):
                    continue  # 原始采样与可读执行日志已在采集时增量落到 PC。
                if attr.st_size > 128 * 2**20:
                    raise ValueError('设备附件超过 128 MiB：' + attr.filename)
                with sftp.open(remote + '/' + attr.filename, 'rb') as stream:
                    local_logs.save_artifact(run_id, attr.filename, stream)
