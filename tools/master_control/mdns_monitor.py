#!/usr/bin/env python3
"""
mDNS消息监控工具
实时打印主控和门锁之间的mDNS交互
"""
import time
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
from datetime import datetime


class MdnsMonitor(ServiceListener):
    def __init__(self):
        self.discovered_services = {}
    
    def _print_timestamp(self):
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]
    
    def add_service(self, zc: Zeroconf, type_: str, name: str):
        """服务添加事件"""
        timestamp = self._print_timestamp()
        print(f"\n[{timestamp}] 📡 mDNS事件: 服务添加")
        print(f"  类型: {type_}")
        print(f"  名称: {name}")
        
        info = zc.get_service_info(type_, name)
        if info:
            self._print_service_info(info, "新服务")
            self.discovered_services[name] = info
    
    def update_service(self, zc: Zeroconf, type_: str, name: str):
        """服务更新事件"""
        timestamp = self._print_timestamp()
        print(f"\n[{timestamp}] 🔄 mDNS事件: 服务更新")
        print(f"  类型: {type_}")
        print(f"  名称: {name}")
        
        info = zc.get_service_info(type_, name)
        if info:
            self._print_service_info(info, "更新服务")
            self.discovered_services[name] = info
    
    def remove_service(self, zc: Zeroconf, type_: str, name: str):
        """服务移除事件"""
        timestamp = self._print_timestamp()
        print(f"\n[{timestamp}] ❌ mDNS事件: 服务移除")
        print(f"  类型: {type_}")
        print(f"  名称: {name}")
        
        if name in self.discovered_services:
            del self.discovered_services[name]
    
    def _print_service_info(self, info, label):
        """打印服务详细信息"""
        print(f"\n  ═══ {label} ═══")
        print(f"  服务名称: {info.name}")
        print(f"  服务类型: {info.type}")
        print(f"  端口: {info.port}")
        
        addresses = info.parsed_addresses()
        if addresses:
            print(f"  IP地址: {', '.join(addresses)}")
        
        if info.properties:
            print(f"  属性:")
            for key, value in info.properties.items():
                try:
                    k = key.decode('utf-8')
                    v = value.decode('utf-8')
                    print(f"    {k}: {v}")
                except:
                    print(f"    {key}: {value}")
        
        print(f"  ═══════════════")


def main():
    """主程序"""
    print("=" * 70)
    print("mDNS消息监控工具")
    print("=" * 70)
    print("\n监听以下服务类型:")
    print("  1. _master._tcp.local.  (主控)")
    print("  2. _mqtt._tcp.local.    (门锁设备)")
    print("\n开始监听...\n")
    
    zeroconf = Zeroconf()
    monitor = MdnsMonitor()
    
    browser1 = ServiceBrowser(zeroconf, "_master._tcp.local.", monitor)
    browser2 = ServiceBrowser(zeroconf, "_mqtt._tcp.local.", monitor)
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n停止监听...")
    finally:
        zeroconf.close()
        print("已退出")


if __name__ == "__main__":
    main()
