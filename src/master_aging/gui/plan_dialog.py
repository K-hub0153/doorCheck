from copy import deepcopy
import yaml
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPlainTextEdit, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)
from ..domain.models import ITEMS
from ..domain.policies import validate_plan
from .styles import STYLE


class PlanDialog(QDialog):
    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.setStyleSheet(STYLE)
        self.plan = deepcopy(plan)
        self.plan.pop('operator', None)
        self.plan.pop('station', None)
        plan = self.plan
        self.setWindowTitle('配置主控老化计划')
        self.resize(880, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('项目并行运行；电池充放电串行。准备、人工等待与数据缺口不计有效时长。'))
        tabs = QTabWidget()
        layout.addWidget(tabs)
        general, safety, advanced = QWidget(), QWidget(), QWidget()
        tabs.addTab(general, '项目与环境')
        tabs.addTab(safety, '保护与夹具')
        tabs.addTab(advanced, '详细参数')
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
            duration.setRange(1, 43200)
            duration.setValue(plan['items'][key]['duration_min'])
            duration.setEnabled(key != 'battery')
            self.table.setCellWidget(row, 0, check)
            cell = QTableWidgetItem(label)
            cell.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 1, cell)
            self.table.setCellWidget(row, 2, duration)
            self.checks[key], self.durations[key] = check, duration
        form.addRow(self.table)
        select = QCheckBox('全选项目（无 HDMI 的四代主控自动标记不适用）')
        select.toggled.connect(lambda value: [c.setChecked(value) for c in self.checks.values()])
        form.addRow(select)
        self.charge = self.integer(plan['items']['battery']['charge_duration_min'], 1, 43200)
        self.discharge = self.integer(plan['items']['battery']['discharge_duration_min'], 1, 43200)
        self.cycles = self.integer(plan['items']['battery']['cycles'], 1, 1000)
        battery_row = QHBoxLayout()
        for label, widget in [('充电分钟', self.charge), ('放电分钟', self.discharge), ('循环数', self.cycles)]:
            battery_row.addWidget(QLabel(label))
            battery_row.addWidget(widget)
        form.addRow('电池串行阶段', battery_row)
        env = plan.get('environment', {})
        self.chamber = QLineEdit(str(env.get('chamber_id') or ''))
        self.temperature = QLineEdit('' if env.get('set_temperature_c') is None else str(env['set_temperature_c']))
        self.humidity = QLineEdit('' if env.get('set_humidity_pct') is None else str(env['set_humidity_pct']))
        form.addRow('温箱编号', self.chamber)
        form.addRow('温箱设定温度 ℃（可空）', self.temperature)
        form.addRow('温箱设定湿度 %（可空）', self.humidity)
        form.addRow(QLabel('温箱设定值仅作为条件记录；实际温湿度无传感器时留空。'))
        protection = plan['protection']
        controls = QFormLayout(safety)
        controls.addRow(QLabel('请按已批准的板卡/电池规格填写保护门限，缺失时不能启动。'))
        self.protection_fields = {}
        for key, label, low, high in [
            ('cpu_stop_temperature_c', 'CPU 停止温度 ℃ *', 0, 150),
            ('battery_min_voltage_mv', '电池最低电压 mV', 0, 100000),
            ('battery_max_voltage_mv', '电池最高电压 mV', 0, 100001),
            ('battery_max_abs_current_ma', '电池最大净电流绝对值 mA', 0, 100000),
            ('discharge_min_soc_pct', '放电最低 SOC %', 0, 99)]:
            spin = QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setValue(protection.get(key) or 0)
            spin.setSpecialValueText('待填写')
            self.protection_fields[key] = spin
            controls.addRow(label, spin)
        self.ranges = {}
        for key, label in [('battery_charge_temperature_range_c', '充电温度范围 ℃'),
                           ('battery_discharge_temperature_range_c', '放电温度范围 ℃')]:
            field = QLineEdit('' if protection.get(key) is None else ', '.join(map(str, protection[key])))
            field.setPlaceholderText('最低温度, 最高温度')
            controls.addRow(label, field)
            self.ranges[key] = field
        self.fixture = QCheckBox('已插入 USB 3.x 测试夹具，并确认线材/接口条件')
        self.fixture.setChecked(plan['items']['usb'].get('fixture_confirmed', False))
        self.display = QCheckBox('已人工确认 HDMI 显示测试图案（四代主控无此接口）')
        self.display.setChecked(plan['items']['hdmi'].get('display_confirmed', False))
        self.direction = QCheckBox('已验证电池净电流：充电为正，放电为负')
        self.direction.setChecked(plan['items']['battery'].get('current_direction_confirmed', False))
        self.isolation = QCheckBox('已确认设备未被其他程序占用，运行期间不启动其他测试任务')
        for widget in (self.fixture, self.display, self.direction, self.isolation):
            controls.addRow(widget)
        controls.addRow(QLabel('电池不写入 Auto Ship/休眠配置；电源适配器由操作者逐设备切换。'))
        controls.addRow(QLabel('本地保护只能停止软件负载；电池保护后需人工接回适配器。'))
        detail = QVBoxLayout(advanced)
        label = QLabel('配置存储目录/预算/轮数、4G 接口/诊断口/目标等。项目勾选、时长、保护门限以上方表单为准。\n存储完成固定写入轮次后只读观察至到时；不做无限覆写。')
        label.setWordWrap(True)
        detail.addWidget(label)
        self.yaml = QPlainTextEdit(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False))
        detail.addWidget(self.yaml)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('保存计划')
        buttons.accepted.connect(self.save_plan)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def integer(value, low, high):
        widget = QSpinBox()
        widget.setRange(low, high)
        widget.setValue(value)
        return widget

    def save_plan(self):
        try:
            plan = yaml.safe_load(self.yaml.toPlainText())
            if not isinstance(plan, dict):
                raise ValueError('配置必须为字典')
            plan.pop('operator', None)
            plan.pop('station', None)
            for key in ITEMS:
                plan['items'][key].update(enabled=self.checks[key].isChecked(), duration_min=self.durations[key].value())
            plan['items']['battery'].update(charge_duration_min=self.charge.value(),
                discharge_duration_min=self.discharge.value(), cycles=self.cycles.value(),
                current_direction_confirmed=self.direction.isChecked())
            plan['items']['usb']['fixture_confirmed'] = self.fixture.isChecked()
            plan['items']['hdmi']['display_confirmed'] = self.display.isChecked()
            for key, widget in self.protection_fields.items():
                plan['protection'][key] = widget.value() or None
            for key, widget in self.ranges.items():
                plan['protection'][key] = [float(p.strip()) for p in widget.text().split(',')] if widget.text().strip() else None
            env = plan.setdefault('environment', {})
            env.update(chamber_id=self.chamber.text().strip() or None,
                set_temperature_c=float(self.temperature.text()) if self.temperature.text().strip() else None,
                set_humidity_pct=float(self.humidity.text()) if self.humidity.text().strip() else None)
            if env['set_humidity_pct'] is not None and not 0 <= env['set_humidity_pct'] <= 100:
                raise ValueError('湿度需为 0～100%')
            if not self.isolation.isChecked():
                raise ValueError('请确认设备未被其他程序占用，运行期间不启动其他测试任务')
            plan['station_isolation_confirmed'] = True
            validate_plan(plan)
            self.plan = plan
            self.accept()
        except (ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, '计划不完整', str(exc))
