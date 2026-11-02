import base64
import ipaddress
import json
import os
import platform
import socket
import struct
import subprocess
import threading
import time


DHCP_COOKIE = b'\x63\x82\x53\x63'
SERVER_ADDRESS = '172.31.250.1'
SERVER_PREFIX = 24
LEASE_SECONDS = 600
WINDOWS_IP_IFLIST = 28
WINDOWS_IP_ADD_IFLIST = 29
WINDOWS_IP_UNICAST_IF = 31


def parse_dhcp_options(packet):
    if len(packet) < 240 or packet[236:240] != DHCP_COOKIE:
        return None
    options = {}
    offset = 240
    while offset < len(packet):
        option = packet[offset]
        offset += 1
        if option == 255:
            return options
        if option == 0:
            continue
        if offset >= len(packet):
            return None
        length = packet[offset]
        offset += 1
        if offset + length > len(packet) or option in options:
            return None
        options[option] = packet[offset:offset + length]
        offset += length
    return None


class DhcpLeasePool:
    def __init__(self, server_ip=SERVER_ADDRESS, clock=time.monotonic):
        self.server_ip = server_ip
        self.network = ipaddress.IPv4Network(f'{server_ip}/{SERVER_PREFIX}', strict=False)
        self.clock = clock
        self.leases = {}
        self.declined = {}

    def _expire(self):
        now = self.clock()
        self.leases = {mac: lease for mac, lease in self.leases.items() if lease[1] > now}
        self.declined = {ip: until for ip, until in self.declined.items() if until > now}

    def _allocate(self, mac):
        self._expire()
        lease = self.leases.get(mac)
        if lease:
            return lease[0]
        occupied = {lease[0] for lease in self.leases.values()} | set(self.declined)
        for offset in range(100, 201):
            candidate = str(self.network[offset])
            if candidate not in occupied:
                self.leases[mac] = (candidate, self.clock() + LEASE_SECONDS)
                return candidate
        return None

    def _build_reply(self, request, assigned_ip, message_type):
        header = bytearray(request[:236])
        header[0] = 2
        header[10:12] = b'\x80\x00'
        header[12:16] = b'\0' * 4
        header[16:20] = socket.inet_aton(assigned_ip)
        header[20:24] = socket.inet_aton(self.server_ip)
        header[44:236] = b'\0' * 192
        response = bytes(header) + DHCP_COOKIE
        response += b'\x35\x01' + bytes([message_type])
        response += b'\x36\x04' + socket.inet_aton(self.server_ip)
        if message_type == 6:
            return (response + b'\xff').ljust(300, b'\0')
        response += b'\x01\x04\xff\xff\xff\x00'
        response += b'\x33\x04' + struct.pack('!I', LEASE_SECONDS)
        response += b'\x3a\x04' + struct.pack('!I', LEASE_SECONDS // 2)
        response += b'\x3b\x04' + struct.pack('!I', LEASE_SECONDS * 7 // 8)
        response += b'\xff'
        return response.ljust(300, b'\0')

    def handle(self, packet):
        if (len(packet) < 240 or packet[0:3] != b'\x01\x01\x06'
                or packet[3] != 0 or packet[24:28] != b'\0' * 4
                or packet[236:240] != DHCP_COOKIE):
            return None
        options = parse_dhcp_options(packet)
        if options is None or len(options.get(53, b'')) != 1:
            return None
        message_type = options[53][0]
        if message_type not in (1, 3, 4, 7):
            return None
        if options.get(54) not in (None, socket.inet_aton(self.server_ip)):
            return None
        mac = packet[28:34]
        if mac == b'\0' * 6 or mac[0] & 1:
            return None
        self._expire()
        if message_type in (4, 7):
            lease = self.leases.get(mac)
            address = options.get(50, packet[12:16])
            if lease and socket.inet_aton(lease[0]) == address:
                self.leases.pop(mac, None)
                if message_type == 4:
                    self.declined[lease[0]] = self.clock() + LEASE_SECONDS
            return None
        lease = self.leases.get(mac)
        if message_type == 1:
            assigned_ip = self._allocate(mac)
            return self._build_reply(packet, assigned_ip, 2) if assigned_ip else None
        requested = options.get(50, packet[12:16])
        if len(requested) != 4:
            return None
        assigned_ip = socket.inet_ntoa(requested)
        in_pool = assigned_ip in {str(self.network[offset]) for offset in range(100, 201)}
        occupied = any(other_mac != mac and value[0] == assigned_ip
                       for other_mac, value in self.leases.items())
        if not in_pool or occupied or assigned_ip in self.declined:
            return self._build_reply(packet, '0.0.0.0', 6)
        if lease is not None and lease[0] != assigned_ip:
            return self._build_reply(packet, '0.0.0.0', 6)
        self.leases[mac] = (assigned_ip, self.clock() + LEASE_SECONDS)
        return self._build_reply(packet, assigned_ip, 5)


class DirectDhcpServer(threading.Thread):
    def __init__(self, interface_index, server_ip=SERVER_ADDRESS):
        super().__init__(name='SpeakerDirectDhcp', daemon=True)
        self.interface_index = int(interface_index)
        self.server_ip = server_ip
        self.stop_event = threading.Event()
        self.error = None
        self.ready = threading.Event()
        self.pool = DhcpLeasePool(server_ip)
        self.socket = None

    def stop(self):
        self.stop_event.set()
        if self.socket:
            self.socket.close()

    def run(self):
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket = server
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            server.setsockopt(socket.IPPROTO_IP, WINDOWS_IP_IFLIST, 1)
            server.setsockopt(socket.IPPROTO_IP, WINDOWS_IP_ADD_IFLIST,
                              struct.pack('I', self.interface_index))
            server.setsockopt(socket.IPPROTO_IP, WINDOWS_IP_UNICAST_IF,
                              socket.htonl(self.interface_index))
            server.bind(('0.0.0.0', 67))
            server.settimeout(1)
            self.ready.set()
            while not self.stop_event.is_set():
                try:
                    packet, _address = server.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                response = self.pool.handle(packet)
                if response:
                    server.sendto(response, ('255.255.255.255', 68))
        except Exception as error:
            self.error = error
        finally:
            self.ready.set()
            if self.socket:
                self.socket.close()
                self.socket = None


class WindowsEthernetProvisioner:
    def __init__(self, interface_name, interface_index, app_dir):
        self.interface_name = interface_name
        self.interface_index = int(interface_index)
        self.backup_path = os.path.join(app_dir, 'logs', 'speaker_sn_network_backup.json')
        self.server = None
        self.changed = False
        self.adapter_guid = None

    @staticmethod
    def _ps_quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _run_powershell(script, elevate=False):
        if elevate and not _is_admin():
            raise PermissionError('请通过模式选择页重新打开工具，并允许 Windows 管理员提权。')
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        command = ['powershell.exe', '-NoProfile', '-NonInteractive',
                   '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding='utf-8-sig', errors='replace',
            timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        return result.stdout.strip()

    def _query_state(self):
        index = self.interface_index
        script = ("$ErrorActionPreference='Stop'; "
                  "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                  f"$interface=Get-NetIPInterface -InterfaceIndex {index} -AddressFamily IPv4; "
                  f"$adapter=Get-NetAdapter -InterfaceIndex {index}; "
                  f"$configuration=Get-NetIPConfiguration -InterfaceIndex {index}; "
                  f"$temporaryAddress=Get-NetIPAddress -InterfaceIndex {index} -AddressFamily IPv4 "
                  f"-ErrorAction SilentlyContinue | Where-Object {{$_.IPAddress -eq '{SERVER_ADDRESS}'}}; "
                  "$result=[pscustomobject]@{Dhcp=[string]$interface.Dhcp; "
                  "Guid=[string]$adapter.InterfaceGuid; Name=$adapter.Name; "
                  "HasGateway=($null -ne $configuration.IPv4DefaultGateway); "
                  "HasTemporaryAddress=($null -ne $temporaryAddress)}; "
                  "$result | ConvertTo-Json -Compress")
        result = self._run_powershell(script)
        return json.loads(result)

    def _write_backup(self):
        os.makedirs(os.path.dirname(self.backup_path), exist_ok=True)
        backup = {
            'interface_index': self.interface_index,
            'interface_name': self.interface_name,
            'server_ip': SERVER_ADDRESS,
            'dhcp_enabled': True,
            'adapter_guid': self.adapter_guid,
        }
        temporary_path = self.backup_path + '.tmp'
        with open(temporary_path, 'w', encoding='utf-8') as backup_file:
            json.dump(backup, backup_file)
            backup_file.flush()
            os.fsync(backup_file.fileno())
        os.replace(temporary_path, self.backup_path)

    def _configure_static(self):
        index = self.interface_index
        ip = SERVER_ADDRESS
        prefix = SERVER_PREFIX
        script = ("$ErrorActionPreference='Stop'; "
                  f"$adapter=Get-NetAdapter -InterfaceIndex {index}; "
                  f"if ([string]$adapter.InterfaceGuid -ne {self._ps_quote(self.adapter_guid)}) "
                  "{throw 'Ethernet adapter changed'}; "
                  "try { "
                  f"Set-NetIPInterface -InterfaceIndex {index} -AddressFamily IPv4 "
                  "-Dhcp Disabled; "
                  f"Get-NetIPAddress -InterfaceIndex {index} -AddressFamily IPv4 "
                  "| Where-Object {$_.IPAddress -like '169.254.*'} "
                  "| Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue; "
                  f"if (-not (Get-NetIPAddress -InterfaceIndex {index} -IPAddress '{ip}' "
                  "-ErrorAction SilentlyContinue)) { "
                  f"New-NetIPAddress -InterfaceIndex {index} -IPAddress '{ip}' "
                  f"-PrefixLength {prefix} -AddressFamily IPv4 | Out-Null }}; "
                  f"$rule='DoorCheck Speaker DHCP {index}'; "
                  "Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue; "
                  f"New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow "
                  f"-Protocol UDP -LocalPort 67 -InterfaceAlias {self._ps_quote(self.interface_name)} "
                  "-Profile Any | Out-Null; "
                  f"$mdns='DoorCheck Speaker mDNS {index}'; "
                  "Remove-NetFirewallRule -DisplayName $mdns -ErrorAction SilentlyContinue; "
                  "New-NetFirewallRule -DisplayName $mdns -Direction Inbound -Action Allow "
                  f"-Protocol UDP -LocalPort 5353 -InterfaceAlias {self._ps_quote(self.interface_name)} "
                  "-Profile Any | Out-Null "
                  "} catch { "
                  f"Remove-NetFirewallRule -DisplayName 'DoorCheck Speaker DHCP {index}' "
                  "-ErrorAction SilentlyContinue; "
                  f"Remove-NetFirewallRule -DisplayName 'DoorCheck Speaker mDNS {index}' "
                  "-ErrorAction SilentlyContinue; "
                  f"Get-NetIPAddress -InterfaceIndex {index} -AddressFamily IPv4 "
                  f"| Where-Object {{$_.IPAddress -eq '{ip}'}} "
                  "| Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue; "
                  f"Set-NetIPInterface -InterfaceIndex {index} -AddressFamily IPv4 -Dhcp Enabled; "
                  "throw $_ }")
        self._run_powershell(script, elevate=True)
        self.changed = True

    def start(self, addresses, all_addresses=()):
        usable = [ipaddress.IPv4Interface(address) for address in addresses
                  if not ipaddress.IPv4Interface(address).ip.is_link_local]
        if platform.system() != 'Windows':
            return False
        if usable:
            raise RuntimeError('RJ45 已有非链路本地 IPv4 地址，为避免修改办公网络，'
                               '请先将专用直连网卡设置为自动获取 IP，并断开交换机。')
        for address in all_addresses:
            existing = ipaddress.IPv4Interface(address).network
            if existing.overlaps(ipaddress.IPv4Network(f'{SERVER_ADDRESS}/{SERVER_PREFIX}', strict=False)):
                raise RuntimeError(f'直连 DHCP 网段与现有网络 {existing} 冲突，已停止配置。')
        state = self._query_state()
        if state.get('Dhcp') != 'Enabled' or state.get('HasGateway'):
            raise RuntimeError('RJ45 当前为静态地址且没有可通信的 IPv4，工具不会覆盖已有静态网络设置。')
        if state.get('Name') != self.interface_name or not state.get('Guid'):
            raise RuntimeError('物理以太网信息已变化，请重新打开工具。')
        self.adapter_guid = state['Guid']
        if os.path.exists(self.backup_path):
            raise RuntimeError('存在未恢复的网卡配置，请先恢复上次直连环境。')
        self._write_backup()
        self.changed = True
        try:
            self._configure_static()
            time.sleep(2)
            self.server = DirectDhcpServer(self.interface_index)
            self.server.start()
            if not self.server.ready.wait(3):
                raise RuntimeError('等待 DHCP 服务启动超时')
            if self.server.error:
                raise RuntimeError(f'启动 RJ45 DHCP 服务失败：{self.server.error}')
            return True
        except Exception:
            if self.server:
                self.server.stop()
                self.server.join(timeout=2)
                self.server = None
            try:
                state = self._query_state()
                if state.get('Dhcp') != 'Enabled' or state.get('HasTemporaryAddress'):
                    self.restore()
                else:
                    self.changed = False
                    if os.path.exists(self.backup_path):
                        os.remove(self.backup_path)
            except Exception:
                raise RuntimeError(f'直连配置失败且恢复未完成，请保留恢复记录 {self.backup_path}')
            raise

    def restore(self):
        if self.server:
            self.server.stop()
            self.server.join(timeout=2)
            self.server = None
        if not self.changed:
            return
        index = self.interface_index
        rule = f'DoorCheck Speaker DHCP {index}'
        script = ("$ErrorActionPreference='Stop'; "
                  f"$adapter=Get-NetAdapter -InterfaceIndex {index}; "
                  f"if ([string]$adapter.InterfaceGuid -ne {self._ps_quote(self.adapter_guid)}) "
                  "{throw 'Ethernet adapter changed; manual recovery required'}; "
                  f"Remove-NetFirewallRule -DisplayName {self._ps_quote(rule)} "
                  "-ErrorAction SilentlyContinue; "
                  f"Remove-NetFirewallRule -DisplayName 'DoorCheck Speaker mDNS {index}' "
                  "-ErrorAction SilentlyContinue; "
                  f"Get-NetIPAddress -InterfaceIndex {index} -AddressFamily IPv4 "
                  f"| Where-Object {{$_.IPAddress -eq '{SERVER_ADDRESS}'}} "
                  "| Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue; "
                  f"Set-NetIPInterface -InterfaceIndex {index} -AddressFamily IPv4 -Dhcp Enabled")
        try:
            self._run_powershell(script, elevate=True)
            self.changed = False
            if os.path.exists(self.backup_path):
                os.remove(self.backup_path)
        except Exception:
            raise RuntimeError(
                f'无法恢复 {self.interface_name} 的 DHCP 设置；恢复记录保留在 {self.backup_path}')

    @classmethod
    def recover(cls, app_dir):
        backup_path = os.path.join(app_dir, 'logs', 'speaker_sn_network_backup.json')
        if not os.path.exists(backup_path):
            return
        with open(backup_path, encoding='utf-8') as backup_file:
            backup = json.load(backup_file)
        provisioner = cls(backup['interface_name'], backup['interface_index'], app_dir)
        provisioner.adapter_guid = backup['adapter_guid']
        provisioner.changed = True
        provisioner.restore()


def acquire_network_lock(app_dir):
    import msvcrt

    lock_path = os.path.join(app_dir, 'logs', 'speaker_sn_network.lock')
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_file = open(lock_path, 'a+b')
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b'0')
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception:
        lock_file.close()
        raise RuntimeError('已有音箱 SN 比对工具运行，不能重复启动 DHCP 或恢复其他进程的配置。')
    return lock_file


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
