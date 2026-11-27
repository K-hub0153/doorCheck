from copy import deepcopy
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)
from ..domain.models import ITEMS
from ..domain.policies import validate_plan
from ..domain.diagnostics import lte_thresholds
from ..infrastructure.config import save_config
from .styles import STYLE


class SettingsForm(QFormLayout):
    """各项目共用列宽和间距，显式覆盖不同平台的表单对齐默认值。"""
    def __init__(self, parent):
        super().__init__(parent)
        self.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.setRowWrapPolicy(QFormLayout.DontWrapRows)
        self.setContentsMargins(16, 22, 16, 16)
        self.setHorizontalSpacing(20)
        self.setVerticalSpacing(12)

    def addRow(self, *args):
        if len(args) == 2 and isinstance(args[0], str):
            label = QLabel(args[0])
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            label.setFixedWidth(230)
            label.setWordWrap(True)
            field = args[1]
            field.setMinimumHeight(34)
            if isinstance(field, (QSpinBox, QDoubleSpinBox)):
                field.setMaximumWidth(200)
                field.setAlignment(Qt.AlignLeft)
            args = (label, field)
        super().addRow(*args)


class PlanDialog(QDialog):
    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.setStyleSheet(STYLE)
        self.plan = deepcopy(plan)
        self.plan.pop('operator', None)
        self.plan.pop('station', None)
        self.plan.pop('max_parallel_devices', None)
        plan = self.plan
        self.setWindowTitle('配置主控老化计划')
        self.resize(880, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('项目并行运行；电池充放电串行。准备、人工等待与数据缺口不计有效时长。'))
        tabs = self.tabs = QTabWidget()
        layout.addWidget(tabs)
        general = self.tab(tabs, '项目与老化柜')
        settings = self.tab(tabs, '项目设置')
        safety = self.tab(tabs, '保护（选填）')
        form = QFormLayout(general)
        self.table = QTableWidget(len(ITEMS), 3)
        self.table.setHorizontalHeaderLabels(['启用', '项目', '有效时长（分钟）'])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(260)
        self.checks, self.durations = {}, {}
        for row, (key, label) in enumerate(ITEMS.items()):
            check = QCheckBox()
            check.setChecked(plan['items'][key]['enabled'])
            duration = QSpinBox()
            duration.setRange(0 if key == 'battery' else 1, 2 * 43200 * 1000 if key == 'battery' else 43200)
            duration.setValue(plan['items'][key]['duration_min'])
            if key == 'battery':
                duration.setReadOnly(True)
                duration.setButtonSymbols(QSpinBox.NoButtons)
                duration.setToolTip('由充电分钟、放电分钟和循环数自动计算')
            self.table.setCellWidget(row, 0, check)
            cell = QTableWidgetItem(label)
            cell.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 1, cell)
            self.table.setCellWidget(row, 2, duration)
            self.checks[key], self.durations[key] = check, duration
        form.addRow(self.table)
        select = QCheckBox('全选项目')
        select.toggled.connect(lambda value: [c.setChecked(value) for c in self.checks.values()])
        form.addRow(select)
        self.charge = self.integer(plan['items']['battery']['charge_duration_min'], 0, 43200)
        self.discharge = self.integer(plan['items']['battery']['discharge_duration_min'], 0, 43200)
        self.cycles = self.integer(plan['items']['battery']['cycles'], 1, 1000)
        for widget in (self.charge, self.discharge, self.cycles):
            widget.valueChanged.connect(self.update_battery_duration)
        self.update_battery_duration()
        battery_row = QHBoxLayout()
        for label, widget in [('充电分钟', self.charge), ('放电分钟', self.discharge), ('循环数', self.cycles)]:
            battery_row.addWidget(QLabel(label))
            battery_row.addWidget(widget)
        form.addRow('电池串行阶段', battery_row)
        form.addRow(QLabel('充电分钟为 0 时只放电，放电分钟为 0 时只充电；两者不能同时为 0。'))
        env = plan.get('environment', {})
        self.cabinet = QLineEdit(str(env.get('cabinet_id') or ''))
        form.addRow('老化柜编号', self.cabinet)
        options = QVBoxLayout(settings)
        settings.setObjectName('planSettingsPage')
        settings.setAttribute(Qt.WA_StyledBackground, True)
        options.setContentsMargins(16, 16, 16, 20)
        options.setSpacing(16)
        execution = self.project_group(options, '数据采集')
        self.interval = self.integer(plan['sample_interval_s'], 1, 60)
        execution.addRow('数据采集间隔（秒）', self.interval)
        cpu_form = self.project_group(options, 'CPU 负载', 'cpu')
        self.cpu_target = self.integer(plan['items']['cpu']['target_busy_pct'], 1, 100)
        cpu_form.addRow('忙碌率通过下限（%）', self.cpu_target)
        self.cpu_grace = self.decimal(plan['items']['cpu']['grace_s'], 0, 600)
        cpu_form.addRow('启动宽限（秒）', self.cpu_grace)
        ddr_form = self.project_group(options, 'DDR 负载', 'ddr')
        self.ddr_ratio = self.integer(plan['items']['ddr']['max_memory_pct'], 1, 70)
        ddr_form.addRow('内存使用比例（%）', self.ddr_ratio)
        self.ddr_reserve = self.decimal(plan['items']['ddr']['reserve_mib'], 128, 1048576)
        ddr_form.addRow('系统保留内存（MiB）', self.ddr_reserve)
        self.settings_note(ddr_form, '预期：内存正确性校验无错误；实际分配内存须驻留并锁页，不发生交换。')
        storage_form = self.project_group(options, 'SSD/eMMC 读写', 'storage')
        storage = plan['items']['storage']
        self.storage_targets = QLineEdit(', '.join(storage.get('targets') or []))
        self.storage_targets.setPlaceholderText('例如 /data 或 /mnt/emmc；多个目录用英文逗号分隔')
        self.storage_budget = QDoubleSpinBox()
        self.storage_budget.setRange(0.01, 4096)
        self.storage_budget.setDecimals(2)
        self.storage_budget.setValue(storage.get('write_budget_gib', 8))
        self.storage_chunk = self.integer(storage.get('chunk_mib', 256), 1, 4096)
        self.storage_rounds = self.integer(storage.get('rounds', 31), 1, 100000)
        storage_form.addRow('设备上的测试目录', self.storage_targets)
        storage_form.addRow('每盘写入上限（GiB）', self.storage_budget)
        storage_form.addRow('每轮测试容量（MiB）', self.storage_chunk)
        storage_form.addRow('写入轮数', self.storage_rounds)
        self.settings_note(storage_form, '预期：完成全部写入轮数，direct I/O 读回校验一致，无 I/O 错误。')
        battery_form = self.project_group(options, '电池判定', 'battery')
        self.completion = QComboBox()
        self.completion.addItem('按设定时长完成', 'fixed_duration')
        self.completion.addItem('等待充放电停止节点（设定时长为上限）', 'stop_node')
        self.completion.setCurrentIndex(max(0, self.completion.findData(plan['items']['battery']['completion_mode'])))
        battery_form.addRow('完成方式', self.completion)
        battery = plan['items']['battery']
        self.stop_current = self.decimal(battery['stop_current_ma'], 0, 100000)
        self.stop_samples = self.integer(battery['stop_confirm_samples'], 1, 60)
        self.read_failures = self.integer(battery['max_read_failures'], 1, 60)
        battery_form.addRow('停止节点电流绝对值 ≤（mA）', self.stop_current)
        battery_form.addRow('停止节点连续确认样本数', self.stop_samples)
        battery_form.addRow('连续读取失败上限', self.read_failures)
        self.settings_note(battery_form, '停止节点仍需核验 SOC / 电池状态；固定时长模式持续监测至时长达标。')
        lte_form = self.project_group(options, '4G 信号预期下限', 'lte')
        limits = lte_thresholds(plan['items']['lte'])
        self.lte_fields = {}
        for key, label, low, high in (('rsrp', 'RSRP ≥（dBm）', -140, -44),
                                      ('rsrq', 'RSRQ ≥（dB）', -40, 0),
                                      ('sinr', 'SINR ≥（dB）', -30, 50)):
            widget = QDoubleSpinBox()
            widget.setRange(low, high)
            widget.setDecimals(1)
            widget.setValue(limits[key])
            self.lte_fields[key] = widget
            lte_form.addRow(label, widget)
        self.settings_note(lte_form, '预检和运行期间使用同一门限；任一指标缺失或低于下限均不通过。')
        options.addStretch()
        protection = plan.get('protection', {})
        controls = QFormLayout(safety)
        controls.addRow(QLabel('保护门限和人工确认均为选填；留空或不勾选时仍可执行对应老化项目。'))
        self.protection_fields, self.protection_enabled = {}, {}
        for key, label, low, high in [
            ('cpu_stop_temperature_c', 'CPU 停止温度 ℃（可选）', 0, 150),
            ('battery_min_voltage_mv', '电池最低电压 mV', 0, 100000),
            ('battery_max_voltage_mv', '电池最高电压 mV', 0, 100001),
            ('battery_max_abs_current_ma', '电池最大净电流绝对值 mA', 0, 100000),
            ('discharge_min_soc_pct', '放电最低 SOC %', 0, 99)]:
            spin = QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setValue(protection.get(key) or 0)
            spin.setSpecialValueText('请输入')
            self.protection_fields[key] = spin
            toggle = QCheckBox(label.replace('（可选）', ''))
            toggle.setChecked(protection.get(key) is not None)
            toggle.toggled.connect(spin.setEnabled)
            spin.setEnabled(toggle.isChecked())
            self.protection_enabled[key] = toggle
            controls.addRow(toggle, spin)
        self.ranges = {}
        for key, label in [('battery_charge_temperature_range_c', '充电温度范围 ℃'),
                           ('battery_discharge_temperature_range_c', '放电温度范围 ℃')]:
            limits = protection.get(key)
            lower = QLineEdit('' if limits is None else str(limits[0]))
            upper = QLineEdit('' if limits is None else str(limits[1]))
            lower.setPlaceholderText('最低温度')
            upper.setPlaceholderText('最高温度')
            row = QHBoxLayout()
            row.addWidget(lower)
            row.addWidget(QLabel('至'))
            row.addWidget(upper)
            toggle = QCheckBox(label)
            toggle.setChecked(limits is not None)
            for field in (lower, upper):
                toggle.toggled.connect(field.setEnabled)
                field.setEnabled(toggle.isChecked())
            self.protection_enabled[key] = toggle
            controls.addRow(toggle, row)
            self.ranges[key] = (lower, upper, label)
        self.direction = QCheckBox('已验证电池净电流：充电为正，放电为负')
        self.direction.setChecked(plan['items']['battery'].get('current_direction_confirmed', False))
        controls.addRow(self.direction)
        controls.addRow(QLabel('电池阶段切换时，按主界面提示接入或断开电源适配器。'))
        controls.addRow(QLabel('已启用的电池保护触发后，请按提示接回适配器。'))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('保存计划')
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.save_plan)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def tab(tabs, title):
        area = QScrollArea()
        area.setWidgetResizable(True)
        page = QWidget()
        area.setWidget(page)
        tabs.addTab(area, title)
        return page

    def project_group(self, layout, title, key=None):
        group = QGroupBox(title)
        group.setObjectName('planSection')
        if key is not None:
            group.setEnabled(self.checks[key].isChecked())
            self.checks[key].toggled.connect(group.setEnabled)
        layout.addWidget(group)
        return SettingsForm(group)

    @staticmethod
    def settings_note(form, text):
        label = QLabel(text)
        label.setObjectName('planHelp')
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        form.addRow(label)

    @staticmethod
    def decimal(value, low, high):
        widget = QDoubleSpinBox()
        widget.setRange(low, high)
        widget.setDecimals(2)
        widget.setValue(value)
        return widget

    @staticmethod
    def integer(value, low, high):
        widget = QSpinBox()
        widget.setRange(low, high)
        widget.setValue(value)
        return widget

    def update_battery_duration(self):
        """电池有效时长是阶段时长与循环数的派生值，禁止单独编辑。"""
        self.durations['battery'].setValue((self.charge.value() + self.discharge.value()) * self.cycles.value())

    def save_plan(self):
        try:
            plan = deepcopy(self.plan)
            for key in ITEMS:
                plan['items'][key].update(enabled=self.checks[key].isChecked(), duration_min=self.durations[key].value())
            plan['items']['battery'].update(charge_duration_min=self.charge.value(),
                discharge_duration_min=self.discharge.value(), cycles=self.cycles.value(),
                duration_min=(self.charge.value() + self.discharge.value()) * self.cycles.value(),
                current_direction_confirmed=self.direction.isChecked(), completion_mode=self.completion.currentData())
            plan['sample_interval_s'] = self.interval.value()
            plan['max_sample_gap_s'] = max(plan['max_sample_gap_s'], self.interval.value() * 2)
            plan['items']['cpu']['target_busy_pct'] = self.cpu_target.value()
            plan['items']['cpu']['grace_s'] = self.cpu_grace.value()
            plan['items']['ddr']['max_memory_pct'] = self.ddr_ratio.value()
            plan['items']['ddr']['reserve_mib'] = self.ddr_reserve.value()
            plan['items']['lte']['thresholds'] = {key: widget.value() for key, widget in self.lte_fields.items()}
            plan['items']['battery'].update(stop_current_ma=self.stop_current.value(),
                stop_confirm_samples=self.stop_samples.value(), max_read_failures=self.read_failures.value())
            storage = plan['items']['storage']
            storage['targets'] = [target.strip() for target in self.storage_targets.text().split(',') if target.strip()]
            storage['write_budget_gib'] = self.storage_budget.value()
            storage['chunk_mib'] = self.storage_chunk.value()
            storage['rounds'] = self.storage_rounds.value()
            protection = plan.setdefault('protection', {})
            for key, widget in self.protection_fields.items():
                protection[key] = widget.value() if self.protection_enabled[key].isChecked() else None
            for key, (lower, upper, label) in self.ranges.items():
                protection[key] = None
                if self.protection_enabled[key].isChecked():
                    try:
                        protection[key] = [float(lower.text()), float(upper.text())]
                    except ValueError:
                        raise ValueError(label + '需填写最低和最高温度')
            plan.pop('station_isolation_confirmed', None)
            plan['environment'] = {'cabinet_id': self.cabinet.text().strip() or None}
            validate_plan(plan)
            save_config(plan)
            self.plan = plan
            self.accept()
        except OSError as exc:
            QMessageBox.warning(self, '计划保存失败', '无法写入计划配置，请检查配置目录的写入权限：\n' + str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, '计划不完整', str(exc))
