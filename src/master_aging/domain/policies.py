"""PC 与设备端共同执行的计划校验，所有单位在边界处显式转换。"""
import ipaddress
import json
import math
import re
from .models import ITEMS


def number(value, low, high, name, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high
            or (integer and int(value) != value)):
        raise ValueError('%s 必须为 %s～%s%s' % (name, low, high, ' 的整数' if integer else ''))


def validate_plan(plan):
    try:
        json.dumps(plan, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('计划必须为有限数值和可序列化字段: ' + str(exc))
    if plan.get('schema_version') != 1:
        raise ValueError('不支持的计划版本')
    items = plan.get('items', {})
    if set(items) != set(ITEMS) or any(not isinstance(v, dict) for v in items.values()):
        raise ValueError('计划必须包含全部七个项目的配置')
    if any(not isinstance(v.get('enabled'), bool) for v in items.values()):
        raise ValueError('项目 enabled 必须为布尔值')
    enabled = [k for k, v in items.items() if v.get('enabled')]
    if not enabled or any(k not in ITEMS for k in enabled):
        raise ValueError('至少选择一项已知测试项目')
    for key in enabled:
        number(items[key].get('duration_min'), 1, 43200, ITEMS[key] + '分钟数', True)
    number(plan.get('sample_interval_s'), 1, 60, '采样间隔', True)
    number(plan.get('max_parallel_devices'), 1, 32, '并行设备数', True)
    number(plan.get('min_free_space_mib'), 64, 1048576, '最小剩余空间 MiB', True)
    number(plan.get('max_log_mib'), 8, 4096, '日志上限 MiB', True)
    number(plan.get('max_sample_gap_s'), plan['sample_interval_s'] * 2, 600, '最大采样缺口秒数')
    protection = plan.get('protection', {})
    # 即使只测接口也可能运行于温箱内，设备必须有可验证的本地温度保护。
    number(protection.get('cpu_stop_temperature_c'), 1, 150, 'CPU 停止温度 ℃')
    if 'cpu' in enabled:
        number(items['cpu'].get('target_busy_pct'), 1, 100, 'CPU 利用率目标 %')
        number(items['cpu'].get('grace_s'), 0, 600, 'CPU 启动宽限秒数')
    if 'ddr' in enabled:
        number(items['ddr'].get('max_memory_pct'), 1, 70, 'DDR 内存比例 %')
        number(items['ddr'].get('reserve_mib'), 128, 1048576, '系统保留内存 MiB')
    if 'storage' in enabled:
        storage = items['storage']
        if not storage.get('targets'):
            raise ValueError('存储测试需要明确目标目录，可用英文逗号分隔多个内置盘目录')
        for target in storage['targets']:
            if not isinstance(target, str) or not target.startswith('/') or target.startswith(('/dev/', '/proc/', '/sys/')):
                raise ValueError('存储目标必须为内置盘上的绝对目录，禁止设备节点')
        number(storage.get('write_budget_gib'), 0.01, 4096, '每盘批准写入预算 GiB')
        number(storage.get('chunk_mib'), 1, 4096, '单轮写入 MiB', True)
        number(storage.get('rounds'), 1, 100000, '写入轮数', True)
        number(storage.get('log_reserve_mib'), plan['max_log_mib'], 8192, '存储日志预留 MiB')
        number(storage.get('metadata_reserve_mib'), 1, 8192, '存储元数据预留 MiB')
        if storage.get('cumulative_write_budget_gib') is not None:
            number(storage['cumulative_write_budget_gib'], storage['write_budget_gib'], 1048576, '该盘累计批准预算 GiB')
        planned_bytes = (storage['rounds'] * storage['chunk_mib'] + storage['log_reserve_mib'] + storage['metadata_reserve_mib']) * 2**20
        if planned_bytes > storage['write_budget_gib'] * 2**30:
            raise ValueError('计划写入量超过每盘批准预算')
        if storage.get('direct') is not True:
            raise ValueError('首期存储校验要求已验证的 direct I/O，不自动降级为缓存模式')
    if 'lte' in enabled:
        lte = items['lte']
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,32}', lte.get('data_interface', '')):
            raise ValueError('4G 必须指定蜂窝数据接口')
        if not re.fullmatch(r'/dev/tty(?:USB|ACM)\d+', lte.get('at_port', '')) or not lte.get('diagnostic_port_confirmed'):
            raise ValueError('4G 需要确认独占诊断 AT 口，不能占用拨号服务串口')
        if not lte.get('probe_targets') or len(lte['probe_targets']) > 4:
            raise ValueError('4G 需要 1～4 个已批准的 IPv4 探测目标')
        for target in lte['probe_targets']:
            ipaddress.IPv4Address(target)
    if 'usb' in enabled:
        if not items['usb'].get('fixture_confirmed'):
            raise ValueError('请确认已连接 USB 3.x 测试夹具')
        number(items['usb'].get('min_speed_mbps'), 5000, 40000, 'USB 最低速率 Mbps')
    if 'battery' in enabled:
        battery = items['battery']
        for field in ('charge_duration_min', 'discharge_duration_min'):
            number(battery.get(field), 1, 43200, field, True)
        number(battery.get('cycles'), 1, 1000, '电池循环数', True)
        number(battery.get('start_current_ma'), 1, 100000, '电池启动电流 mA')
        number(battery.get('stop_current_ma'), 0, battery['start_current_ma'] - 1, '电池停止电流 mA')
        for key in ('start_confirm_samples', 'stop_confirm_samples', 'max_read_failures'):
            number(battery.get(key), 1, 60, key, True)
        number(battery.get('start_timeout_min'), 1, 120, '电流启动等待分钟数')
        if not battery.get('current_direction_confirmed'):
            raise ValueError('电池正负电流方向必须经样机接电/拔电验证')
        if battery.get('completion_mode') not in ('fixed_duration', 'stop_node'):
            raise ValueError('未知电池完成模式')
        for key in ('battery_charge_temperature_range_c', 'battery_discharge_temperature_range_c'):
            limits = protection.get(key)
            if not isinstance(limits, list) or len(limits) != 2:
                raise ValueError('缺少电池温度范围: ' + key)
            number(limits[0], -50, 100, key)
            number(limits[1], limits[0] + 0.1, 120, key)
        number(protection.get('battery_min_voltage_mv'), 1, 100000, '电池最低电压 mV')
        number(protection.get('battery_max_voltage_mv'), protection['battery_min_voltage_mv'] + 1, 100001, '电池最高电压 mV')
        number(protection.get('battery_max_abs_current_ma'), 1, 100000, '电池最大电流 mA')
        number(protection.get('discharge_min_soc_pct'), 1, 99, '放电保护 SOC %')
        if battery.get('configure_shutdown'):
            raise ValueError('尚未确认上锁回读规范，老化流程禁止写入电池休眠配置')
    if not plan.get('station_isolation_confirmed'):
        raise ValueError('请确认设备未被其他程序占用，避免并发操作设备')
    return plan


def planned_seconds(plan):
    items = plan['items']
    parallel = max([v['duration_min'] * 60 for k, v in items.items()
                    if v.get('enabled') and k != 'battery'] or [0])
    b = items.get('battery', {})
    battery = ((b['charge_duration_min'] + b['discharge_duration_min']) * 60 * b['cycles']
               if b.get('enabled') else 0)
    return max(parallel, battery)
