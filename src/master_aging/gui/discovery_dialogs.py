"""与主控产测相同的按需网卡选择和扫描结果多选流程。"""
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox,
                             QHBoxLayout, QHeaderView, QLabel, QListWidget, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)


class InterfaceDialog(QDialog):
    def __init__(self, interfaces, parent=None):
        super().__init__(parent)
        self.setWindowTitle('选择网卡')
        self.resize(540, 280)
        self.interfaces = interfaces
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('检测到多个网卡，请选择设备所在局域网：'))
        self.list = QListWidget()
        for interface in interfaces:
            self.list.addItem('%s - %s (%s)' % (interface['name'], interface['ip'], interface['subnet']))
        self.list.setCurrentRow(0)
        layout.addWidget(self.list)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def subnet(self):
        return self.interfaces[self.list.currentRow()]['subnet']


class DeviceSelectionDialog(QDialog):
    def __init__(self, devices, parent=None):
        super().__init__(parent)
        self.setWindowTitle('选择设备')
        self.resize(980, 440)
        self.devices, self.checks = devices, []
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel('扫描到 %s 台设备，请勾选要添加的设备（支持多选）' % len(devices)))
        select_all = QCheckBox('全选')
        header.addWidget(select_all)
        layout.addLayout(header)
        table = QTableWidget(len(devices), 6)
        table.setHorizontalHeaderLabels(['选择', 'SN / 主机名', 'IP', '机型', '开放端口', '识别状态'])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        for row, device in enumerate(devices):
            check = QCheckBox()
            self.checks.append(check)
            table.setCellWidget(row, 0, check)
            for col, key in enumerate(('sn', 'ip', 'model'), 1):
                table.setItem(row, col, QTableWidgetItem(str(device.get(key, ''))))
            table.setItem(row, 4, QTableWidgetItem(', '.join(str(port) for port in device.get('open_ports', []))))
            table.setItem(row, 5, QTableWidgetItem(device.get('discovery_note', '')))
        select_all.toggled.connect(lambda checked: [c.setChecked(checked) for c in self.checks])
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('添加选中设备')
        buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        for check in self.checks:
            check.toggled.connect(lambda: buttons.button(QDialogButtonBox.Ok).setEnabled(any(c.isChecked() for c in self.checks)))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_devices(self):
        return [device for device, check in zip(self.devices, self.checks) if check.isChecked()]
