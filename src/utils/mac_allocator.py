import requests
import csv
import os
import sys
from typing import Optional, Dict
from ..utils.logger import logger
import threading


class MACAllocator:
    def __init__(self, api_url: str = "http://ishop-oqa.weidian.com/api/mac/allocate"):
        self.api_url = api_url
        self.timeout = 10
        self.api_key = "WD_MAC_ALLOC_SECRET"
        
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
        else:
            exe_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "dist")
        
        self.csv_file = os.path.join(exe_dir, "mac_pool.csv")
        self.lock = threading.Lock()
        self.use_csv = os.path.exists(self.csv_file)
    
    def allocate_mac(self, sn: str, module_type: str = "WS73") -> Optional[Dict]:
        if self.use_csv:
            return self._allocate_from_csv(sn, module_type)
        else:
            return self._allocate_from_api(sn, module_type)
    
    def _allocate_from_csv(self, sn: str, module_type: str) -> Optional[Dict]:
        try:
            with self.lock:
                rows = []
                allocated_mac = None
                
                with open(self.csv_file, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        rows.append(row)
                        if allocated_mac is None and row['status'] != '1':
                            allocated_mac = {
                                'wifi_mac': row['wifi'],
                                'starflash_mac': row['sle']
                            }
                            row['status'] = '1'
                
                if allocated_mac is None:
                    logger.error("CSV文件中没有可用的MAC地址")
                    return None
                
                with open(self.csv_file, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=['wifi', 'sle', 'status'])
                    writer.writeheader()
                    writer.writerows(rows)
                
                logger.info(f"从CSV文件分配MAC地址成功:")
                logger.info(f"  SN: {sn}")
                logger.info(f"  模块类型: {module_type}")
                logger.info(f"  WiFi MAC: {allocated_mac['wifi_mac']}")
                logger.info(f"  星闪 MAC: {allocated_mac['starflash_mac']}")
                
                return {
                    "sn": sn,
                    "module_type": module_type,
                    "wifi_mac": allocated_mac['wifi_mac'],
                    "bluetooth_mac": None,
                    "starflash_mac": allocated_mac['starflash_mac'],
                    "is_new_allocation": True
                }
                
        except Exception as e:
            logger.error(f"从CSV文件分配MAC地址失败: {e}")
            return None
    
    def _allocate_from_api(self, sn: str, module_type: str) -> Optional[Dict]:
        try:
            headers = {
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "*/*",
                "User-Agent": "hornChecker/1.0"
            }
            
            payload = {
                "sn": sn,
                "moduleType": module_type
            }
            
            logger.info(f"请求MAC地址分配: SN={sn}, 模块类型={module_type}")
            
            response = requests.post(self.api_url, json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()

            logger.debug(f"API响应状态码: {response.status_code}")
            logger.debug(f"API响应内容: {response.text}")

            data = response.json()
            
            if data.get("code") == 200:
                mac_data = data.get("data", {})
                sn = mac_data.get("sn")
                wifi_mac = mac_data.get("wifiMac")
                bluetooth_mac = mac_data.get("bluetoothMac")
                starflash_mac = mac_data.get("sparkMac")
                module_type = mac_data.get("moduleType")
                is_new = mac_data.get("isNewAllocation", False)
                
                logger.info(f"MAC地址分配成功:")
                logger.info(f"  SN: {sn}")
                logger.info(f"  模块类型: {module_type}")
                logger.info(f"  WiFi MAC: {wifi_mac}")
                logger.info(f"  蓝牙 MAC: {bluetooth_mac}")
                logger.info(f"  星闪 MAC: {starflash_mac}")
                logger.info(f"  是否新分配: {is_new}")
                
                return {
                    "sn": sn,
                    "module_type": module_type,
                    "wifi_mac": wifi_mac,
                    "bluetooth_mac": bluetooth_mac,
                    "starflash_mac": starflash_mac,
                    "is_new_allocation": is_new
                }
            else:
                error_msg = data.get("message", "未知错误")
                logger.error(f"MAC地址分配失败: {error_msg}")
                return None
                
        except requests.exceptions.Timeout:
            logger.error(f"MAC地址分配请求超时")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"MAC地址分配请求失败: {e}")
            return None
        except Exception as e:
            logger.error(f"MAC地址分配异常: {e}")
            return None
