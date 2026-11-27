"""老化窗口独立样式，与主控产测的蓝色分区和操作色保持一致。"""

STYLE = '''
QMainWindow, QDialog { background: #f5f6f8; }
QWidget { color: #263238; font-size: 13px; }
QFrame#devicePanel, QFrame#workspace { background: white; border: 1px solid #dfe5eb; border-radius: 6px; }
QLabel#sectionHeader { background: #2196f3; color: white; font-size: 16px; font-weight: bold; padding: 12px 14px; border-radius: 4px; }
QLabel#muted { color: #78909c; }
QLabel#deviceInfo { background: #f5f5f5; padding: 12px; border-radius: 5px; font-weight: bold; }
QLabel#planSummary { background: #e3f2fd; color: #1565c0; padding: 10px 12px; border-radius: 5px; }
QPushButton { background: #2196f3; color: white; border: none; padding: 8px 14px; border-radius: 4px; }
QPushButton:hover { background: #0b7dda; }
QPushButton:pressed { background: #1565c0; }
QPushButton[role="start"] { background: #ff5722; }
QPushButton[role="start"]:hover { background: #e64a19; }
QPushButton[role="danger"] { background: #e53935; }
QPushButton[role="danger"]:hover { background: #c62828; }
QPushButton[role="secondary"] { background: #eef3f7; color: #455a64; border: 1px solid #dce4ec; }
QPushButton[role="secondary"]:hover { background: #e3edf5; }
QPushButton[role="confirm"] { background: #ff9800; }
QPushButton:disabled { background: #e0e0e0; color: #9e9e9e; }
QFrame#deviceCard { background: white; border: 1px solid #dce4ec; border-radius: 8px; }
QFrame#deviceCard[active="true"] { background: #e3f2fd; border: 2px solid #2196f3; }
QLabel#cardSn { font-size: 13px; font-weight: bold; }
QToolButton#deviceAction { background: transparent; border: none; padding: 0; border-radius: 4px; }
QToolButton#deviceAction:hover { background: #dcecf8; }
QToolButton#deviceAction:pressed { background: #c6dff2; }
QToolButton#deviceAction[action="delete"]:hover { background: #ffebee; }
QToolButton#deviceAction[action="delete"]:pressed { background: #ffcdd2; }
QScrollArea, QTabWidget::pane { background: white; border: none; }
QTabBar::tab { background: #eef2f6; color: #607080; padding: 11px 18px; border-bottom: 3px solid transparent; }
QTabBar::tab:selected { color: #1976d2; background: white; border-bottom: 3px solid #2196f3; }
QTabBar::tab:hover { background: #e3f2fd; }
QTableWidget { background: white; alternate-background-color: #f7fafc; border: 1px solid #e3e9ef; gridline-color: #edf1f5; selection-background-color: #e3f2fd; selection-color: #1565c0; }
QHeaderView::section { background: #eef3f8; color: #455a64; border: none; border-bottom: 1px solid #dce4ec; padding: 10px; font-weight: bold; }
QPlainTextEdit { background: #fafbfd; border: 1px solid #e3e9ef; border-radius: 4px; padding: 8px; selection-background-color: #bbdefb; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #cfd8dc; border-radius: 4px; padding: 5px; }
QGroupBox { border: 1px solid #dfe5eb; border-radius: 5px; margin-top: 12px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QStatusBar { background: #eef2f6; color: #607080; border-top: 1px solid #dce4ec; }
QSplitter::handle { background: #edf1f5; width: 8px; }
QProgressBar { border: none; background: #e3f2fd; border-radius: 3px; text-align: center; min-height: 18px; color: #1565c0; }
QProgressBar::chunk { background: #90caf9; border-radius: 3px; }
'''

STATES = {'DISCOVERED': '待配置', 'DEPLOYING': '准备环境', 'READY': '准备就绪', 'PREFLIGHT': '预检中', 'RUNNING': '老化中',
          'WAIT_OPERATOR': '等待电源确认', 'WAIT_CURRENT': '等待充放电电流',
          'RECONNECTING': '等待重连', 'FINISHED': '已结束',
          'BLOCKED': '预检未通过', 'STOPPING': '停止中'}


def status_color(state):
    if state == 'PASS':
        return '#4caf50'
    if state in ('FAIL', 'BLOCKED'):
        return '#e53935'
    if state in ('RUNNING', 'PREFLIGHT'):
        return '#2196f3'
    if state in ('ABORTED', 'INCOMPLETE', 'WAIT_OPERATOR', 'WAIT_CURRENT', 'RECONNECTING', 'STOPPING'):
        return '#ff9800'
    return '#78909c'
