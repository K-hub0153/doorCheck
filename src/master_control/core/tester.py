import json
import re
import subprocess
import time
import os
from ..utils.paths import config_dir

from typing import List

from src.master_control.core.ssh_client import SSHClient
from src.master_control.core.test_result import DeviceTestResults, TestResult
from src.master_control.core.chip_config import ChipManager, ChipConfig
from src.master_control.core import lte_signal
from src.master_control.core import poe_net
from src.master_control.core import usb_link
from src.master_control.core import disk_info
from src.master_control.core import battery_profile
from src.master_control.utils.logger import logger
from src.master_control.utils.config import config
from src.master_control.utils.mac_allocator import MACAllocator


class DeviceTester:

    def __init__(self, host: str, sn: str):
        self.host = host
        self.sn = sn
        self.ssh = None
        self.results = DeviceTestResults(sn=sn, ip=host)
        # 识别芯片类型并加载配置
        self.chip_config: ChipConfig = ChipManager.get_chip_config(sn)

    def connect(self) -> bool:
        try:
            self.ssh = SSHClient(self.host)
            return self.ssh.connect()
        except Exception as e:
            logger.error(f"连接设备失败 ({self.host}): {e}")
            return False

    def init_device(self) -> tuple:
        """连接后初始化：启动 wifi-service 并切换到产测模式"""
        results = []

        # 1. 启动 wifi-service（仅 hisi）
        if self.chip_config.chip_type == 'hisi':
            logger.info(f"[{self.sn}] 启动 wifi-service...")
            success, stdout, stderr = self.ssh.execute_command("systemctl stop wifi-service", timeout=15)
            if success:
                results.append("wifi-service 停止成功")
                logger.info(f"[{self.sn}] wifi-service 停止成功")
            else:
                results.append(f"wifi-service 停止失败: {stderr}")
                logger.warning(f"[{self.sn}] wifi-service 停止失败: {stderr}")

        # 2. 切换产测模式：hisi 和 RK3576 使用统一的 ws73-modules 命令
        if self.chip_config.chip_type in ['hisi', 'wdse']:
            logger.info(f"[{self.sn}] {self.chip_config.chip_type} 芯片，切换产测模式...")
            success, stdout, stderr = self.ssh.execute_command("ws73-modules switch factory", timeout=120)
            logger.info(f"[{self.sn}] ws73-modules switch factory 结果: success={success}, stdout={stdout}, stderr={stderr}")
            
            if success:
                results.append("已切换到产测模式")
                logger.info(f"[{self.sn}] 成功切换到产测模式")
            else:
                results.append(f"切换产测模式失败: {stderr}")
                logger.warning(f"[{self.sn}] 切换产测模式失败: {stderr}")
                return False, results
        else:
            logger.info(f"[{self.sn}] {self.chip_config.chip_type} 芯片，跳过产测模式切换")
            results.append(f"{self.chip_config.chip_type} 芯片无需切换产测模式")

        return True, results

    def switch_to_normal_mode(self) -> tuple:
        """切换回正常（生产）模式"""
        results = []

        logger.info(f"[{self.sn}] 切换到正式模式...")
        
        # hisi 和 RK3576 使用统一的 ws73-modules 命令
        if self.chip_config.chip_type in ['hisi', 'wdse']:
            success, stdout, stderr = self.ssh.execute_command("ws73-modules switch official", timeout=120)
            logger.info(f"[{self.sn}] ws73-modules switch official 结果: success={success}, stdout={stdout}, stderr={stderr}")
            
            if success:
                results.append("已切换到正式模式")
                logger.info(f"[{self.sn}] 成功切换到正式模式")
                return True, results
            else:
                results.append(f"切换正式模式失败: {stderr}")
                logger.warning(f"[{self.sn}] 切换正式模式失败: {stderr}")
                return False, results
        else:
            logger.info(f"[{self.sn}] {self.chip_config.chip_type} 芯片无需切换模式")
            results.append(f"{self.chip_config.chip_type} 芯片无需切换模式")
            return True, results

    def is_connected(self) -> bool:
        """检查设备连接是否存活"""
        if not self.ssh:
            return False
        return self.ssh.is_alive()
    

    def disconnect(self):
        if self.ssh:
            self.ssh.disconnect()

    def test_usb3(self) -> TestResult:
        """USB 3.0 接口检测：协商速率 >= 5000M 的设备存在即通过

        质检时接入一个 USB 3.0 设备（U 盘/移动硬盘），
        只要它在 USB 3.x 超速模式下枚举出来就判通过；
        退化成 480M（USB 2.0）或在 3.0 总线上没有设备则判不通过。

        root hub 自己也会报 5000M，必须排除，否则永远通过（见 usb_link）。
        """
        logger.info(f"[{self.sn}] 开始检测 USB 接口...")
        result = TestResult()
        result.set_testing()

        try:
            if not self.chip_config.usb_detection:
                logger.info(f"[{self.sn}] 芯片类型 {self.chip_config.chip_model}，USB 检测默认通过")
                result.set_passed("USB 检测跳过",
                                  f"{self.chip_config.chip_model} 芯片 USB 检测默认通过")
                return result

            min_speed = config.usb_min_speed
            connector = self.chip_config.usb_connector
            logger.info(f"[{self.sn}] 芯片 {self.chip_config.chip_model} 的 USB 接口: "
                        f"{connector or '未配置'}, 判定阈值 {min_speed:g} Mbps")

            devices, source, notes = usb_link.probe(
                self.ssh, min_speed=min_speed,
                retries=config.usb_probe_retries, interval=config.usb_probe_interval)

            found = usb_link.superspeed_devices(devices, min_speed)
            only_usb2 = usb_link.usb2_devices(devices)
            others = [d for d in devices if not d.is_root_hub]
            detail = usb_link.format_detail(devices, source, min_speed, connector, notes)

            logger.info(f"[{self.sn}] USB 设备树 ({source}):\n"
                        + "\n".join(d.label for d in devices if not d.is_root_hub))
            for note in notes:
                logger.info(f"[{self.sn}] USB 检测备注: {note}")

            if found:
                # 面板一行：接口 + 主设备速率 + 主设备类型
                head = found[0]
                extra = f" 等 {len(found)} 个设备" if len(found) > 1 else ""
                message = (f"USB 3.0 正常 ({connector or '接口'}: {head.dev_class} "
                           f"{head.speed_text}{extra})")
                result.set_passed(message, detail)
                logger.info(f"[{self.sn}] USB 检测通过: {message}")
            elif only_usb2:
                # 有设备但都只跑在 2.0：质检用的设备插上了却没跑 3.0，
                # 常见原因是线材/接口是 2.0、或插在了 2.0 口上
                summary = '、'.join(f"{d.dev_class} {d.speed_text}" for d in only_usb2[:3])
                message = f"USB 未跑在 3.0 ({summary})"
                result.set_failed(message, detail)
                logger.warning(f"[{self.sn}] USB 检测失败: {message}")
            elif others:
                # 有设备但都读不到速率（老固件 / 权限不足）：不能当通过
                summary = '、'.join(d.dev_class for d in others[:3])
                message = f"USB 设备速率未知 ({summary})"
                result.set_failed(message, detail)
                logger.warning(f"[{self.sn}] USB 检测失败: {message}")
            else:
                message = "未检测到 USB 3.0 设备"
                result.set_failed(message, detail)
                logger.warning(f"[{self.sn}] USB 检测失败: {message}")

        except Exception as e:
            result.set_failed("USB 检测异常", str(e))
            logger.error(f"[{self.sn}] USB 检测异常: {e}", exc_info=True)

        return result

    def test_poe_ports(self) -> TestResult:
        """POE 网口检测：自动获取设备的 POE 网段（eth1）再扫描"""
        logger.info(f"[{self.sn}] 开始检测 POE 网口...")
        result = TestResult()
        result.set_testing()

        try:
            # 使用芯片配置中的 POE 口数量
            expected = self.chip_config.poe_count
            logger.info(f"[{self.sn}] 芯片类型: {self.chip_config.chip_model}, 期望POE口数量: {expected}")

            if expected == 0:
                result.set_passed("跳过检测", "POE 检测已配置为跳过")
                logger.info(f"[{self.sn}] POE 检测已跳过（配置为0）")
                return result

            # 自动识别设备上 POE 网口的网段（默认 eth1，识别不到回落到配置文件）
            target = poe_net.resolve(self.ssh, iface=config.poe_interface,
                                    fallback_subnet=config.poe_subnet)
            logger.info(f"[{self.sn}] POE网段: {target.label}, 来源: {target.source}"
                       f"{' (' + target.note + ')' if target.note else ''}")
            detail_lines = [
                f"扫描网段: {target.label} ({target.source})",
                f"网口: {target.interface or '未识别'}",
                f"CIDR: {target.cidr or 'N/A'}",
            ]
            if target.note:
                detail_lines.append(f"备注: {target.note}")

            online_count, arp_detail, err = poe_net.count_online(self.ssh, target)
            if arp_detail:
                logger.info(f"[{self.sn}] ARP表详情:\n{arp_detail}")

            if online_count is not None:
                logger.info(f"[{self.sn}] POE设备检测结果: 发现 {online_count} 个REACHABLE设备，期望 {expected} 个")

                detail_lines.append(f"在线设备: {online_count} / 期望 {expected}")
                if arp_detail:
                    detail_lines.append("邻居表:\n" + arp_detail)

                if online_count >= expected:
                    result.set_passed(f"通过 (检测到 {online_count} 路/目标 {expected} 路)",
                                     '\n'.join(detail_lines))
                    logger.info(f"[{self.sn}] POE 检测通过: {online_count}/{expected}")
                else:
                    result.set_failed(f"失败 (检测到 {online_count} 路/目标 {expected} 路)",
                                     '\n'.join(detail_lines))
                    logger.warning(f"[{self.sn}] POE 检测失败: {online_count}/{expected}")
            else:
                result.set_failed("执行失败", err)
                logger.error(f"[{self.sn}] POE 检测命令执行失败: {err}")
        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] POE 检测异常: {e}")

        return result

    def check_poe_cleared(self) -> tuple:
        """打印标签前的强校验：POE 口当前在线设备数必须为 0

        与 `test_poe_ports()` 的判定方向相反 —— 那边要求「至少有 expected 路」，
        这边要求「一路都没有」，所以不能复用检测结论，只能重新数一次。

        为什么要**清表后重数**：POE 设备拔掉后，设备侧 ARP 表里的条目不会立刻消失
        （REACHABLE 还会残留一段时间），不清表直接数会把刚拔掉的设备算进去，
        校验永远不通过、现场以为没拔干净。

        Returns:
            (是否已清零, 提示文本)
            未清零时仅提示在线数量，统计失败时提示失败原因
        """
        try:
            if self.chip_config.poe_count == 0:
                return True, '该芯片未配置 POE 口，跳过校验'

            target = poe_net.resolve(self.ssh, iface=config.poe_interface,
                                     fallback_subnet=config.poe_subnet)
            logger.info(f"[{self.sn}] 打印前 POE 校验，扫描网段: {target.label}"
                        f" ({target.source})")

            online, neighbours, err = poe_net.count_online(self.ssh, target)
            if online is None:
                logger.error(f"[{self.sn}] 打印前 POE 校验无法统计: {err}")
                return False, f'无法确认 POE 口状态：{err}'

            if online == 0:
                logger.info(f"[{self.sn}] 打印前 POE 校验通过：在线 0 路")
                return True, 'POE 口在线设备数为 0'

            logger.warning(f"[{self.sn}] 打印前 POE 校验未通过：在线 {online} 台\n{neighbours}")
            return False, f'POE 口仍检测到 {online} 台在线设备，请拔掉全部 POE 设备后重试。'
        except Exception as e:
            logger.error(f"[{self.sn}] 打印前 POE 校验异常: {e}", exc_info=True)
            return False, f'POE 校验异常: {e}'

    def _get_disk_firmware(self, dev_name: str) -> str:
        """通过 smartctl -a /dev/xxx 读取硬盘固件版本（Firmware Version）"""
        if not dev_name or dev_name in ('未知设备',):
            return ""
        try:
            cmd = f"smartctl -a /dev/{dev_name} 2>&1 | grep -i 'Firmware Version'"
            success, stdout, stderr = self.ssh.execute_command(cmd, timeout=20)
            output = (stdout or "") + (stderr or "")
            logger.info(f"[{self.sn}] smartctl -a /dev/{dev_name} 固件信息: {output.strip()}")
            match = re.search(r'Firmware\s+Version\s*:\s*(.+)', output, re.IGNORECASE)
            if match:
                return match.group(1).strip()
            # NVMe 盘可能使用 "Firmware Revision" 字段
            match = re.search(r'Firmware\s+Revision\s*:\s*(.+)', output, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        except Exception as e:
            logger.warning(f"[{self.sn}] 获取硬盘 /dev/{dev_name} 固件版本失败: {e}")
        return ""

    def test_disk(self) -> TestResult:
        logger.info(f"[{self.sn}] 开始检测硬盘...")
        result = TestResult()
        result.set_testing()

        try:
            # 一次读回整盘列表 + 可移动标记/总线归属，接 U 盘时不会把 U 盘当硬盘
            disks, error = disk_info.probe(self.ssh)

            if error and not disks:
                result.set_failed("检测命令执行失败", error)
                logger.error(f"[{self.sn}] 硬盘检测失败: {error}")
                return result

            logger.info(f"[{self.sn}] 磁盘原始信息: "
                        + "; ".join(f"{d.name}={d.display_model}/{d.size}"
                                    f"{'/可移动' if not d.is_internal else ''}"
                                    for d in disks))

            internal = disk_info.internal_disks(disks)
            note = disk_info.ignored_note(disks)
            if note:
                logger.info(f"[{self.sn}] {note}")

            if internal:
                # 查询固件版本（smartctl -a /dev/xxx）
                firmwares = {info.name: self._get_disk_firmware(info.name) for info in internal}

                disk_summary = []
                for info in internal:
                    fw = firmwares.get(info.name)
                    suffix = f", FW:{fw}" if fw else ""
                    size = info.size or "容量未知"
                    disk_summary.append(f"{info.display_model} ({size}{suffix})")

                result_message = f"检测到 {len(internal)} 个硬盘: {', '.join(disk_summary)}"

                detail_lines = []
                for i, info in enumerate(internal):
                    fw = firmwares.get(info.name) or '未获取'
                    vendor = info.vendor or '未知品牌'
                    size = info.size or '容量未知'
                    detail_lines.append(
                        f"硬盘{i+1}: 品牌={vendor}, 型号={info.display_model}, "
                        f"大小={size}, 固件版本={fw}"
                    )
                if note:
                    detail_lines.append(note)
                result_detail = "\n".join(detail_lines)

                result.set_passed(result_message, result_detail)
                logger.info(f"[{self.sn}] 硬盘检测通过: {result_message}")
                logger.info(f"[{self.sn}] 硬盘详情: {result_detail}")
            elif disks:
                # 只有 U 盘/移动硬盘：说明「没装内置硬盘」，不能当通过
                movable = disk_info.movable_disks(disks)
                found = '、'.join(d.label for d in movable)
                result.set_failed(
                    "未检测到内置硬盘",
                    f"只找到可移动设备（U 盘/移动硬盘），不算内置硬盘: {found}"
                )
                logger.error(f"[{self.sn}] 硬盘检测失败: 只有可移动设备 {found}")
            else:
                result.set_failed("未检测到硬盘", "没有找到任何块设备")
                logger.error(f"[{self.sn}] 硬盘检测失败: 未找到存储设备")
        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] 硬盘检测异常: {e}")

        return result

    def test_wifi(self) -> TestResult:
        """通过 wpa_cli 扫描 WiFi，检测是否有信号强度达标的网络"""
        logger.info(f"[{self.sn}] 开始 WiFi 检测...")
        result = TestResult()
        result.set_testing()

        try:
            # 针对不支持的芯片，WiFi 检测默认通过
            if not self.chip_config.wifi_detection:
                logger.info(f"[{self.sn}] 芯片类型 {self.chip_config.chip_model} WiFi 检测默认通过")
                result.set_passed("WiFi 检测跳过", f"{self.chip_config.chip_model} 芯片 WiFi 检测默认通过")
                return result

            # 正常 WiFi 检测流程（适用于 hisi 等芯片）
            # 1. 自动检测 WiFi 接口名称（使用 ls /sys/class/net 查找 wlan 接口）
            logger.info(f"[{self.sn}] 检测 WiFi 接口...")
            success, stdout, _ = self.ssh.execute_command("ls /sys/class/net/ | grep -E '^wlan[0-9]'", timeout=5)
            # 只取第一行，过滤非字母数字的异常内容
            wifi_interface = 'wlan0'
            if success and stdout.strip():
                candidate = stdout.strip().split('\n')[0].strip()
                if candidate and candidate.replace('_', '').isalnum():
                    wifi_interface = candidate
            logger.info(f"[{self.sn}] 使用 WiFi 接口: {wifi_interface}")

            # 2. 根据芯片类型采用不同的 wpa_supplicant 启动流程
            if self.chip_config.chip_model == 'RK3576':
                # RK3576 芯片专用流程
                logger.info(f"[{self.sn}] RK3576 芯片，执行专用 WiFi 检测流程...")
                
                # 步骤1: 启用网络接口
                logger.info(f"[{self.sn}] 启用 {wifi_interface} 接口...")
                self.ssh.execute_command(f"ip link set {wifi_interface} up", timeout=5)
                time.sleep(0.5)
                
                # 步骤2: 温和地停止 wpa_supplicant 进程（避免触发系统重启）
                logger.info(f"[{self.sn}] 检查并停止旧的 wpa_supplicant 进程...")
                # 先检查是否有进程在运行
                success, stdout, _ = self.ssh.execute_command("pgrep -x wpa_supplicant", timeout=5)
                if success and stdout.strip():
                    # 使用 SIGTERM 优雅停止，避免 killall 可能触发的看门狗
                    logger.info(f"[{self.sn}] 发现运行中的 wpa_supplicant，发送 SIGTERM...")
                    self.ssh.execute_command("pkill -TERM wpa_supplicant", timeout=5)
                    time.sleep(1)
                    # 验证是否已停止
                    success2, stdout2, _ = self.ssh.execute_command("pgrep -x wpa_supplicant", timeout=5)
                    if success2 and stdout2.strip():
                        logger.warning(f"[{self.sn}] wpa_supplicant 未能优雅停止，使用 SIGKILL...")
                        self.ssh.execute_command("pkill -KILL wpa_supplicant", timeout=5)
                        time.sleep(0.5)
                else:
                    logger.info(f"[{self.sn}] 没有运行中的 wpa_supplicant 进程")
                time.sleep(0.5)
                
                # 步骤3: 创建控制套接字目录
                logger.info(f"[{self.sn}] 创建 wpa_supplicant 控制套接字目录...")
                self.ssh.execute_command("mkdir -p /var/run/wpa_supplicant", timeout=5)
                
                # 步骤4: 启动 wpa_supplicant (使用 /dev/null 配置文件)
                logger.info(f"[{self.sn}] 启动 wpa_supplicant...")
                success, stdout, stderr = self.ssh.execute_command(
                    f"wpa_supplicant -B -i {wifi_interface} -c /dev/null -C /var/run/wpa_supplicant",
                    timeout=10
                )
                if not success:
                    result.set_failed("wpa_supplicant 启动失败", stderr)
                    logger.error(f"[{self.sn}] wpa_supplicant 启动失败: {stderr}")
                    return result
                
                # 等待 wpa_supplicant 就绪
                time.sleep(1)
            else:
                # hisi 芯片专用流程
                logger.info(f"[{self.sn}] hisi 芯片，执行专用 WiFi 检测流程...")
                
                # 步骤1: 停止 wifi-service
                logger.info(f"[{self.sn}] 停止 wifi-service...")
                self.ssh.execute_command("systemctl stop wifi-service", timeout=10)
                time.sleep(1)
                
                # 步骤2: 启动 wpa_supplicant
                logger.info(f"[{self.sn}] 启动 wpa_supplicant...")
                self.ssh.execute_command("killall wpa_supplicant 2>/dev/null", timeout=5)
                time.sleep(0.5)
                
                success, stdout, stderr = self.ssh.execute_command(
                    f"wpa_supplicant -i {wifi_interface} -D nl80211 -c /etc/Wireless/wpa_supplicant.conf -B",
                    timeout=10
                )
                if not success:
                    result.set_failed("wpa_supplicant 启动失败", stderr)
                    logger.error(f"[{self.sn}] wpa_supplicant 启动失败: {stderr}")
                    return result
                
                # 等待 wpa_supplicant 就绪
                time.sleep(1)

            # 3. 验证 wpa_cli 可用性
            success, stdout, _ = self.ssh.execute_command(f"wpa_cli -i {wifi_interface} ping", timeout=5)
            if not success or "PONG" not in stdout:
                result.set_failed("wpa_supplicant 未就绪", "wpa_cli ping 无响应")
                logger.error(f"[{self.sn}] wpa_cli ping 失败，控制套接字未就绪")
                return result

            # 4. 执行扫描（显式指定接口）
            logger.info(f"[{self.sn}] 执行 wpa_cli scan...")
            success, stdout, stderr = self.ssh.execute_command(f"wpa_cli -i {wifi_interface} scan", timeout=10)
            if not success:
                result.set_failed("wpa_cli scan 失败", stderr)
                logger.error(f"[{self.sn}] wpa_cli scan 失败: {stderr}")
                return result
            
            # 根据芯片类型设置扫描等待时间
            if self.chip_config.chip_model == 'RK3576':
                time.sleep(3)  # RK3576 等待 3 秒
            else:
                time.sleep(1)  # hisi 等待 1 秒

            # 5. 获取扫描结果
            logger.info(f"[{self.sn}] 获取扫描结果...")
            success, stdout, stderr = self.ssh.execute_command(f"wpa_cli -i {wifi_interface} scan_results", timeout=10)

            if not success or not stdout:
                result.set_failed("扫描失败", f"wpa_cli 执行失败: {stderr}")
                logger.error(f"[{self.sn}] WiFi 扫描失败: {stderr}")
                return result

            logger.info(f"[{self.sn}] 扫描结果:\n{stdout}")

            # 解析结果，提取信号强度达标的网络
            # 输出前两行为 "Selected interface 'wlan0'" 和 "bssid / frequency / ..."，跳过
            threshold = config.wifi_signal_threshold
            lines = stdout.strip().split('\n')
            strong_networks = []
            for line in lines:
                parts = line.split()
                # 有效数据行以 MAC 地址开头 (xx:xx:xx:xx:xx:xx)
                if len(parts) >= 4 and re.match(r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$', parts[0]):
                    try:
                        signal = int(parts[2])
                    except ValueError:
                        continue
                    if signal > threshold:
                        ssid = ' '.join(parts[4:]) if len(parts) > 4 else ''
                        strong_networks.append({'bssid': parts[0], 'signal': signal, 'ssid': ssid})

            if strong_networks:
                detail_lines = [f"{n['ssid'] or n['bssid']} (signal: {n['signal']})" for n in strong_networks]
                result.set_passed(
                    f"WiFi 正常 ({len(strong_networks)}个强信号)",
                    f"signal > {threshold} 的网络:\n" + "\n".join(detail_lines)
                )
                logger.info(f"[{self.sn}] WiFi 检测通过: {len(strong_networks)} 个信号 > {threshold}")
            else:
                result.set_failed("WiFi 信号弱", f"未检测到 signal > {threshold} 的网络")
                logger.warning(f"[{self.sn}] WiFi 信号不足")
        except Exception as e:
            result.set_failed("WiFi 扫描异常", str(e))
            logger.error(f"[{self.sn}] WiFi 检测异常: {e}")

        return result

    def test_bluetooth(self) -> TestResult:
        logger.info(f"[{self.sn}] 开始蓝牙检测...")
        result = TestResult()
        result.set_testing()

        try:
            # 针对不支持的芯片，蓝牙检测默认通过
            if not self.chip_config.bt_detection:
                logger.info(f"[{self.sn}] 芯片类型 {self.chip_config.chip_model} 蓝牙检测默认通过")
                result.set_passed("蓝牙检测跳过", f"{self.chip_config.chip_model} 芯片蓝牙检测默认通过")
                return result
            script_path = str(config_dir() / "blescan.sh")
            if not os.path.exists(script_path):
                result.set_failed("脚本不存在", f"找不到文件: {script_path}")
                logger.error(f"[{self.sn}] 蓝牙测试脚本不存在: {script_path}")
                return result

            remote_path = "/root/blescan.sh"
            if not self.ssh.upload_file(script_path, remote_path):
                result.set_failed("上传失败", "无法上传测试脚本")
                logger.error(f"[{self.sn}] 蓝牙测试脚本上传失败")
                return result

            self.ssh.execute_command(f"chmod +x {remote_path}", timeout=5)
            success, stdout, stderr = self.ssh.execute_command(f"{remote_path}", timeout=30)

            output = stdout + stderr
            devices = re.findall(r'ScanResultCallback: gap ext adv report.*?addr:(0x[0-9a-fA-F:*]+)', output)

            if devices:
                count = len(devices)
                result.set_passed(f"蓝牙正常 (检测到 {count} 个信号)", f"扫描到 {count} 个蓝牙设备")
                logger.info(f"[{self.sn}] 蓝牙检测通过: 扫描到 {count} 个设备")
            else:
                result.set_failed("未扫描到设备", "蓝牙扫描结果为空")
                logger.warning(f"[{self.sn}] 蓝牙检测失败")
        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] 蓝牙检测异常: {e}")

        return result

    def test_nearlink(self) -> TestResult:
        logger.info(f"[{self.sn}] 开始星闪检测...")
        result = TestResult()
        result.set_testing()

        try:
            # 针对不支持的芯片，星闪检测默认通过
            if not self.chip_config.nearlink_detection:
                logger.info(f"[{self.sn}] 芯片类型 {self.chip_config.chip_model} 星闪检测默认通过")
                result.set_passed("星闪检测跳过", f"{self.chip_config.chip_model} 芯片星闪检测默认通过")
                return result
            script_path = str(config_dir() / "slescan.sh")
            if not os.path.exists(script_path):
                result.set_failed("脚本不存在", f"找不到文件: {script_path}")
                logger.error(f"[{self.sn}] 星闪测试脚本不存在: {script_path}")
                return result

            remote_path = "/root/slescan.sh"
            if not self.ssh.upload_file(script_path, remote_path):
                result.set_failed("上传失败", "无法上传测试脚本")
                logger.error(f"[{self.sn}] 星闪测试脚本上传失败")
                return result

            self.ssh.execute_command(f"chmod +x {remote_path}", timeout=5)
            success, stdout, stderr = self.ssh.execute_command(f"{remote_path}", timeout=30)

            output = stdout + stderr
            devices = re.findall(r'\[adv_report\].*?addr:\s*([\d:*a-fA-F]+)', output)

            if devices:
                count = len(devices)
                result.set_passed(f"星闪正常 (检测到 {count} 个信号)", f"扫描到 {count} 个星闪设备")
                logger.info(f"[{self.sn}] 星闪检测通过: 扫描到 {count} 个设备")
            else:
                result.set_failed("未扫描到设备", "星闪扫描结果为空")
                logger.warning(f"[{self.sn}] 星闪检测失败")
        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] 星闪检测异常: {e}")

        return result

    # ---- MAC 烧写 ----

    def _switch_to_factory(self, reason: str = 'MAC烧写') -> tuple:
        """释放 ble_soc 后切换产测模式，返回 (是否可以继续烧写, 详情行列表)

        顺序固定，不能反：先杀 wd24h-edge-core(java) 释放 ble_soc，
        再 `ws73-modules switch factory`。若模组仍被业务占用，切换会直接报
        `Module ble_soc is in use`，此时烧写必然全部失败。

        切换后仍报 in use（java 被守护脚本拉起）时，再清一次进程重试一次。

        Returns:
            (ok, notes)：ok=False 表示 ble_soc 仍未释放，调用方应立即中止；
            notes 是可以直接塞进检测详情的说明行
        """
        notes = []
        released, msg = self.release_ble_soc(reason)
        notes.append(f"释放 ble_soc: {msg}")
        if not released:
            return False, notes

        logger.info(f"[{self.sn}] {self.chip_config.chip_type} 芯片，执行 ws73-modules switch factory...")
        success, stdout, stderr = self.ssh.execute_command("ws73-modules switch factory", timeout=120)
        out = f"{stdout}\n{stderr}".strip()

        if 'in use' in out.lower():
            logger.warning(f"[{self.sn}] ws73-modules 报模块被占用，重新清理进程后重试")
            released, msg = self.release_ble_soc(f'{reason}重试')
            notes.append(f"重试释放 ble_soc: {msg}")
            if released:
                success, stdout, stderr = self.ssh.execute_command(
                    "ws73-modules switch factory", timeout=120)
                out = f"{stdout}\n{stderr}".strip()

        logger.info(f"[{self.sn}] ws73-modules 输出:\n{out}")

        if 'in use' in out.lower():
            logger.error(f"[{self.sn}] ble_soc 释放后仍被占用，中止 MAC 烧写")
            notes.append(f"ble_soc 仍被占用: {out or '无输出'}")
            return False, notes

        if success:
            notes.append("产测模式已加载 (ws73-modules)")
        else:
            logger.warning(f"[{self.sn}] ws73-modules 切换失败: {out}")
            notes.append(f"产测模式切换警告: {out or '无输出'}")
        return True, notes

    def release_ble_soc(self, reason: str = '') -> tuple:
        """释放 ble_soc 占用：杀掉业务侧 Java 进程，再做一次兜底回收

        `ws73-modules switch factory` 要重新加载 8800 无线模组，若 wd24h-edge-core
        （java）还在跑，模组处于 business 模式被业务占用，切换会直接报
        `Module ble_soc is in use`，随后的 MAC 烧写自然全部失败。

        处理顺序与蓝牙检测脚本（config/blescan.sh）保持一致：先 SIGKILL java，
        再等内核回收，最后确认真的没了。

        Args:
            reason: 写日志用的场景说明（如 'MAC烧写'）

        Returns:
            (释放成功, 说明)
        """
        tag = f'[{self.sn}]{"" if not reason else " " + reason}'
        pkgs = self._ble_holder_processes()
        if not pkgs:
            logger.info(f"{tag} 未发现占用 ble_soc 的 java 进程")
            return True, '无 java 进程占用'

        logger.info(f"{tag} 检测到占用 ble_soc 的 java 进程: {pkgs}，开始清理")
        self.ssh.execute_command('pkill -9 -x java', timeout=10)
        self.ssh.execute_command('pkill -9 -f wd24h-edge-core', timeout=10)

        for _ in range(6):
            time.sleep(0.5)
            if not self._ble_holder_processes():
                logger.info(f"{tag} java 进程已清理")
                return True, '已清理 java 进程'

        remain = self._ble_holder_processes()
        logger.warning(f"{tag} java 进程未能完全清理: {remain}")
        return False, f'java 进程残留: {remain}'

    BLE_HOLDER_PATTERNS = ('java', 'wd24h-edge-core')

    def _ps_supports_ef(self) -> bool:
        """本机 ps 是否支持 -ef，探测一次后缓存

        不能靠 `ps -ef | grep ...` 的退出码判断：grep 无匹配时也返回非 0，
        与「ps 不认 -ef」的表现完全一样，那样会在空闲设备上无谓地退回裸 ps，
        还打一条误导性的 warning。所以单独探测一次真正的 ps，结果缓存。
        """
        cached = getattr(self, '_ps_ef_ok', None)
        if cached is None:
            ok, _, _ = self.ssh.execute_command('ps -ef > /dev/null 2>&1', timeout=5)
            cached = self._ps_ef_ok = bool(ok)
            if not cached:
                logger.warning(f"[{self.sn}] ps 不支持 -ef（裁剪版 BusyBox），改用裸 ps 读进程表")
        return cached

    def _ble_holder_processes(self) -> str:
        """返回仍在运行的占用进程（ps 行），空串表示已无占用

        必须用 `ps -ef`：裸 `ps` 的 COMMAND 列只有可执行文件名（还会按宽度截断），
        `wd24h-edge-core` 是 java 的 jar 参数，根本不会出现在那一列里，
        按它兜底匹配等于白匹配；只有带全量命令行才有意义。

        设备是 BusyBox，`pgrep` 不一定存在（blescan.sh 用过，但别处固件缺），
        所以走 ps + grep。极少数裁剪版 BusyBox 的 ps 不认 `-ef`（报 invalid option），
        此时退回裸 `ps`：那种固件的 COMMAND 列本身就是完整 cmdline，够用。
        """
        pattern = '|'.join(self.BLE_HOLDER_PATTERNS)
        ps_cmd = 'ps -ef' if self._ps_supports_ef() else 'ps'
        success, stdout, stderr = self.ssh.execute_command(
            f"{ps_cmd} | grep -E '{pattern}' | grep -v grep", timeout=10)
        if not success and not stdout:
            return ''
        return stdout.strip()

    def _exec_and_read_dmesg(self, cmd: str) -> str:
        """写 /sys/ccsys/ccpriv 后读 dmesg 最后几行，返回拼接的文本"""
        self.ssh.execute_command(cmd, timeout=10)
        time.sleep(0.5)
        success, stdout, stderr = self.ssh.execute_command("dmesg | grep Addr", timeout=50)
        return stdout if success else ""

    def _read_mac(self, read_cmd: str) -> str | None:
        """执行读取命令，从 dmesg 解析 Mac Addr 行，返回最后一个 MAC 字符串或 None"""
        output = self._exec_and_read_dmesg(read_cmd)
        logger.info(f"[{self.sn}] dmesg 原始输出:\n{output}")
        matches = re.findall(r'Mac\s+Addr\s*:\s*((?:[0-9a-fA-F*]{2}:){5}[0-9a-fA-F*]{2})', output, re.IGNORECASE)
        if matches:
            mac = matches[-1]
            logger.info(f"[{self.sn}] 读取到 MAC: {mac} (共找到 {len(matches)} 个匹配)")
            return mac
        logger.warning(f"[{self.sn}] 未能解析 MAC")
        return None

    def _write_and_verify_mac(self, name: str, read_cmd: str, write_cmd: str, mac_colon: str) -> tuple[bool, str]:
        """
        读→判断→烧写→验证
        返回 (成功, 描述信息)
        """
        # 1. 先读取当前值
        current = self._read_mac(read_cmd)
        if current and not current.startswith("00:00:00"):
            msg = f"{name} 已烧写: {current}，跳过"
            logger.info(f"[{self.sn}] {msg}")
            return True, msg

        # 2. 需要烧写 — 将冒号格式转为逗号格式
        mac_comma = mac_colon.replace(":", ",")
        burn_cmd = write_cmd.format(mac=mac_comma)
        logger.info(f"[{self.sn}] 烧写 {name}: {burn_cmd}")
        self._exec_and_read_dmesg(burn_cmd)

        # 3. 验证
        time.sleep(0.5)
        verify = self._read_mac(read_cmd)
        if verify and not verify.startswith("00:00:00"):
            msg = f"{name} 烧写成功: {verify}"
            logger.info(f"[{self.sn}] {msg}")
            return True, msg
        else:
            msg = f"{name} 烧写后验证失败，读取到: {verify}"
            logger.error(f"[{self.sn}] {msg}")
            return False, msg

    def test_mac_write(self) -> TestResult:
        """MAC 烧写主流程"""
        logger.info(f"[{self.sn}] 开始 MAC 烧写...")
        result = TestResult()
        result.set_testing()
        details = []

        try:
            # 1. 先读取当前MAC地址
            logger.info(f"[{self.sn}] 读取当前MAC地址...")
            wifi_current = self._read_mac('echo "efuse_read_mac" > /sys/ccsys/ccpriv')
            starflash_current = self._read_mac('echo "efuse_read_sle_mac" > /sys/ccsys/ccpriv')

            wifi_ok = wifi_current and not wifi_current.startswith("00:00:00")
            starflash_ok = starflash_current and not starflash_current.startswith("00:00:00")

            # 2. 如果两个MAC都已烧写，直接返回成功
            if wifi_ok and starflash_ok:
                details.append(f"WiFi MAC 已烧写: {wifi_current}")
                details.append(f"星闪 MAC 已烧写: {starflash_current}")
                ble_mac = self._read_mac('echo "get_ble_mac" > /sys/ccsys/ccpriv')
                if ble_mac:
                    details.append(f"蓝牙 MAC: {ble_mac}")
                summary = f"WiFi:{wifi_current} | 星闪:{starflash_current}"
                if ble_mac:
                    summary += f" | 蓝牙:{ble_mac}"
                result.set_passed(summary, "\n".join(details))
                logger.info(f"[{self.sn}] MAC已烧写，无需重新烧写")
                return result

            # 3. 需要烧写，先切换到产测模式
            logger.info(f"[{self.sn}] 检测到未烧写的MAC，切换产测模式...")
            if self.chip_config.chip_type in ['hisi', 'wdse']:
                ok, notes = self._switch_to_factory('MAC烧写')
                details.extend(notes)
                if not ok:
                    result.set_failed("ble_soc 被占用", "\n".join(details))
                    return result
                time.sleep(2)  # 等待模块加载完成
            else:
                logger.info(f"[{self.sn}] {self.chip_config.chip_type} 芯片，跳过产测模式切换")
                details.append(f"{self.chip_config.chip_type} 芯片无需切换产测模式")

            # 4. 分配MAC地址
            allocator = MACAllocator()
            mac_info = allocator.allocate_mac(self.sn)
            if not mac_info:
                result.set_failed("MAC 分配失败", "无法从服务器或 CSV 获取 MAC 地址")
                return result

            wifi_mac = mac_info['wifi_mac']
            starflash_mac = mac_info['starflash_mac']
            details.append(f"分配 WiFi MAC: {wifi_mac}")
            details.append(f"分配星闪 MAC: {starflash_mac}")

            # 5. 烧写需要的MAC
            if not wifi_ok:
                mac_comma = wifi_mac.replace(":", ",")
                burn_cmd = f'echo "set_efuse_mac {mac_comma}; " > /sys/ccsys/ccpriv'
                logger.info(f"[{self.sn}] 烧写 WiFi MAC: {burn_cmd}")
                self._exec_and_read_dmesg(burn_cmd)
                time.sleep(0.5)
                details.append(f"WiFi MAC 已烧写")

            if not starflash_ok:
                mac_comma = starflash_mac.replace(":", ",")
                burn_cmd = f'echo "set_sle_mac {mac_comma}; " > /sys/ccsys/ccpriv'
                logger.info(f"[{self.sn}] 烧写星闪 MAC: {burn_cmd}")
                self._exec_and_read_dmesg(burn_cmd)
                time.sleep(0.5)
                details.append(f"星闪 MAC 已烧写")

            # 6. 验证烧写结果
            time.sleep(0.5)
            wifi_verify = self._read_mac('echo "efuse_read_mac" > /sys/ccsys/ccpriv')
            starflash_verify = self._read_mac('echo "efuse_read_sle_mac" > /sys/ccsys/ccpriv')

            wifi_verify_ok = wifi_verify and not wifi_verify.startswith("00:00:00")
            starflash_verify_ok = starflash_verify and not starflash_verify.startswith("00:00:00")

            if wifi_verify_ok and starflash_verify_ok:
                ble_mac = self._read_mac('echo "get_ble_mac" > /sys/ccsys/ccpriv')
                if ble_mac:
                    details.append(f"蓝牙 MAC: {ble_mac}")
                summary = f"烧写成功 WiFi:{wifi_verify} | 星闪:{starflash_verify}"
                if ble_mac:
                    summary += f" | 蓝牙:{ble_mac}"
                result.set_passed(summary, "\n".join(details))
            else:
                fail_msg = []
                if not wifi_verify_ok:
                    fail_msg.append(f"WiFi MAC 烧写失败: {wifi_verify}")
                if not starflash_verify_ok:
                    fail_msg.append(f"星闪 MAC 烧写失败: {starflash_verify}")
                result.set_failed(" | ".join(fail_msg), "\n".join(details))

        except Exception as e:
            result.set_failed("MAC 烧写异常", f"{e}\n" + "\n".join(details))
            logger.error(f"[{self.sn}] MAC 烧写异常: {e}")

        return result

    def _exec_at_on_port(self, at_port: str) -> str:
        """在指定 AT 口下发识别命令（ATI），返回原始响应"""
        spec = lte_signal.ati_spec(at_port)
        at_cmd = lte_signal.build_at_command(spec)
        success, stdout, stderr = self.ssh.execute_command(at_cmd, timeout=15)
        if not success:
            logger.warning(f"[{self.sn}] {spec.at_port} 执行 ATI 失败: {stderr}")
            return ''
        return stdout

    def _read_signal(self, spec) -> str:
        """按模组各自的 AT 命令读取信号，返回原始响应"""
        success, stdout, stderr = self.ssh.execute_command(
            lte_signal.build_at_command(spec), timeout=15)
        if not success:
            logger.warning(f"[{self.sn}] {spec.name} 信号读取失败: {stderr}")
            return ''
        return stdout

    def _probe_lte_module(self):
        """识别 4G 模组厂商并读取信号，返回 (spec, model, parsed, note)

        note 是详情里的识别方式说明：ATI 认出 / 按响应特征兜底 / 识别失败。

        1. `lte.force_module` 指定了厂商就直接用（已知批次可省一次往返）
        2. 否则在 `lte.at_port` 下发 ATI，按模组表的关键字识别厂商
        3. ATI 认不出来（串口未握手 / 旧固件不回型号）时按响应格式依次试读。
           移远的 QENG 直出真实值、龙尚的 CPSI 才需要换算码值，试读能落到
           正确的解析分支；两个格式都解析不出才判失败，并把各自的原因带上
        """
        modules = lte_signal.load_modules(config, at_port=lte_signal.configured_at_port(config))
        forced = lte_signal.forced_module_name(config)
        if forced:
            spec = next((m for m in modules if m.name == forced), None) or modules[0]
            parsed = lte_signal.decode(spec, self._read_signal(spec))
            logger.info(f"[{self.sn}] 4G 模组按配置指定为 {spec.name}")
            return spec, forced, parsed, f'lte.force_module 指定为 {forced}'

        ati_out = self._exec_at_on_port(
            lte_signal.configured_at_port(config) or lte_signal.DEFAULT_AT_PORT)
        spec, model = lte_signal.identify_module(ati_out, modules)
        if spec is not None:
            logger.info(f"[{self.sn}] 4G 模组识别为 {spec.name} ({model})")
            parsed = lte_signal.decode(spec, self._read_signal(spec))
            return spec, model, parsed, 'ATI 型号识别'

        logger.warning(f"[{self.sn}] ATI 未识别出模组厂商，按响应格式依次试读")
        reasons = []
        for candidate in modules:
            parsed = lte_signal.decode(candidate, self._read_signal(candidate))
            if parsed.ok:
                logger.info(f"[{self.sn}] 按 {candidate.name} 的响应格式解析成功")
                return candidate, '', parsed, 'ATI 未识别，按响应格式判定'
            reasons.append(f'{candidate.name}: {parsed.reason}')

        reason = '模组厂商识别失败（' + '; '.join(reasons) + '）'
        logger.warning(f"[{self.sn}] {reason}")
        spec = lte_signal.ModuleSpec(name='未知', at_command='ATI')
        return spec, '', lte_signal.Outcome(reason=reason), 'ATI 未识别，且无模组的响应格式能解析'

    def test_4g(self) -> TestResult:
        """4G 检测：识别模组厂商 → 读取信号 → 按统一标准判定

        判定标准与厂商无关（RSRP >= -90 dBm, SINR >= 5 dB, RSRQ >= -13 dB），
        厂商只决定「怎么读」和「怎么把码值换成真实值」。
        """
        logger.info(f"[{self.sn}] 开始检测 4G 模块...")
        result = TestResult()
        try:
            spec, model, parsed, note = self._probe_lte_module()
            vendor = lte_signal.vendor_text(spec, model)

            if not parsed.ok:
                detail = lte_signal.format_detail(spec, model, identify_note=note,
                                                  reason=parsed.reason)
                logger.warning(f"[{self.sn}] 4G 检测失败: {parsed.reason}")
                result.set_failed(f"4G 检测失败 ({vendor}, {parsed.reason})", detail)
                return result

            metrics = parsed.metrics
            passed, checks, judge_reason = lte_signal.judge(metrics)
            # 面板上括号里带字段名与单位；判定不达标的字段带 ✗，一眼看出是哪项拖累判定
            values = '(' + ' / '.join(lte_signal.summary_tokens(metrics, checks)) + ')'
            detail = lte_signal.format_detail(spec, model, metrics,
                                              checks=checks or None,
                                              identify_note=note,
                                              reason=judge_reason)

            if passed:
                message = f"4G 正常 {vendor} {values}"
                logger.info(f"[{self.sn}] 4G 检测通过: {message}")
                result.set_passed(message, detail)
            else:
                message = f"4G 信号不达标 {vendor} {values}"
                logger.warning(f"[{self.sn}] 4G 检测失败: {message} | {judge_reason}")
                result.set_failed(message, detail)

        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] 4G 检测异常: {e}")

        return result

    def test_fan(self) -> TestResult:
        logger.info(f"[{self.sn}] 开始检测风扇...")
        result = TestResult()
        result.set_testing()

        try:
            # 针对不启用风扇检测的芯片，默认通过
            if not self.chip_config.fan_detection:
                logger.info(f"[{self.sn}] 芯片类型 {self.chip_config.chip_model} 风扇检测默认通过")
                result.set_passed("风扇检测跳过", f"{self.chip_config.chip_model} 芯片风扇检测默认通过")
                return result

            # RK3576 芯片专用风扇测试流程
            if self.chip_config.chip_model == 'RK3576':
                return self._test_fan_rk3576()

            # 正常风扇检测流程（适用于 hisi 等芯片）
            for attempt in range(3):
                logger.info(f"[{self.sn}] 风扇设置尝试 {attempt + 1}/3")
                success, stdout, stderr = self.ssh.execute_command("/usr/local/bin/fan_pwm_control.sh 100", timeout=10)
                if success:
                    break
                time.sleep(1)

            time.sleep(1)
            success, stdout, stderr = self.ssh.execute_command("/usr/local/bin/fan_pwm_control.sh status", timeout=10)

            if success and stdout:
                if "10000" in stdout and "100%" in stdout:
                    result.set_passed("风扇正常 (转速100%/目标100%)", f"PWM值: 10000, 占空比: 100%")
                    logger.info(f"[{self.sn}] 风扇检测通过")
                else:
                    result.set_failed("风扇异常", f"未达到100%: {stdout}")
                    logger.warning(f"[{self.sn}] 风扇检测失败")
            else:
                result.set_failed("状态读取失败", stderr)
                logger.error(f"[{self.sn}] 风扇状态读取失败")
        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] 风扇检测异常: {e}")

        return result

    def _test_fan_rk3576(self) -> TestResult:
        """RK3576 芯片专用风扇测试流程（不 unbind pwm-fan，避免内核 use-after-free）"""
        result = TestResult()
        keeper_pid = None

        try:
            logger.info(f"[{self.sn}] RK3576 芯片，执行专用风扇检测流程...")

            # 1. 查找 hwmon 接口（编号随驱动加载顺序变化，不能硬编码）
            success, stdout, stderr = self.ssh.execute_command(
                'ls -d /sys/devices/platform/pwm-fan/hwmon/hwmon* 2>/dev/null | head -1',
                timeout=5
            )
            hw_path = stdout.strip() if success and stdout.strip() else ""
            if not hw_path:
                result.set_failed("找不到 pwm-fan hwmon 接口", "驱动未加载或已被 unbind，必须重启设备恢复")
                return result
            pwm1_path = f"{hw_path}/pwm1"
            logger.info(f"[{self.sn}] hwmon 接口: {hw_path}")

            # 2. 写入全速
            success, stdout, stderr = self.ssh.execute_command(
                f'echo 255 > {pwm1_path}',
                timeout=5
            )
            if not success:
                result.set_failed("写入 pwm1 失败", stderr)
                return result

            # 3. 启动后台重申循环（每 50ms 写一次 255，防止 thermal notifier 在温度跨档时覆盖）
            #    用 nohup + setsid 使进程在 SSH 会话结束后继续运行，finally 中再 kill
            success, stdout, stderr = self.ssh.execute_command(
                f'nohup sh -c "while :; do echo 255 > {pwm1_path} 2>/dev/null; sleep 0.05; done" '
                f'>/dev/null 2>&1 & echo $!',
                timeout=5
            )
            if success and stdout.strip().isdigit():
                keeper_pid = stdout.strip()
                logger.info(f"[{self.sn}] 重申循环已启动，PID={keeper_pid}")
            else:
                logger.warning(f"[{self.sn}] 重申循环启动失败，继续测试（可能被温控短暂打断）")

            # 等待风扇转速稳定
            logger.info(f"[{self.sn}] 等待风扇稳定...")
            time.sleep(2)

            # 4. 导出 PWM 捕获通道（pwmchip1 / PWM2_8CH_6），与 pwm-fan 占用的 pwmchip0 无关
            logger.info(f"[{self.sn}] 导出 PWM 捕获通道...")
            success, stdout, stderr = self.ssh.execute_command(
                '[ -d /sys/class/pwm/pwmchip1/pwm0 ] || echo 0 > /sys/class/pwm/pwmchip1/export',
                timeout=5
            )
            if not success and "Device or resource busy" not in stderr:
                result.set_failed("捕获通道导出失败", stderr)
                return result
            time.sleep(0.5)

            # 5. 多次采样取最大值（最多 8 次，取 3 次有效读数即止）
            #    capture 超时（风扇未转）会返回错误文本而非数字，需做格式校验
            logger.info(f"[{self.sn}] 读取风扇转速...")
            best_rpm = 0
            valid_count = 0

            for attempt in range(1, 9):
                # 采样前确认处于全速，避免温控恰好覆盖时的错误读数
                chk_ok, chk_out, _ = self.ssh.execute_command(f'cat {pwm1_path}', timeout=5)
                if not chk_ok or chk_out.strip() != "255":
                    logger.debug(f"[{self.sn}] 采样 {attempt}: pwm1 非 255，跳过")
                    time.sleep(0.2)
                    continue

                cap_ok, cap_out, cap_err = self.ssh.execute_command(
                    'cat /sys/class/pwm/pwmchip1/pwm0/capture',
                    timeout=5
                )

                # 采样后再次确认全速，两端都是 255 才算有效
                chk_ok2, chk_out2, _ = self.ssh.execute_command(f'cat {pwm1_path}', timeout=5)
                if not chk_ok2 or chk_out2.strip() != "255":
                    logger.debug(f"[{self.sn}] 采样 {attempt}: 采样后 pwm1 非 255，作废")
                    time.sleep(0.2)
                    continue

                if not cap_ok or not cap_out:
                    logger.warning(f"[{self.sn}] 采样 {attempt} 无输出: {cap_err}")
                    time.sleep(0.2)
                    continue

                # capture 返回 "period_ns duty_ns"，超时时返回错误文本
                parts = cap_out.strip().split()
                if not parts or not parts[0].isdigit():
                    logger.warning(f"[{self.sn}] 采样 {attempt} 格式无效: {cap_out.strip()}")
                    time.sleep(0.2)
                    continue

                period_ns = int(parts[0])
                if period_ns <= 0:
                    logger.warning(f"[{self.sn}] 采样 {attempt}: period={period_ns} 无效")
                    time.sleep(0.2)
                    continue

                # RPM = (1e9 / period_ns) × 60 / 2，TACH 每转产生 2 个脉冲
                rpm = 1000000000 * 60 // period_ns // 2
                logger.info(f"[{self.sn}] 采样 {attempt}: period={period_ns}ns → {rpm} RPM")

                if rpm > best_rpm:
                    best_rpm = rpm
                valid_count += 1
                if valid_count >= 3:
                    break

            if valid_count == 0:
                result.set_failed("转速读取失败", "未取得有效采样（风扇未接或未转动）")
                return result

            if best_rpm > 7000:
                result.set_passed(f"转速正常 (检测 {best_rpm} RPM/目标 >7000 RPM)",
                                  f"全速运行, 转速: {best_rpm} RPM (目标 >7000 RPM)")
                logger.info(f"[{self.sn}] 风扇检测通过: {best_rpm} RPM")
            else:
                result.set_failed(f"转速不足 (检测 {best_rpm} RPM/目标 >7000 RPM)",
                                  f"期望 >7000 RPM, 实际: {best_rpm} RPM")
                logger.warning(f"[{self.sn}] 风扇检测失败: {best_rpm} RPM < 7000 RPM")

        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] RK3576 风扇检测异常: {e}")

        finally:
            # 终止重申循环，将控制权交还温控（写任意值即可，下次温度跨档时温控自动接管）
            if keeper_pid:
                self.ssh.execute_command(f'kill {keeper_pid} 2>/dev/null', timeout=5)
                logger.info(f"[{self.sn}] 重申循环已停止 (PID={keeper_pid})")
            self.ssh.execute_command(
                'HW=$(ls -d /sys/devices/platform/pwm-fan/hwmon/hwmon* 2>/dev/null | head -1); '
                '[ -n "$HW" ] && echo 150 > "$HW/pwm1" 2>/dev/null',
                timeout=5
            )

        return result

    def test_hdmi(self) -> TestResult:
        """HDMI 接口检测（仅适用于支持 HDMI 的芯片）"""
        logger.info(f"[{self.sn}] 开始检测 HDMI 接口...")
        result = TestResult()
        result.set_testing()

        try:
            # 检查芯片是否支持 HDMI
            if not self.chip_config.has_hdmi:
                logger.info(f"[{self.sn}] 芯片类型 {self.chip_config.chip_model} 不支持 HDMI，跳过检测")
                result.set_passed("跳过检测", f"{self.chip_config.chip_model} 芯片无 HDMI 接口")
                return result

            # 执行 HDMI 检测
            cmd = "cat /sys/class/drm/card0-HDMI-A-1/status"
            success, stdout, stderr = self.ssh.execute_command(cmd, timeout=10)

            if success and stdout:
                status = stdout.strip().lower()
                logger.info(f"[{self.sn}] HDMI 状态: {status}")
                
                if status == "connected":
                    result.set_passed("HDMI 已连接", f"HDMI 接口状态: {status}")
                    logger.info(f"[{self.sn}] HDMI 检测通过")
                else:
                    result.set_failed("HDMI 未连接", f"HDMI 接口状态: {status}")
                    logger.warning(f"[{self.sn}] HDMI 检测失败: {status}")
            else:
                result.set_failed("检测失败", f"无法读取 HDMI 状态: {stderr}")
                logger.error(f"[{self.sn}] HDMI 检测失败: {stderr}")
        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] HDMI 检测异常: {e}")

        return result

    # ---- 电池检测 ----

    # I2C 地址 0x0b 的电量计寄存器（命令集与 SBS 智能电池规范一致）
    # (内部键, 寄存器, 显示名)；列表顺序即读取顺序与展示顺序
    #
    # 必读集合：检测结果要展示的字段全部落在这里，任一读失败即该检测项失败
    BATTERY_REGS = [
        ("absolute_soc", "0x0e", "绝对电量"),
        ("relative_soc", "0x0d", "相对电量"),
        ("temperature",  "0x08", "温度"),
        ("voltage",      "0x09", "电池电压"),
        ("design_cap",   "0x18", "额定容量"),
        ("remaining",    "0x0f", "当前容量"),
        ("current",      "0x0a", "电流"),
        ("status",       "0x16", "状态寄存器"),
    ]

    # 附加信息：读得到就一起展示，读不到只记日志，不影响判定。
    # 海思310P 的电量计不保证实现这两条命令（产线脚本也没读），
    # 所以不能算进必读集合，否则会因“多余寄存器读失败”把正常电池判成失败。
    BATTERY_REGS_OPTIONAL = [
        ("full_charge",  "0x10", "满充容量FCC"),
        ("cycle_count",  "0x17", "循环次数"),
    ]

    # 电池厂商名：SBS 命令集的 ManufacturerName(0x20)。
    # 该命令返回的是字符串（第 1 字节是名称长度，后面才是 ASCII 名称），
    # i2cget 只能按寄存器读一个 16 位字，读不到这种变长字符串，必须用
    # i2ctransfer 整段读：w1@0x0b 0x20 写 1 字节命令码，r33 一次读回
    # 1 字节长度 + 32 字节名称（厂商名最长 32 字节，33 字节足够）。
    #
    # 总线号与地址和寄存器读取取同一份配置（见 _battery_i2c_bus），
    # 保证同一个电量计不会读一半走 i2c-2、读一半走 i2c-0。
    BATTERY_MANUFACTURER_CMD = "i2ctransfer -y {bus} w1@{addr} 0x20 r33"
    BATTERY_MANUFACTURER_MAX_LEN = 32

    # 状态寄存器(0x16)位定义。按 SBS 1.1 BatteryStatus 解读，
    # 注意：实测机台返回 0x00E0，低字节 0xE0 不在标准错误码表内，位定义待核对，
    # 因此这里只做“尽力解读”，详情里始终保留原始十六进制值供人工核对。
    BATTERY_STATUS_BITS = [
        (15, "过充告警"),
        (14, "充电终止告警"),
        (13, "过温告警"),
        (12, "放电终止告警"),
        (10, "已放空"),
        (9,  "已充满"),
    ]
    BATTERY_ERROR_CODES = {
        0x00: "正常",
        0x01: "忙",
        0x03: "过温/低温",
        0x04: "过压/欠压",
        0x05: "过流",
    }

    def _battery_i2c_bus(self) -> int:
        """当前芯片的电量计所在 I2C 总线号

        优先取芯片级 battery_i2c_bus（各芯片主板走线不同：RK3576 是 i2c-0，
        海思310P 是 i2c-2），芯片未配置时回落到全局 battery.i2c_bus。
        """
        chip_bus = getattr(self.chip_config, 'battery_i2c_bus', None)
        if chip_bus is not None:
            try:
                return int(chip_bus)
            except (TypeError, ValueError):
                logger.warning(f"[{self.sn}] 芯片配置 battery_i2c_bus={chip_bus} 非法，"
                               f"回落到全局 battery.i2c_bus")
        return config.battery_i2c_bus

    def _read_battery_registers(self) -> tuple:
        """一次性读取全部电池寄存器

        单条 SSH 命令读全部寄存器（必读 + 附加），并给每个读数打上寄存器号标记：
        只走一次往返，且 i2cget 失败时 stderr 也被 2>&1 收进同一行，
        标记后取不到合法的 0x???? 即视为该项失败。

        I2C 总线号取芯片级配置（见 _battery_i2c_bus），从机地址来自
        config.yaml 的 battery.i2c_addr，默认 0x0b。

        Returns:
            (readings, errors, optional_readings):
                readings            必读寄存器 {key: 原始整数}
                errors              必读失败项 {key: 失败原因}
                optional_readings   附加寄存器 {key: 原始整数}（读不到就不在字典里）
        """
        bus = self._battery_i2c_bus()
        addr = config.battery_i2c_addr
        all_regs = self.BATTERY_REGS + self.BATTERY_REGS_OPTIONAL
        regs = " ".join(reg for _, reg, _ in all_regs)
        cmd = (f"for r in {regs}; do printf '%s=' \"$r\"; "
               f"i2cget -y {bus} {addr} $r w 2>&1; done")
        success, stdout, stderr = self.ssh.execute_command(cmd, timeout=30)

        required_keys = [key for key, _, _ in self.BATTERY_REGS]

        if not success or not stdout:
            reason = stderr.strip() or "命令执行失败"
            # 把总线和地址带进失败原因：读不到时排查第一步就是确认 i2c-N 是否存在，
            # 否则现场只看到“命令执行失败”无从下手
            reason = f"i2c-{bus} {addr} 读取失败: {reason}"
            logger.error(f"[{self.sn}] 电池寄存器读取失败: {reason}")
            return {}, {key: reason for key in required_keys}, {}

        logger.info(f"[{self.sn}] 电池寄存器原始输出 (i2c-{bus} {addr}):\n{stdout}")

        raw_values = {}
        for line in stdout.splitlines():
            match = re.match(r'^(0x[0-9a-fA-F]{2,4})\s*=\s*(0x[0-9a-fA-F]{4})\s*$', line.strip())
            if match:
                raw_values[match.group(1).lower()] = int(match.group(2), 16)

        readings, errors, optional_readings = {}, {}, {}
        for key, reg, label in self.BATTERY_REGS:
            if reg.lower() in raw_values:
                readings[key] = raw_values[reg.lower()]
            else:
                errors[key] = f"{label}({reg}) 读数失败"
                logger.warning(f"[{self.sn}] 电池 {label}({reg}) 读数失败")

        # 附加项读不到只记日志，不计入失败
        for key, reg, label in self.BATTERY_REGS_OPTIONAL:
            if reg.lower() in raw_values:
                optional_readings[key] = raw_values[reg.lower()]
            else:
                logger.info(f"[{self.sn}] 电池 {label}({reg}) 无读数，跳过附加信息")

        return readings, errors, optional_readings

    def _read_battery_manufacturer(self) -> tuple:
        """读取电池厂商名（SBS ManufacturerName 0x20）

        这条命令返回的是变长字符串，i2cget 按寄存器读 16 位的用法读不出来，
        必须走 i2ctransfer 整段读（命令见 BATTERY_MANUFACTURER_CMD）。

        总线号与从机地址和寄存器读取共用同一份配置，避免同一个电量计
        寄存器走一条总线、厂商名走另一条。

        Returns:
            (name, error):
                name   解析出的厂商名；取不到时为空字符串
                error  失败原因；成功时为空字符串
        """
        bus = self._battery_i2c_bus()
        addr = config.battery_i2c_addr
        cmd = self.BATTERY_MANUFACTURER_CMD.format(bus=bus, addr=addr)

        try:
            success, stdout, stderr = self.ssh.execute_command(cmd, timeout=10)
        except Exception as e:
            logger.warning(f"[{self.sn}] 电池厂商读取异常: {e}")
            return "", f"电池厂商读取异常: {e}"

        if not success:
            reason = stderr.strip() or "命令执行失败"
            logger.warning(f"[{self.sn}] 电池厂商读取失败 (i2c-{bus} {addr}): {reason}")
            return "", f"电池厂商读取失败: {reason}"

        logger.info(f"[{self.sn}] 电池厂商原始输出 (i2c-{bus} {addr}): {stdout.strip()}")
        name, error = self._decode_battery_manufacturer(stdout)
        if error:
            logger.warning(f"[{self.sn}] 电池厂商解析失败: {error}")
        else:
            logger.info(f"[{self.sn}] 电池厂商: {name}")
        return name, error

    def _decode_battery_manufacturer(self, raw: str) -> tuple:
        """解析 i2ctransfer 读回的厂商名数据

        输出形如 `0x08 0x47 0x72 0x65 0x65 0x6e 0x77 0x61 0x79`：
        首字节是名称长度（0x08），其后按 ASCII 解码。

        数据不完整时必须显式报错，不能按现有字节硬解：机台偶尔会返回被截断的
        短输出（如只有 `0x08 0x47`），硬解会显示成厂商名「G」，比读不到更误导现场。

        Returns:
            (name, error)
        """
        tokens = re.findall(r'0x([0-9a-fA-F]{1,2})', raw or "")
        if not tokens:
            return "", "厂商名输出无法识别"

        data = [int(t, 16) for t in tokens]
        length = data[0]
        if length <= 0 or length > self.BATTERY_MANUFACTURER_MAX_LEN:
            return "", f"厂商名长度非法: {length}"
        if len(data) - 1 < length:
            return "", (f"厂商名数据不完整: 声明长度 {length} 字节，"
                        f"实际只有 {len(data) - 1} 字节")
        name_bytes = data[1:1 + length]
        if any(b < 0x20 or b > 0x7E for b in name_bytes):
            return "", f"厂商名含非 ASCII 字节: {raw.strip()}"
        return bytes(name_bytes).decode('ascii'), ""

    @staticmethod

    def _to_signed16(value: int) -> int:
        """16 位补码转有符号整数（电流寄存器用）"""
        return value - 0x10000 if value >= 0x8000 else value

    def _decode_battery_status(self, status: int) -> tuple:
        """解读状态寄存器，返回 (摘要文本, 明细文本)

        位定义为按 SBS 规范推测，实测值待核对，故明细里保留原始十六进制。
        """
        flags = [name for bit, name in self.BATTERY_STATUS_BITS if status & (1 << bit)]
        error_code = status & 0xFF
        error_text = self.BATTERY_ERROR_CODES.get(error_code, f"未知(0x{error_code:02X})")

        summary = "、".join(flags) if flags else "无标志位"
        detail = (f"原始值: 0x{status:04X}\n"
                  f"置位: {summary}\n"
                  f"错误码(低字节): {error_text}")
        return summary, detail

    def test_battery(self) -> TestResult:
        """电池检测：i2cget 读取电量计寄存器

        判定标准：默认能读出全部必读数据即通过，任一必读寄存器读失败则失败。

        检测结果展示字段（顺序固定）：
            电池厂商、充电状态、绝对电量、相对电量、温度(℃)、电池电压、额定容量、当前容量、电流
        电池厂商走 i2ctransfer 单独读（SBS ManufacturerName，字符串命令 i2cget
        读不出来），读不到只在详情里标注，不影响该项判定。

        检测通过后是否继续配置 shutdown 模式由芯片配置 battery_shutdown_config
        决定（该流程只适配 RK3576 电量计，海思310P 需设 False 跳过）。
        """
        logger.info(f"[{self.sn}] 开始电池检测...")
        result = TestResult()
        result.set_testing()

        try:
            if not self.chip_config.battery_detection:
                logger.info(f"[{self.sn}] 芯片类型 {self.chip_config.chip_model}，电池检测默认通过")
                result.set_passed("电池检测跳过", f"{self.chip_config.chip_model} 芯片电池检测默认通过")
                return result

            readings, errors, optional_readings = self._read_battery_registers()

            if errors:
                detail = "\n".join(errors.values())
                result.set_failed(
                    f"电池读数失败 ({len(errors)}/{len(self.BATTERY_REGS)} 个寄存器)", detail)
                logger.warning(f"[{self.sn}] 电池检测失败:\n{detail}")
                return result

            # 单位换算
            relative_soc = readings['relative_soc']
            absolute_soc = readings['absolute_soc']
            voltage_mv = readings['voltage']
            current_ma = self._to_signed16(readings['current'])
            temperature = readings['temperature'] / 10.0 - 273.15   # 0.1K → ℃
            rated_capacity = readings['design_cap']
            current_capacity = readings['remaining']
            status = readings['status']

            status_summary, status_detail = self._decode_battery_status(status)

            # 充电状态：以电流符号为准，再叠加状态寄存器的充满/放空标志
            if current_ma > 0:
                charge_state = "充电中"
            elif current_ma < 0:
                charge_state = "放电中"
            else:
                charge_state = "静置"
            extra_flags = [f for f in ("已充满", "已放空") if f in status_summary]
            if extra_flags:
                charge_state += "、" + "、".join(extra_flags)
            if status & (1 << 15) or status & (1 << 13):
                charge_state += "（存在告警标志）"

            # 电池厂商：字符串命令，单独走 i2ctransfer。
            # 读不到不算失败（部分电量计不实现 ManufacturerName），
            # 只把原因记在详情里，判定仍以必读寄存器为准。
            manufacturer, manufacturer_error = self._read_battery_manufacturer()

            # 面板展示：字段按固定顺序拼成一行，过长时界面靠 tooltip 看全文
            manufacturer_text = manufacturer if manufacturer else "未读到"
            message = (
                f"电池厂商: {manufacturer_text} | 充电状态: {charge_state} | "
                f"绝对电量: {absolute_soc}% | 相对电量: {relative_soc}% | "
                f"温度: {temperature:.1f}℃ | 电池电压: {voltage_mv} mV | "
                f"额定容量: {rated_capacity} mAh | 当前容量: {current_capacity} mAh | 电流: {current_ma} mA"
            )

            detail_lines = [
                f"充电状态: {charge_state}",
                f"绝对电量: {absolute_soc}%",
                f"相对电量: {relative_soc}%",
                f"温度: {temperature:.1f}℃ (原始 0x{readings['temperature']:04X} = 0.1K)",
                f"电池电压: {voltage_mv} mV",
                f"额定容量: {rated_capacity} mAh",
                f"当前容量: {current_capacity} mAh",
                f"电流: {current_ma} mA (原始 0x{readings['current']:04X})",
            ]
            if 'full_charge' in optional_readings:
                detail_lines.append(f"满充容量FCC: {optional_readings['full_charge']} mAh")
            if 'cycle_count' in optional_readings:
                detail_lines.append(f"循环次数: {optional_readings['cycle_count']}")
            detail_lines.append(f"状态寄存器: {status_summary}")
            detail_lines.append(status_detail)
            # 厂商名放在最前面：详情里字段顺序与面板展示保持一致
            detail_lines.insert(0, f"电池厂商: {manufacturer}" if manufacturer
                                else f"电池厂商: 未读到 ({manufacturer_error})")
            detail = "\n".join(detail_lines)

            result.set_passed(message, detail)
            logger.info(f"[{self.sn}] 电池检测通过: {message}")
            logger.info(f"[{self.sn}] 电池检测详情:\n{detail}")

            # 电池检测通过后按芯片配置决定是否配置 shutdown 模式
            if self.chip_config.battery_shutdown_config:
                # 传入厂商名：休眠配置的地址字按品牌区分（格瑞普 0xD04B / 九夷 0x8B48）
                summary = self._configure_battery_shutdown_mode(manufacturer)
                shutdown_lines = summary.get('lines') or []
                if summary.get('configured'):
                    detail += '\n\n休眠配置: 已完成\n' + '\n'.join(shutdown_lines)
                    result.detail = detail
                else:
                    # 配置没完成不是电池检测失败，但必须让人看见，
                    # 否则现场会以为休眠模式已经配好（旧版的隐性问题）
                    detail += '\n\n休眠配置: 未完成\n' + '\n'.join(shutdown_lines)
                    result.detail = detail
                    message = f"{message} ｜ 休眠配置未完成"
                    result.message = message
                    logger.warning(f"[{self.sn}] 电池休眠配置未完成，已标注在检测结果里")
            else:
                logger.info(f"[{self.sn}] 芯片 {self.chip_config.chip_model} "
                            f"不做 shutdown 模式配置，跳过解密/使能/休眠流程")
                detail += '\n\n休眠配置: 跳过（芯片配置 battery_shutdown_config=false）'
                result.detail = detail

        except Exception as e:
            result.set_failed("电池检测异常", str(e))
            logger.error(f"[{self.sn}] 电池检测异常: {e}", exc_info=True)

        return result

    def _configure_battery_shutdown_mode(self, manufacturer: str = '') -> dict:
        """电池检测通过后自动配置 shutdown 模式

        流程：
        1. 解密执行两遍（全解）
        2. 判定并配置 Auto Ship Bit 位（未开启才写）
        3. 判定并配置关机时间（不是 3 天才写）
        4. 上锁，并回读确认（上锁后 0x44 应为 0x00）

        **地址字按电池品牌选择**（见 src/core/battery_profile.py）：
        格瑞普是 0xD04B / 0x044C，九夷是 0x8B48 / 0xAF48。旧实现把格瑞普的
        地址字写死，换九夷后读不到、写错地址，现场却只看到「已配置」的日志。

        每一步写完都**回读校验**，结论不再是「命令退出码为 0 就算成功」：
        退出码为 0 只说明 i2cset 发出去了，不代表地址字对、值真的落盘。

        厂商名认不出品牌时**直接跳过并写进详情**：往不匹配的地址字写数据
        会把电量计配置写坏，宁可跳过让人工处理。

        Returns:
            {'configured': bool, 'lines': [详情行], 'profile': 档案名或 ''}
        """
        bus = self._battery_i2c_bus()
        addr = config.battery_i2c_addr
        lines: List[str] = []
        summary = {'configured': False, 'lines': lines, 'profile': ''}

        def fail(reason: str) -> dict:
            lines.append(f'结果: 未完成 ({reason})')
            logger.warning(f"[{self.sn}] 电池 shutdown 配置未完成: {reason}")
            return summary

        try:
            logger.info(f"[{self.sn}] 开始配置电池 shutdown 模式...")

            # 步骤0：确定地址字（品牌）
            profile, note = battery_profile.resolve_profile(config, manufacturer)
            if profile is None:
                logger.warning(f"[{self.sn}] {note}")
                return fail(note)

            summary['profile'] = profile.name
            logger.info(f"[{self.sn}] 休眠配置档案: {profile.label} ({note})")
            lines.append(f'配置档案: {profile.label}')
            lines.append(f'来源: {note}')

            # 步骤1：解密（两遍全解）
            success, _, stderr = self.ssh.execute_command(
                battery_profile.build_unlock(bus, addr), timeout=20)
            if not success:
                return fail(f'解密失败: {(stderr or "").strip() or "命令非零退出"}')
            lines.append('解密: 成功（两遍全解）')
            logger.info(f"[{self.sn}] 电池解密成功")

            # 步骤2：Auto Ship Bit
            enable_ok, reason = self._ensure_autoship(bus, addr, profile)
            lines.append(f'Auto Ship Bit: {reason}')
            if not enable_ok:
                return fail(f'Auto Ship Bit: {reason}')

            # 步骤3：关机时间 3 天
            time_ok, reason = self._ensure_shutdown_time(bus, addr, profile)
            lines.append(f'关机时间: {reason}')
            if not time_ok:
                return fail(f'关机时间: {reason}')

            # 步骤4：上锁 + 回读确认
            lock_ok, reason = self._lock_and_verify(bus, addr)
            lines.append(f'上锁: {reason}')
            if not lock_ok:
                return fail(f'上锁: {reason}')

            lines.append('结果: 配置完成（Auto Ship + 关机时间均已回读校验，上锁命令成功）')
            summary['configured'] = True
            logger.info(f"[{self.sn}] 电池 shutdown 模式配置完成 ({profile.name})")
            return summary

        except Exception as e:
            logger.error(f"[{self.sn}] 配置电池 shutdown 模式异常: {e}", exc_info=True)
            return fail(f'异常: {e}')

    def _ensure_autoship(self, bus: int, addr: str, profile) -> tuple:
        """判定 Auto Ship Bit，未开启才写，写完回读校验

        Returns:
            (ok, 说明)
        """
        read_cmd = battery_profile.build_read_autoship(bus, addr, profile)
        success, stdout, stderr = self.ssh.execute_command(read_cmd, timeout=15)
        if not success:
            return False, f'读取失败: {(stderr or "").strip() or "命令非零退出"}'

        logger.info(f"[{self.sn}] Auto Ship Bit 读数: {stdout.strip()}")
        enabled, reason = battery_profile.autoship_state(stdout, profile)
        if enabled:
            logger.info(f"[{self.sn}] Auto Ship Bit {reason}，跳过写入")
            return True, f'{reason}，无需写入'

        # 未开启（或前缀异常）→ 写入后必须回读确认为 0x01
        logger.info(f"[{self.sn}] Auto Ship Bit {reason}，写入 0x01 0x00")
        write_cmd = battery_profile.build_enable_autoship(bus, addr, profile)
        success, _, stderr = self.ssh.execute_command(write_cmd, timeout=15)
        if not success:
            return False, f'写入失败: {(stderr or "").strip() or "命令非零退出"}'

        time.sleep(battery_profile.READBACK_SETTLE_SECONDS)
        success, stdout, stderr = self.ssh.execute_command(read_cmd, timeout=15)
        if not success:
            return False, f'写入后回读失败: {(stderr or "").strip() or "命令非零退出"}'

        logger.info(f"[{self.sn}] Auto Ship Bit 写入后回读: {stdout.strip()}")
        enabled, verify = battery_profile.autoship_state(stdout, profile)
        if enabled:
            return True, f'写入后校验通过 ({verify})'
        return False, f'写入后校验失败 ({verify})，地址字 {profile.autoship_word_text} 可能不匹配'

    def _ensure_shutdown_time(self, bus: int, addr: str, profile) -> tuple:
        """判定关机时间，非 3 天才写，写完回读校验

        Returns:
            (ok, 说明)
        """
        read_cmd = battery_profile.build_read_shutdown_time(bus, addr, profile)
        success, stdout, stderr = self.ssh.execute_command(read_cmd, timeout=15)
        if not success:
            return False, f'读取失败: {(stderr or "").strip() or "命令非零退出"}'

        logger.info(f"[{self.sn}] 关机时间读数: {stdout.strip()}")
        configured, reason = battery_profile.shutdown_time_state(stdout, profile)
        if configured:
            logger.info(f"[{self.sn}] 关机时间{reason}，跳过写入")
            return True, f'{reason}，无需写入'

        logger.info(f"[{self.sn}] 关机时间{reason}，写入 0xE0 0x10")
        write_cmd = battery_profile.build_set_shutdown_time(bus, addr, profile)
        success, _, stderr = self.ssh.execute_command(write_cmd, timeout=15)
        if not success:
            return False, f'写入失败: {(stderr or "").strip() or "命令非零退出"}'

        time.sleep(battery_profile.READBACK_SETTLE_SECONDS)
        success, stdout, stderr = self.ssh.execute_command(read_cmd, timeout=15)
        if not success:
            return False, f'写入后回读失败: {(stderr or "").strip() or "命令非零退出"}'

        logger.info(f"[{self.sn}] 关机时间写入后回读: {stdout.strip()}")
        configured, verify = battery_profile.shutdown_time_state(stdout, profile)
        if configured:
            return True, f'写入后校验通过 ({verify})'
        return False, f'写入后校验失败 ({verify})，地址字 {profile.shutdown_word_text} 可能不匹配'

    def _lock_and_verify(self, bus: int, addr: str) -> tuple:
        """上锁

        发送上锁命令，命令退出码为 0 即视为成功。
        上锁后 0x44 回读值不固定（实测为 0x16），**不做回读校验**。

        Returns:
            (ok, 说明)
        """
        success, _, stderr = self.ssh.execute_command(
            battery_profile.build_lock(bus, addr), timeout=15)
        if not success:
            return False, f'上锁命令失败: {(stderr or "").strip() or "命令非零退出"}'
        return True, '已上锁（命令退出码 0）'

    def run_all_tests(self) -> DeviceTestResults:
        if not self.connect():
            logger.error(f"[{self.sn}] SSH 连接失败，无法执行测试")
            return self.results

        try:
            self.results.usb = self.test_usb3()
            self.results.poe = self.test_poe_ports()
            self.results.disk = self.test_disk()
            self.results.lte_4g = self.test_4g()
            self.results.wifi = self.test_wifi()
            self.results.bluetooth = self.test_bluetooth()
            self.results.nearlink = self.test_nearlink()
            self.results.fan = self.test_fan()
            # HDMI 检测（仅适用于支持的芯片）
            if self.chip_config.has_hdmi:
                self.results.hdmi = self.test_hdmi()
        finally:
            self.disconnect()

        return self.results
