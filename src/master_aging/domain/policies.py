"""PC 与设备端共同执行的计划校验，所有单位在边界处显式转换。"""
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
        raise ValueError('计划必须包含 CPU、DDR、SSD/eMMC、4G 和电池五个项目的配置')
    if any(not isinstance(v.get('enabled'), bool) for v in items.values()):
        raise ValueError('项目 enabled 必须为布尔值')
    enabled = [k for k, v in items.items() if v.get('enabled')]
    if not enabled or any(k not in ITEMS for k in enabled):
        raise ValueError('至少选择一项已知测试项目')
    for key in enabled:
        if key != 'battery':
            number(items[key].get('duration_min'), 1, 43200, ITEMS[key] + '分钟数', True)
    number(plan.get('sample_interval_s'), 1, 60, '采样间隔', True)
    number(plan.get('min_free_space_mib'), 64, 1048576, '最小剩余空间 MiB', True)
    number(plan.get('max_log_mib'), 8, 4096, '日志上限 MiB', True)
    number(plan.get('max_sample_gap_s'), plan['sample_interval_s'] * 2, 600, '最大采样缺口秒数')
    protection = plan.get('protection', {})
    if protection.get('cpu_stop_temperature_c') is not None:
        number(protection['cpu_stop_temperature_c'], 1, 150, 'CPU 停止温度 ℃')
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
        # 留空使用内置模组表；运行时核验串口占用，不要求蜂窝联网配置。
        port = lte.get('at_port')
        if port not in (None, '') and (not isinstance(port, str) or not re.fullmatch(r'/dev/tty(?:USB|ACM)\d+', port)):
            raise ValueError('4G AT 口必须为 /dev/ttyUSB数字 或 /dev/ttyACM数字，留空自动识别')
    if 'battery' in enabled:
        battery = items['battery']
        for field in ('charge_duration_min', 'discharge_duration_min'):
            number(battery.get(field), 0, 43200, field, True)
        if battery['charge_duration_min'] == 0 and battery['discharge_duration_min'] == 0:
            raise ValueError('电池充电分钟和放电分钟不能同时为 0，至少保留一个阶段')
        number(battery.get('duration_min'), 1, 2 * 43200 * 1000, '电池分钟数', True)
        number(battery.get('cycles'), 1, 1000, '电池循环数', True)
        number(battery.get('stop_current_ma'), 0, 100000, '电池停止电流 mA')
        for key in ('stop_confirm_samples', 'max_read_failures'):
            number(battery.get(key), 1, 60, key, True)
        if battery.get('completion_mode') not in ('fixed_duration', 'stop_node'):
            raise ValueError('未知电池完成模式')
        for key, label in (('battery_charge_temperature_range_c', '电池充电温度范围 ℃'),
                           ('battery_discharge_temperature_range_c', '电池放电温度范围 ℃')):
            limits = protection.get(key)
            if limits is None:
                continue
            if not isinstance(limits, list) or len(limits) != 2:
                raise ValueError(label + '需填写最低和最高温度')
            number(limits[0], -50, 100, label + '下限')
            number(limits[1], limits[0] + 0.1, 120, label + '上限')
        for key, low, high, label in (
                ('battery_min_voltage_mv', 1, 100000, '电池最低电压 mV'),
                ('battery_max_voltage_mv', 1, 100001, '电池最高电压 mV'),
                ('battery_max_abs_current_ma', 1, 100000, '电池最大电流 mA'),
                ('discharge_min_soc_pct', 1, 99, '放电保护 SOC %')):
            if protection.get(key) is not None:
                number(protection[key], low, high, label)
        minimum = protection.get('battery_min_voltage_mv')
        maximum = protection.get('battery_max_voltage_mv')
        if minimum is not None and maximum is not None and maximum <= minimum:
            raise ValueError('电池最高电压必须大于最低电压')
        if battery.get('configure_shutdown'):
            raise ValueError('尚未确认上锁回读规范，老化流程禁止写入电池休眠配置')
    return plan


def planned_seconds(plan):
    items = plan['items']
    parallel = max([v['duration_min'] * 60 for k, v in items.items()
                    if v.get('enabled') and k != 'battery'] or [0])
    b = items.get('battery', {})
    battery = ((b['charge_duration_min'] + b['discharge_duration_min']) * 60 * b['cycles']
               if b.get('enabled') else 0)
    return max(parallel, battery)
