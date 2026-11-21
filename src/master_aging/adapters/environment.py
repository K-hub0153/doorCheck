"""通过设备现有软件源补齐老化依赖，不改写软件源配置。"""


class DeploymentCancelled(RuntimeError):
    pass


INSPECT = '''set -eu
printf 'system=%s\n' "$(uname -s)"
printf 'arch=%s\n' "$(uname -m)"
printf 'uid=%s\n' "$(id -u)"
for tool in python3 stress-ng fio findmnt i2cget; do
  printf '%s=%s\n' "$tool" "$(command -v "$tool" || true)"
done
for manager in apt-get dnf yum apk opkg; do
  if command -v "$manager" >/dev/null 2>&1; then printf 'manager=%s\n' "$manager"; break; fi
done
'''

PYTHON_CHECK = ('import sys, fcntl, termios, zlib, ctypes; '
                'assert sys.version_info >= (3, 8), "需要 Python 3.8+"; '
                'print(sys.version.split()[0])')

# 包名交给目标设备的包管理器按本机架构解析。
PACKAGES = {'python3': 'python3', 'stress-ng': 'stress-ng', 'fio': 'fio',
            'findmnt': 'util-linux', 'i2cget': 'i2c-tools'}


class EnvironmentDeployer:
    def __init__(self, command, progress=None, cancelled=None):
        self.command = command
        self.progress = progress or (lambda message: None)
        self.cancelled = cancelled or (lambda: False)

    def check_cancelled(self):
        if self.cancelled():
            raise DeploymentCancelled('已取消环境部署，未启动老化负载')

    def inspect(self):
        self.check_cancelled()
        output = self.command(['sh', '-c', INSPECT])
        result = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if result.get('system') != 'Linux':
            raise ValueError('自动部署仅支持 Linux 主控设备')
        return result

    def ready(self, tool, info):
        if not info.get(tool):
            return False, '未安装'
        try:
            if tool == 'python3':
                version = self.command([info[tool], '-c', PYTHON_CHECK]).strip()
            elif tool in ('fio', 'stress-ng'):
                version = self.command([info[tool], '--version']).strip()
                if not version.startswith('fio-' if tool == 'fio' else 'stress-ng'):
                    return False, '命令不是老化所需的 ' + tool
            elif tool == 'findmnt':
                version = self.command([info[tool], '--version']).strip()
                if 'util-linux' not in version:
                    return False, '需要支持 JSON 输出的 util-linux findmnt'
            else:
                version = ''
        except RuntimeError as exc:
            return False, str(exc)
        return True, version

    def ensure(self, tools):
        info = self.inspect()
        versions, missing = {}, []
        for tool in tools:
            ready, detail = self.ready(tool, info)
            if ready:
                versions[tool] = detail
            else:
                missing.append(tool)
        result = {'arch': info['arch'], 'manager': info.get('manager'),
                  'requested_tools': list(tools), 'installed_packages': []}
        if missing:
            manager = info.get('manager')
            if not manager:
                raise RuntimeError('缺少 %s；设备没有可用的包管理器，无法使用现有软件源自动安装' % '、'.join(missing))
            prefix = []
            if info['uid'] != '0':
                try:
                    self.command(['sudo', '-n', 'true'])
                except RuntimeError as exc:
                    raise RuntimeError('自动安装依赖需要 root 或免密 sudo：' + str(exc))
                prefix = ['sudo', '-n']
            packages = list(dict.fromkeys(PACKAGES[tool] for tool in missing))
            if manager == 'opkg' and 'findmnt' in missing:
                packages = ['util-linux-findmnt' if p == 'util-linux' else p for p in packages]
            try:
                self.check_cancelled()
                if manager in ('apt-get', 'opkg'):
                    self.progress('正在更新设备现有软件源索引（%s）' % manager)
                    args = [manager, 'update']
                    if manager == 'apt-get':
                        args = ['env', 'DEBIAN_FRONTEND=noninteractive', manager,
                                '-o', 'Acquire::Retries=1', '-o', 'Acquire::http::Timeout=20',
                                '-o', 'Acquire::https::Timeout=20', 'update']
                    self.command(prefix + args, timeout=120)
                self.check_cancelled()
                self.progress('正在安装缺失依赖：' + '、'.join(packages))
                if manager == 'apt-get':
                    args = ['env', 'DEBIAN_FRONTEND=noninteractive', manager,
                            '-o', 'DPkg::Lock::Timeout=60', 'install', '-y', '--no-install-recommends']
                elif manager in ('dnf', 'yum'):
                    args = [manager, 'install', '-y']
                elif manager == 'apk':
                    args = [manager, 'add', '--no-cache']
                else:
                    args = [manager, 'install']
                self.command(prefix + args + packages, timeout=300)
            except DeploymentCancelled:
                raise
            except RuntimeError as exc:
                raise RuntimeError('使用设备现有软件源自动安装 %s 失败：%s' % ('、'.join(packages), exc))
            self.check_cancelled()
            self.progress('依赖安装完成，正在复检')
            info = self.inspect()
            for tool in tools:
                ready, detail = self.ready(tool, info)
                if not ready:
                    raise RuntimeError('自动安装后 %s 仍不可用：%s' % (tool, detail))
                versions[tool] = detail
            result['installed_packages'] = packages
        self.check_cancelled()
        result.update(tools={tool: info[tool] for tool in tools}, versions=versions)
        self.progress('设备依赖已就绪' if tools else '所选项目无需额外安装工具')
        return result


def workload_tools(plan):
    items = plan['items']
    tools = []
    if items['cpu']['enabled'] or items['ddr']['enabled']:
        tools.append('stress-ng')
    if items['storage']['enabled']:
        tools.extend(('fio', 'findmnt'))
    if items['battery']['enabled']:
        tools.append('i2cget')
    return tools
