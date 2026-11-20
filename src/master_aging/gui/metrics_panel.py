"""原生 Qt 实时曲线，不引入图表依赖。"""
from collections import deque
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import QWidget


class MetricsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.runs = {}
        self.selected_run = None

    def select(self, run_id):
        self.selected_run = run_id
        self.update()

    def update_status(self, run_id, status):
        latest = status.get('latest', {})
        stamp = latest.get('sample_monotonic', status.get('wall_s'))
        if stamp is None:
            return
        points = self.runs.setdefault(run_id, deque(maxlen=600))
        if points and stamp <= points[-1][0]:
            return
        temps = list(latest.get('temperature_c', {}).values())
        battery = latest.get('battery', {})
        values = [latest.get('cpu', {}).get('busy_pct'), max(temps) if temps else None,
                  battery.get('current_ma'), battery.get('soc_pct')]
        points.append((stamp, values))
        if not self.selected_run:
            self.selected_run = run_id
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('#f5f7fb'))
        points = self.runs.get(self.selected_run, [])
        labels = ['CPU 忙碌率 %', '最高 CPU 温度 ℃', '电池净电流 mA', '电池 SOC %']
        for index, label in enumerate(labels):
            column, row = index % 2, index // 2
            rect = QRectF(column * self.width() / 2 + 8, row * self.height() / 2 + 8,
                          self.width() / 2 - 16, self.height() / 2 - 16)
            painter.setPen(QPen(QColor('#dde4ed'), 1))
            painter.setBrush(QColor('white'))
            painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(QColor('#23416a'))
            painter.drawText(rect.adjusted(12, 7, -12, -7), Qt.AlignTop | Qt.AlignLeft, label)
            valid = [(t, values[index]) for t, values in points if values[index] is not None]
            if len(valid) < 2:
                painter.setPen(QColor('#718096'))
                painter.drawText(rect, Qt.AlignCenter, '等待有效采样')
                continue
            xmin, xmax = valid[0][0], valid[-1][0]
            ymin, ymax = min(v for _, v in valid), max(v for _, v in valid)
            area = rect.adjusted(42, 32, -18, -28)
            painter.setPen(QColor('#8c9aad'))
            painter.drawLine(area.bottomLeft(), area.bottomRight())
            painter.drawText(rect.adjusted(8, 32, -8, -25), Qt.AlignTop | Qt.AlignLeft, '%.0f' % ymax)
            painter.drawText(rect.adjusted(8, 32, -8, -25), Qt.AlignBottom | Qt.AlignLeft, '%.0f' % ymin)
            painter.drawText(rect.adjusted(12, 0, -12, -5), Qt.AlignBottom | Qt.AlignRight,
                             '最近 %.1f 分钟 · 当前 %.1f' % ((xmax - xmin) / 60, valid[-1][1]))
            painter.setPen(QPen(QColor('#286aca'), 2))
            segment, previous = QPolygonF(), None
            for stamp, value in valid:
                if previous is not None and stamp - previous > 60:
                    painter.drawPolyline(segment)
                    segment = QPolygonF()
                segment.append(QPointF(area.left() + (stamp - xmin) / max(1, xmax - xmin) * area.width(),
                                       area.bottom() - (value - ymin) / max(1, ymax - ymin) * area.height()))
                previous = stamp
            painter.drawPolyline(segment)
