import json
import re
import subprocess
import time
import os
from ..utils.paths import config_dir

from src.master_control.core.ssh_client import SSHClient
from src.master_control.core.test_result import DeviceTestResults, TestResult
from src.master_control.core.chip_config import ChipManager, ChipConfig
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

    def test_poe_ports(self) -> TestResult:
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

            poe_subnet = config.poe_subnet.split('/')[0].rsplit('.', 1)[0]
            logger.info(f"[{self.sn}] POE网段: {poe_subnet}.x")

            cmd_clear = "ip neigh flush all"
            self.ssh.execute_command(cmd_clear)
            logger.info(f"[{self.sn}] 已清空ARP表")

            cmd_ping = f"for i in {{2..254}}; do ping -c 1 -W 1 {poe_subnet}.$i > /dev/null 2>&1 & done; wait"
            success, stdout, stderr = self.ssh.execute_command(cmd_ping, timeout=30)
            logger.info(f"[{self.sn}] 完成网段ping扫描")

            cmd_arp_detail = f"ip neigh show | grep '{poe_subnet}'"
            success_detail, arp_detail, stderr_detail = self.ssh.execute_command(cmd_arp_detail)
            if success_detail:
                logger.info(f"[{self.sn}] ARP表详情:\n{arp_detail}")

            cmd_arp = f"ip neigh show | grep '{poe_subnet}' | grep 'REACHABLE' | wc -l"
            success, stdout, stderr = self.ssh.execute_command(cmd_arp)

            if success and stdout:
                online_count = int(stdout.strip())
                logger.info(f"[{self.sn}] POE设备检测结果: 发现 {online_count} 个REACHABLE设备，期望 {expected} 个")

                if online_count >= expected:
                    result.set_passed(f"通过 (检测到 {online_count} 路/目标 {expected} 路)", f"检测到 {online_count} 个POE设备在线")
                    logger.info(f"[{self.sn}] POE 检测通过: {online_count}/{expected}")
                else:
                    result.set_failed(f"失败 (检测到 {online_count} 路/目标 {expected} 路)", f"仅检测到 {online_count} 个POE设备，期望 {expected} 个")
                    logger.warning(f"[{self.sn}] POE 检测失败: {online_count}/{expected}")
            else:
                result.set_failed("执行失败", stderr)
                logger.error(f"[{self.sn}] POE 检测命令执行失败: {stderr}")
        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] POE 检测异常: {e}")

        return result

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
            # 使用 --pairs 输出 KEY=VALUE 格式，避免列宽/空格导致解析错位
            cmd = "lsblk -d -n -P -o NAME,MODEL,SIZE | grep -E 'NAME=\"(sd|nvme|mmcblk)'"
            success, stdout, stderr = self.ssh.execute_command(cmd)

            if success and stdout:
                logger.info(f"[{self.sn}] lsblk 原始输出: {stdout}")
                disk_lines = stdout.strip().split('\n')
                disk_info_list = []

                for line in disk_lines:
                    if not line.strip():
                        continue

                    # 解析 KEY="VALUE" 格式：NAME="sda" MODEL="WD Blue" SIZE="500G"
                    import re as _re
                    name_match = _re.search(r'NAME="([^"]+)"', line)
                    model_match = _re.search(r'MODEL="([^"]*)"', line)
                    size_match = _re.search(r'SIZE="([^"]+)"', line)

                    name = name_match.group(1) if name_match else '未知设备'
                    model = model_match.group(1).strip() if model_match and model_match.group(1).strip() else name
                    size = size_match.group(1) if size_match else '未知大小'

                    logger.info(f"[{self.sn}] 解析硬盘: 设备={name}, 型号={model}, 大小={size}")

                    # 从型号中提取品牌
                    vendor = '未知品牌'
                    if model != '未知型号':
                        model_upper = model.upper()
                        if 'SAMSUNG' in model_upper:
                            vendor = 'Samsung'
                        elif 'WD' in model_upper or 'WESTERN' in model_upper:
                            vendor = 'Western Digital'
                        elif 'SEAGATE' in model_upper or 'ST' in model_upper:
                            vendor = 'Seagate'
                        elif 'TOSHIBA' in model_upper:
                            vendor = 'Toshiba'
                        elif 'KINGSTON' in model_upper:
                            vendor = 'Kingston'
                        elif 'SANDISK' in model_upper:
                            vendor = 'SanDisk'
                        elif 'INTEL' in model_upper:
                            vendor = 'Intel'
                        elif 'OSC' in model_upper:
                            vendor = 'OSC'

                    disk_info_list.append({
                        'name': name,
                        'vendor': vendor,
                        'model': model,
                        'size': size
                    })
                    logger.info(f"[{self.sn}] 解析硬盘信息: 品牌={vendor}, 型号={model}, 大小={size}")

                if disk_info_list:
                    # 查询固件版本（smartctl -a /dev/xxx）
                    for info in disk_info_list:
                        info['firmware'] = self._get_disk_firmware(info['name'])

                    # 构建简洁的消息显示
                    disk_summary = []
                    for info in disk_info_list:
                        fw = f", FW:{info['firmware']}" if info.get('firmware') else ""
                        disk_summary.append(f"{info['model']} ({info['size']}{fw})")

                    result_message = f"检测到 {len(disk_info_list)} 个硬盘: {', '.join(disk_summary)}"
                    result_detail = "\n".join([
                        f"硬盘{i+1}: 品牌={info['vendor']}, 型号={info['model']}, 大小={info['size']}, "
                        f"固件版本={info.get('firmware') or '未获取'}"
                        for i, info in enumerate(disk_info_list)
                    ])
                    result.set_passed(result_message, result_detail)
                    logger.info(f"[{self.sn}] 硬盘检测通过: {result_message}")
                    logger.info(f"[{self.sn}] 硬盘详情: {result_detail}")
                else:
                    result.set_failed("未检测到硬盘", "没有找到有效的存储设备")
                    logger.error(f"[{self.sn}] 硬盘检测失败: 未找到存储设备")
            else:
                result.set_failed("检测命令执行失败", stderr)
                logger.error(f"[{self.sn}] 硬盘检测失败: 命令执行失败")
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
                # hisi 和 RK3576 使用统一的 ws73-modules 切换产测模式
                logger.info(f"[{self.sn}] {self.chip_config.chip_type} 芯片，执行 ws73-modules switch factory...")
                success, stdout, stderr = self.ssh.execute_command("ws73-modules switch factory", timeout=120)
                logger.info(f"[{self.sn}] ws73-modules 输出:\n{stdout}")
                if success:
                    details.append("产测模式已加载 (ws73-modules)")
                else:
                    logger.warning(f"[{self.sn}] ws73-modules 切换失败: {stderr}")
                    details.append(f"产测模式切换警告: {stderr}")
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

    def _test_4g_qcsq(self) -> TestResult:
        """统一4G检测: 通过 AT+QENG="servingcell" 命令获取信号质量并按标准判定"""
        result = TestResult()
        try:
            # AT 指令必须以 \r 结尾，否则模组不响应；用 printf 发送，避免转义问题
            at_body = 'AT+QENG="servingcell"'
            # cat 后台读取 ttyUSB2，printf 发送带 \r 的 AT 命令，最后 kill 掉后台 cat 进程
            at_cmd = (
                'cat /dev/ttyUSB2 & CATPID=$!; sleep 1; '
                "printf '" + at_body + "\\r\\n' > /dev/ttyUSB2; "
                'sleep 1; kill $CATPID 2>/dev/null || true'
            )
            logger.info(f"[{self.sn}] 执行 {at_body} 命令...")
            sig_success, sig_stdout, sig_stderr = self.ssh.execute_command(at_cmd, timeout=15)

            rsrp_val = rsrq_val = sinr_val = None
            signal_info = []
            signal_pass = False

            if sig_success and sig_stdout:
                logger.info(f"[{self.sn}] AT+QENG输出: {sig_stdout.strip()}")

                # 只取 +QENG 的 servingcell 行，例如：
                # +QENG: "servingcell","NOCONN","LTE","FDD",460,00,1234567,123,1650,3,5,5,1234,-83,-11,-55,18,120
                qeng_line = next(
                    (ln.strip() for ln in sig_stdout.splitlines()
                     if '+QENG:' in ln and 'servingcell' in ln),
                    None
                )

                if qeng_line:
                    head = re.search(
                        r'\+QENG:\s*"servingcell"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"',
                        qeng_line
                    )
                    state        = head.group(1) if head else ''
                    network_mode = head.group(2) if head else ''
                    logger.info(f"[{self.sn}] 网络模式: {network_mode}, 状态: {state}")

                    if network_mode != "LTE":
                        # WCDMA / GSM 模式没有 RSRP/RSRQ/SINR，暂不判定
                        logger.warning(f"[{self.sn}] 当前网络模式 {network_mode} 不支持信号质量判定")
                    elif state not in ("NOCONN", "CONNECT", "CAMPED"):
                        logger.warning(f"[{self.sn}] 未注册到网络, state={state}")
                    else:
                        # 字段索引: 0="servingcell" 1=state 2=LTE 3=is_tdd 4=MCC 5=MNC 6=cellID
                        #          7=PCI 8=EARFCN 9=band 10=UL_bw 11=DL_bw 12=TAC
                        #         13=RSRP 14=RSRQ 15=RSSI 16=SINR 17=srxlev
                        fields = [f.strip().strip('"')
                                  for f in qeng_line.split('+QENG:', 1)[1].split(',')]
                        if len(fields) >= 17:
                            try:
                                rsrp_val = int(fields[13])
                                rsrq_val = int(fields[14])
                                rssi_val = int(fields[15])
                                sinr_val = int(fields[16])

                                signal_info = [
                                    f"RSRP:{rsrp_val}dBm",
                                    f"RSRQ:{rsrq_val}dB",
                                    f"SINR:{sinr_val}dB",
                                ]
                                # 判定标准：RSRP >= -90 dBm, SINR >= 5 dB, RSRQ >= -13 dB
                                if rsrp_val >= -90 and sinr_val >= 5 and rsrq_val >= -13:
                                    signal_pass = True
                                    logger.info(f"[{self.sn}] 4G信号质量达标: " + ", ".join(signal_info))
                                else:
                                    logger.warning(
                                        f"[{self.sn}] 4G信号质量不达标: "
                                        f"RSRP={rsrp_val}(需>=-90) "
                                        f"SINR={sinr_val}(需>=5) "
                                        f"RSRQ={rsrq_val}(需>=-13)"
                                    )
                            except (ValueError, IndexError) as e:
                                logger.warning(f"[{self.sn}] LTE 信号字段解析失败: {e}")
                        else:
                            logger.warning(f"[{self.sn}] LTE 响应字段数不足: {len(fields)}")
                else:
                    logger.warning(f"[{self.sn}] 未能解析AT+QENG输出: {sig_stdout.strip()}")
            else:
                logger.warning(f"[{self.sn}] AT+QENG命令执行失败: {sig_stderr}")

            if signal_pass:
                message = "4G 正常 (" + ", ".join(signal_info) + ")"
                detail  = f"RSRP: {rsrp_val} dBm\nSINR: {sinr_val} dB\nRSRQ: {rsrq_val} dB"
                result.set_passed(message, detail)
                logger.info(f"[{self.sn}] 4G 检测通过: {message}")
            else:
                if signal_info:
                    fail_msg    = "4G 信号不达标 (" + ", ".join(signal_info) + ")"
                    fail_detail = (f"RSRP: {rsrp_val} dBm (需>=-90)\n"
                                   f"SINR: {sinr_val} dB (需>=5)\n"
                                   f"RSRQ: {rsrq_val} dB (需>=-13)")
                else:
                    fail_msg    = "4G 信号检测失败"
                    fail_detail = "无法获取信号质量数据"
                result.set_failed(fail_msg, fail_detail)
                logger.warning(f"[{self.sn}] 4G 检测失败: {fail_msg}")

        except Exception as e:
            result.set_failed("异常", str(e))
            logger.error(f"[{self.sn}] 4G 检测异常: {e}")

        return result

    def test_4g(self) -> TestResult:
        logger.info(f"[{self.sn}] 开始检测 4G 模块...")
        result = TestResult()
        result.set_testing()
        # RK3576 和 310P/3576 主控统一使用 AT+QENG="servingcell" 检测
        return self._test_4g_qcsq()

    def _test_4g_rk3576(self) -> TestResult:
        """RK3576 芯片 4G 检测（统一使用 AT+QENG="servingcell"）"""
        return self._test_4g_qcsq()

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
    BATTERY_REGS = [
        ("relative_soc", "0x0d", "相对电量"),
        ("absolute_soc", "0x0e", "绝对电量"),
        ("remaining",    "0x0f", "剩余容量"),
        ("full_charge",  "0x10", "满充容量FCC"),
        ("design_cap",   "0x18", "设计容量DC"),
        ("temperature",  "0x08", "温度"),
        ("voltage",      "0x09", "电压"),
        ("current",      "0x0a", "电流"),
        ("status",       "0x16", "状态寄存器"),
        ("cycle_count",  "0x17", "循环次数"),
    ]

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

    def _read_battery_registers(self) -> tuple:
        """一次性读取全部电池寄存器

        单条 SSH 命令读全部寄存器，并给每个读数打上寄存器号标记：
        只走一次往返，且 i2cget 失败时 stderr 也被 2>&1 收进同一行，
        标记后取不到合法的 0x???? 即视为该项失败。

        I2C 总线号与从机地址来自 config.yaml 的 battery 段，默认 0 / 0x0b。

        Returns:
            (readings, errors): readings 为 {key: 原始整数}，errors 为 {key: 失败原因}
        """
        bus = config.battery_i2c_bus
        addr = config.battery_i2c_addr
        regs = " ".join(reg for _, reg, _ in self.BATTERY_REGS)
        cmd = (f"for r in {regs}; do printf '%s=' \"$r\"; "
               f"i2cget -y {bus} {addr} $r w 2>&1; done")
        success, stdout, stderr = self.ssh.execute_command(cmd, timeout=30)

        if not success or not stdout:
            reason = stderr.strip() or "命令执行失败"
            logger.error(f"[{self.sn}] 电池寄存器读取失败: {reason}")
            return {}, {key: reason for key, _, _ in self.BATTERY_REGS}

        logger.info(f"[{self.sn}] 电池寄存器原始输出:\n{stdout}")

        raw_values = {}
        for line in stdout.splitlines():
            match = re.match(r'^(0x[0-9a-fA-F]{2,4})\s*=\s*(0x[0-9a-fA-F]{4})\s*$', line.strip())
            if match:
                raw_values[match.group(1).lower()] = int(match.group(2), 16)

        readings, errors = {}, {}
        for key, reg, label in self.BATTERY_REGS:
            if reg.lower() in raw_values:
                readings[key] = raw_values[reg.lower()]
            else:
                errors[key] = f"{label}({reg}) 读数失败"
                logger.warning(f"[{self.sn}] 电池 {label}({reg}) 读数失败")
        return readings, errors

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

        判定标准：默认能读出全部数据即通过，任一寄存器读失败则失败。
        """
        logger.info(f"[{self.sn}] 开始电池检测...")
        result = TestResult()
        result.set_testing()

        try:
            if not self.chip_config.battery_detection:
                logger.info(f"[{self.sn}] 芯片类型 {self.chip_config.chip_model}，电池检测默认通过")
                result.set_passed("电池检测跳过", f"{self.chip_config.chip_model} 芯片电池检测默认通过")
                return result

            readings, errors = self._read_battery_registers()

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
            cycle_count = readings['cycle_count']
            status = readings['status']

            status_summary, status_detail = self._decode_battery_status(status)

            # 充放电状态：以电流符号为准，再叠加状态寄存器的充满/放空标志
            if current_ma > 0:
                state = f"充电中 (电流 {current_ma} mA)"
            elif current_ma < 0:
                state = f"放电中 (电流 {current_ma} mA)"
            else:
                state = "静置 (电流 0 mA)"
            extra_flags = [f for f in ("已充满", "已放空") if f in status_summary]
            if extra_flags:
                state += "、" + "、".join(extra_flags)
            if status & (1 << 15) or status & (1 << 13):
                state += "，注意：存在告警标志"

            message = (
                f"电池正常 ({state} | 相对电量: {relative_soc}% | 绝对电量: {absolute_soc}% | "
                f"电压: {voltage_mv} mV | 温度: {temperature:.1f}℃ | 循环次数: {cycle_count})"
            )
            detail = "\n".join([
                f"充放电状态: {state}",
                f"相对电量: {relative_soc}%",
                f"绝对电量: {absolute_soc}%",
                f"剩余容量: {readings['remaining']} mAh",
                f"满充容量FCC: {readings['full_charge']} mAh",
                f"设计容量DC: {readings['design_cap']} mAh",
                f"电压: {voltage_mv} mV",
                f"电流: {current_ma} mA (原始 0x{readings['current']:04X})",
                f"温度: {temperature:.1f}℃ (原始 0x{readings['temperature']:04X} = 0.1K)",
                f"循环次数: {cycle_count}",
                f"状态寄存器: {status_summary}",
                status_detail,
            ])
            result.set_passed(message, detail)
            logger.info(f"[{self.sn}] 电池检测通过: {message}")
            logger.info(f"[{self.sn}] 电池检测详情:\n{detail}")

            # 电池检测通过后自动配置 shutdown 模式
            self._configure_battery_shutdown_mode()

        except Exception as e:
            result.set_failed("电池检测异常", str(e))
            logger.error(f"[{self.sn}] 电池检测异常: {e}", exc_info=True)

        return result

    def _configure_battery_shutdown_mode(self):
        """电池检测后自动配置 shutdown 模式
        
        流程：
        1. 解密执行两遍（全解）
        2. 判定并配置 Auto Ship Bit 位（如未开启则开启）
        3. 判定并配置关机时间（如非 3 天则配置为 3 天）
        4. 上锁
        """
        bus = config.battery_i2c_bus
        addr = config.battery_i2c_addr
        
        try:
            logger.info(f"[{self.sn}] 开始配置电池 shutdown 模式...")
            
            # 步骤1：解密执行两遍（全解）
            unlock_cmd = (
                f"i2cset -y {bus} {addr} 0x00 0x88 0x20 i && "
                f"i2cset -y {bus} {addr} 0x00 0x26 0x20 i && "
                f"i2cset -y {bus} {addr} 0x00 0xFF 0xFF i && "
                f"i2cset -y {bus} {addr} 0x00 0xFF 0xFF i && "
                f"i2cset -y {bus} {addr} 0x00 0x88 0x20 i && "
                f"i2cset -y {bus} {addr} 0x00 0x26 0x20 i && "
                f"i2cset -y {bus} {addr} 0x00 0xFF 0xFF i && "
                f"i2cset -y {bus} {addr} 0x00 0xFF 0xFF i"
            )
            success, stdout, stderr = self.ssh.execute_command(unlock_cmd, timeout=15)
            if not success:
                logger.warning(f"[{self.sn}] 电池解密失败: {stderr}")
                return
            logger.info(f"[{self.sn}] 电池解密成功")
            
            # 步骤2：检查并配置 Auto Ship Bit 位
            # 2.1 读取当前值（期望 0x22 0xd0 0x4b 0x01 0x00）
            read_autoship_cmd = (
                f"i2cset -y {bus} {addr} 0x44 0x02 0xD0 0x4B i && "
                f"i2cget -y {bus} {addr} 0x44 i 5"
            )
            success, stdout, stderr = self.ssh.execute_command(read_autoship_cmd, timeout=10)
            if not success:
                logger.warning(f"[{self.sn}] 读取 Auto Ship Bit 失败: {stderr}")
                return
                
            # 解析输出，期望看到 "0x01"（第4个字节，tokens 索引为 3）
            logger.info(f"[{self.sn}] Auto Ship Bit 读数: {stdout.strip()}")
            tokens = stdout.strip().split()
            
            # 2.2 判断是否需要开启（期望第4个字节为 0x01，即 tokens[3]）
            need_enable = True
            if len(tokens) >= 5:
                try:
                    # tokens: [0x22, 0xd0, 0x4b, 0x01, 0x00]
                    # 第4个字节（索引3）应为 0x01
                    if tokens[3].lower() == '0x01':
                        need_enable = False
                        logger.info(f"[{self.sn}] Auto Ship Bit 已开启，跳过")
                except (ValueError, IndexError):
                    pass
            
            if need_enable:
                enable_autoship_cmd = (
                    f"i2cset -y {bus} {addr} 0x44 0x02 0xD0 0x4B i && "
                    f"i2cset -y {bus} {addr} 0x44 0x04 0xD0 0x4B 0x01 0x00 i"
                )
                success, stdout, stderr = self.ssh.execute_command(enable_autoship_cmd, timeout=10)
                if success:
                    logger.info(f"[{self.sn}] Auto Ship Bit 已开启")
                else:
                    logger.warning(f"[{self.sn}] Auto Ship Bit 开启失败: {stderr}")
                    return
            
            # 步骤3：检查并配置关机时间（3天）
            # 3.1 读取当前关机时间（期望 0x04 0x4C 0xE0 0x10）
            read_shutdown_time_cmd = (
                f"i2cset -y {bus} {addr} 0x44 0x02 0x04 0x4C i && "
                f"i2cget -y {bus} {addr} 0x44 i 5"
            )
            success, stdout, stderr = self.ssh.execute_command(read_shutdown_time_cmd, timeout=10)
            if not success:
                logger.warning(f"[{self.sn}] 读取关机时间失败: {stderr}")
                return
                
            logger.info(f"[{self.sn}] 关机时间读数: {stdout.strip()}")
            tokens = stdout.strip().split()
            
            # 3.2 判断是否需要配置（期望前4字节为 0x04 0x4C 0xE0 0x10）
            need_config = True
            if len(tokens) >= 4:
                try:
                    # tokens: [0x04, 0x4C, 0xE0, 0x10, 0x??]（5字节，判前4个）
                    if (tokens[0].lower() == '0x04' and tokens[1].lower() == '0x4c' and
                        tokens[2].lower() == '0xe0' and tokens[3].lower() == '0x10'):
                        need_config = False
                        logger.info(f"[{self.sn}] 关机时间已配置为 3 天，跳过")
                except (ValueError, IndexError):
                    pass
            
            if need_config:
                set_shutdown_time_cmd = (
                    f"i2cset -y {bus} {addr} 0x44 0x02 0x04 0x4C i && "
                    f"i2cset -y {bus} {addr} 0x44 0x04 0x04 0x4C 0xE0 0x10 i"
                )
                success, stdout, stderr = self.ssh.execute_command(set_shutdown_time_cmd, timeout=10)
                if success:
                    logger.info(f"[{self.sn}] 关机时间已配置为 3 天")
                else:
                    logger.warning(f"[{self.sn}] 关机时间配置失败: {stderr}")
                    return
            
            # 步骤4：执行上锁
            lock_cmd = f"i2cset -y {bus} {addr} 0x44 0x02 0x30 0x00 i"
            success, stdout, stderr = self.ssh.execute_command(lock_cmd, timeout=10)
            if success:
                logger.info(f"[{self.sn}] 电池配置上锁成功")
                logger.info(f"[{self.sn}] 电池 shutdown 模式配置完成")
            else:
                logger.warning(f"[{self.sn}] 电池配置上锁失败: {stderr}")
                
        except Exception as e:
            logger.error(f"[{self.sn}] 配置电池 shutdown 模式异常: {e}", exc_info=True)

    def run_all_tests(self) -> DeviceTestResults:
        if not self.connect():
            logger.error(f"[{self.sn}] SSH 连接失败，无法执行测试")
            return self.results

        try:
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
