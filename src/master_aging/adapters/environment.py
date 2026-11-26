"""检查设备环境，缺失依赖通过 exe 内嵌运行包部署，不访问设备软件源。"""
from .offline_runtime import TOOLS, select_bundle


class DeploymentCancelled(RuntimeError):
    pass


INSPECT = '''set -eu
printf 'system=%s\\n' "$(uname -s)"
printf 'arch=%s\\n' "$(uname -m)"
printf 'uid=%s\\n' "$(id -u)"
if [ -r /etc/os-release ]; then
  . /etc/os-release
  printf 'os_id=%s\\nversion_id=%s\\n' "${ID:-}" "${VERSION_ID:-}"
fi
printf 'glibc=%s\\n' "$(getconf GNU_LIBC_VERSION 2>/dev/null | sed 's/^glibc //' || true)"
for tool in python3 stress-ng fio findmnt i2cget; do
  printf '%s=%s\\n' "$tool" "$(command -v "$tool" || true)"
done
'''

PYTHON_CHECK = ('import sys, fcntl, termios, zlib, ctypes; '
                'assert sys.version_info >= (3, 8), "需要 Python 3.8+"; '
                'print(sys.version.split()[0])')


class EnvironmentDeployer:
    def __init__(self, command, progress=None, cancelled=None, install_runtime=None):
        self.command = command
        self.progress = progress or (lambda message: None)
        self.cancelled = cancelled or (lambda: False)
        self.install_runtime = install_runtime

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
                if tool == 'stress-ng':
                    help_text = self.command([info[tool], '--help'])
                    if any(option not in help_text for option in ('--vm-locked', '--vm-keep', '--vm-madvise')):
                        return False, '缺少 DDR 锁页和内存策略支持'
            elif tool == 'findmnt':
                version = self.command([info[tool], '--version']).strip()
                if 'util-linux' not in version:
                    return False, '需要支持 JSON 输出的 util-linux findmnt'
            else:
                # -V 只检查动态链接与版本，不访问 I2C 总线。
                version = self.command([info[tool], '-V']).strip()
        except (OSError, RuntimeError) as exc:
            return False, str(exc)
        return True, version

    def ensure(self, tools):
        tools = list(dict.fromkeys(tools))
        if any(tool not in TOOLS for tool in tools):
            raise ValueError('未知的老化运行工具')
        info = self.inspect()
        versions = {}
        result = {'arch': info['arch'], 'os_id': info.get('os_id'), 'version_id': info.get('version_id'),
                  'requested_tools': tools, 'installed_packages': [], 'source': 'existing'}
        if self.install_runtime is not None:
            bundle = select_bundle(info)
            self.check_cancelled()
            self.progress('启用程序内置离线依赖：' + '、'.join(tools))
            self.install_runtime(bundle)
            self.check_cancelled()
            info = self.inspect()
            for tool in tools:
                ready, detail = self.ready(tool, info)
                if not ready:
                    raise RuntimeError('离线部署后 %s 仍不可用：%s' % (tool, detail))
                versions[tool] = detail
            result.update(source='embedded', bundle=bundle['id'], bundle_sha256=bundle['sha256'])
        else:
            raise RuntimeError('离线部署通道未初始化，无法启用程序内置依赖')
        self.check_cancelled()
        result.update(tools={tool: info[tool] for tool in tools}, versions=versions)
        self.progress('设备依赖已就绪（无需设备联网）' if tools else '所选项目无需额外部署工具')
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
