"""按设备连接地址选择传输，VSM 地址可直接保存和恢复。"""
from .ssh_transport import SSHTransport


def is_vsm(device):
    return device.get('transport') == 'vsm' or device.get('ip', '').startswith('vsm:')


def create_transport(device, **kwargs):
    if is_vsm(device):
        from .vsm import VSMTransport
        return VSMTransport(device, **kwargs)
    return SSHTransport(device, **kwargs)
