"""
标签模板编辑器
使用 QGraphicsScene 实现可拖拽的字段编辑
"""
import copy
import math
import re
from pathlib import Path

from PyQt5.QtGui import QImage

from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QSplitter,
    QGroupBox, QListWidget, QListWidgetItem, QPushButton,
    QLabel, QLineEdit, QSpinBox, QComboBox, QCheckBox,
    QFormLayout, QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsPixmapItem,
    QGraphicsItem, QInputDialog, QMessageBox, QFileDialog,
    QWidget, QSizePolicy
)
from PyQt5.QtCore import Qt, QPointF, pyqtSignal
from PyQt5.QtGui import QPen, QBrush, QColor, QPainter, QPixmap, QTransform

from src.qrcode_print.label_background import (background_path, barcode_field_image,
                                                copy_background, import_background, label_pixels,
                                                load_background, text_font_size,
                                                qrcode_field_image, text_field_image)
from src.utils.logger import logger

# 物理尺寸常量
_DPI = 203  # TSPL打印机标准DPI（203 DPI）


def has_chinese(text: str) -> bool:
    """
    检测文本中是否包含中文字符（汉字、中文标点等）

    包含的Unicode范围：
    - CJK 统一汉字: U+4E00-U+9FFF
    - CJK 扩展A: U+3400-U+4DBF
    - CJK 兼容: U+F900-U+FAFF
    - 中文标点: U+3000-U+303F, U+FF00-U+FFEF
    """
    pattern = re.compile(r'[一-鿿㐀-䶿豈-﫿　-〿＀-￯]')
    return bool(pattern.search(text))


def image_pixmap(image):
    image = image.convert('RGBA')
    data = image.tobytes()
    qimage = QImage(data, image.width, image.height, image.width * 4,
                    QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimage.copy())


# ──────────────────────────────────────────────────────────────────────────────
# 字段
# ──────────────────────────────────────────────────────────────────────────────

class FieldItem(QGraphicsRectItem):

    TYPE_COLORS = {
        'text':    QColor(33, 150, 243, 200),
        'date':    QColor(76, 175, 80,  200),
        'qrcode':  QColor(156, 39, 176, 200),
        'barcode': QColor(255, 152, 0,  200),
        'model':   QColor(0, 188, 212,  200),
    }
    def __init__(self, field: dict, scale: float = 1.0, sample_data: str = 'SN'):
        super().__init__(0, 0, 1, 1)
        self.field = field
        self._scale = scale
        self._sample_data = sample_data

        ftype = field.get('type', 'text')
        if ftype in ('text', 'date', 'model'):
            field.setdefault('font_size', text_font_size(field))
            field.pop('x_scale', None)
            field.pop('y_scale', None)
        color = self.TYPE_COLORS.get(ftype, QColor(128, 128, 128, 200))
        self.setPen(QPen(color.darker(140), 1))
        self.setBrush(QBrush(Qt.NoBrush))

        self._image_item = QGraphicsPixmapItem(self)
        self._image_item.setTransformationMode(Qt.SmoothTransformation)
        self._image_item.setAcceptedMouseButtons(Qt.NoButton)
        self._print_image_size = None

        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)

        self._apply_scale(scale)

    def _apply_scale(self, scale: float):
        self._scale = scale
        x = field_dot_x(self.field) * scale
        y = field_dot_y(self.field) * scale
        self.setPos(x, y)
        self._update_text()
        w, h = self._calc_size(scale)
        self.setRect(0, 0, w, h)

        # 强制更新图形项的显示
        self.prepareGeometryChange()
        self.update()
        if self.scene():
            self.scene().update()

    def _calc_size(self, scale: float) -> tuple:
        ftype = self.field.get('type', 'text')
        if ftype == 'qrcode':
            pixmap = self._image_item.pixmap()
            return pixmap.width() * scale, pixmap.height() * scale
        elif ftype == 'barcode':
            pixmap = self._image_item.pixmap()
            return pixmap.width() * scale, pixmap.height() * scale
        else:
            width, height = self._print_image_size
            return max(1, width * scale), max(1, height * scale)

    def _update_text(self):
        ftype = self.field.get('type', 'text')
        if ftype in ('text', 'date', 'model'):
            print_image = text_field_image(self.field, self._sample_data)
            self._print_image_size = print_image.size
            # 画布上的打印点会被放大；用屏幕分辨率重绘文字，避免放大小位图时发虚。
            render_scale = (min(6, math.ceil(self._scale * 2))
                            if self._scale > 1.2 else 1)
            image = (text_field_image(self.field, self._sample_data,
                                      _DPI * render_scale)
                     if render_scale > 1 else print_image)
        elif ftype == 'qrcode':
            render_scale = 1
            image = qrcode_field_image(self._sample_data, self.field.get('cell_width', 6))
        elif ftype == 'barcode':
            render_scale = 1
            image = barcode_field_image(self._sample_data, self.field)
        else:
            render_scale = 1
            image = None
        if image is not None:
            self._image_item.setPixmap(image_pixmap(image))
            if ftype in ('text', 'date', 'model'):
                width, height = self._print_image_size
                self._image_item.setTransform(QTransform().scale(
                    width * self._scale / image.width,
                    height * self._scale / image.height))
            else:
                self._image_item.setTransform(QTransform().scale(self._scale, self._scale))
            self._image_item.setVisible(True)
        else:
            self._image_item.setVisible(False)

    def refresh(self, scale: float = None):
        if scale is not None:
            self._scale = scale
        # 设置刷新标志，避免触发 field_resized 信号
        self._refreshing = True
        try:
            self._apply_scale(self._scale)
        finally:
            self._refreshing = False

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self._scale:
            return QPointF(round(value.x() / self._scale) * self._scale,
                           round(value.y() / self._scale) * self._scale)
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene():
            self.field['x'] = round(value.x() / self._scale)
            self.field['y'] = round(value.y() / self._scale)
            # 只有在非刷新状态下才发射信号
            # _refreshing 标志用于避免在批量刷新时触发大量的 field_resized 信号
            if not getattr(self, '_refreshing', False):
                # 拖动时只更新属性面板的值，不重新加载整个面板
                scene = self.scene()
                if scene and hasattr(scene, 'field_resized'):
                    # 使用 QTimer 延迟发射信号，避免拖动过程中频繁刷新
                    if not hasattr(self, '_update_timer'):
                        from PyQt5.QtCore import QTimer
                        self._update_timer = QTimer()
                        self._update_timer.setSingleShot(True)
                        self._update_timer.timeout.connect(lambda: scene.field_resized.emit(self))
                    self._update_timer.start(100)  # 100ms 延迟
        return super().itemChange(change, value)


def field_dot_x(field): return field.get('x', 0)
def field_dot_y(field): return field.get('y', 0)


# ──────────────────────────────────────────────────────────────────────────────
# 画布（自适应标签大小）
# ──────────────────────────────────────────────────────────────────────────────

class LabelScene(QGraphicsScene):
    field_resized = pyqtSignal(object)


class LabelCanvas(QGraphicsView):

    field_selected = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = LabelScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setBackgroundBrush(QBrush(QColor(200, 200, 200)))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 虚线标签背景
        self._bg = QGraphicsRectItem()
        self._bg.setBrush(QBrush(Qt.white))
        pen = QPen(QColor(60, 60, 60), 2, Qt.DashLine)
        pen.setDashPattern([8, 5])
        self._bg.setPen(pen)
        self._bg.setZValue(-1)
        self._scene.addItem(self._bg)

        self._background = QGraphicsPixmapItem()
        self._background.setZValue(-0.5)
        self._background.setTransformationMode(Qt.SmoothTransformation)
        self._scene.addItem(self._background)

        self._items: list[FieldItem] = []
        self._sample_data = 'SN'
        self._paper_width_mm = 50
        self._paper_height_mm = 30
        self._paper_w_dot, self._paper_h_dot = label_pixels(
            {'paper_width': 50, 'paper_height': 30}, _DPI)
        self._scale = 1.0
        self._background_path = None
        self._background_dpi = None
        self._applying = False  # 重入保护标志

        self._scene.selectionChanged.connect(self._on_selection)
        self._scene.field_resized.connect(self._on_field_resized)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._applying:
            self._recalc_scale()

    def set_paper_size(self, width_mm: float, height_mm: float):
        self._paper_width_mm = width_mm
        self._paper_height_mm = height_mm
        self._paper_w_dot, self._paper_h_dot = label_pixels(
            {'paper_width': width_mm, 'paper_height': height_mm}, _DPI)
        self._recalc_scale()

    def set_background(self, path):
        self._background_path = path
        self._background_dpi = None
        self._apply_paper_size()

    def _refresh_background(self):
        if self._background_path is None:
            if not self._background.pixmap().isNull():
                self._background.setPixmap(QPixmap())
            self._background_dpi = None
            return
        # 从原图直接生成屏幕尺寸图，避免先缩到 203 DPI 再放大造成模糊。
        render_scale = min(6, max(1, math.ceil(self._scale * 2)))
        dpi = _DPI * render_scale
        template = {'paper_width': self._paper_width_mm,
                    'paper_height': self._paper_height_mm}
        width, height = label_pixels(template, dpi)
        pixmap = self._background.pixmap()
        if (self._background_dpi == dpi and pixmap.width() == width
                and pixmap.height() == height):
            return
        self._background.setPixmap(image_pixmap(
            load_background(self._background_path, template, dpi)))
        self._background_dpi = dpi

    def set_sample_data(self, value):
        self._sample_data = value or 'SN'

    def _recalc_scale(self):
        """根据 view 大小重新计算缩放比，让标签铺满画布"""
        vw = self.viewport().width() - 40
        vh = self.viewport().height() - 40
        if self._paper_w_dot <= 0 or self._paper_h_dot <= 0:
            return
        self._scale = min(vw / self._paper_w_dot, vh / self._paper_h_dot)
        self._apply_paper_size()

    def _apply_paper_size(self):
        """用当前 _paper_w/h_dot 和 _scale 刷新背景和所有字段"""
        if self._applying:
            return
        self._applying = True
        try:
            w = self._paper_w_dot * self._scale
            h = self._paper_h_dot * self._scale
            self._bg.setRect(0, 0, w, h)
            self._refresh_background()
            pixmap = self._background.pixmap()
            if not pixmap.isNull():
                self._background.setTransform(QTransform().scale(
                    w / pixmap.width(), h / pixmap.height()))
            self._scene.setSceneRect(-20, -20, w + 40, h + 40)
            for item in self._items:
                item.refresh(self._scale)
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        finally:
            self._applying = False

    def load_fields(self, fields: list):
        for item in self._items:
            self._scene.removeItem(item)
        self._items.clear()
        for field in fields:
            item = FieldItem(field, self._scale, self._sample_data)
            self._scene.addItem(item)
            self._items.append(item)

    def add_field(self, field: dict):
        item = FieldItem(field, self._scale, self._sample_data)
        self._scene.addItem(item)
        self._items.append(item)
        # 主动选中新添加的字段
        self._scene.clearSelection()
        item.setSelected(True)
        return item

    def remove_selected(self):
        for item in self._scene.selectedItems():
            if isinstance(item, FieldItem):
                self._scene.removeItem(item)
                self._items.remove(item)
                return item.field
        return None

    def _on_selection(self):
        sel = self._scene.selectedItems()
        if sel and isinstance(sel[0], FieldItem):
            self.field_selected.emit(sel[0])

    def _on_field_resized(self, item: FieldItem):
        self.field_selected.emit(item)


# ──────────────────────────────────────────────────────────────────────────────
# 属性面板
# ──────────────────────────────────────────────────────────────────────────────

class FieldPropertyPanel(QWidget):

    changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._item: FieldItem = None
        self._building = False
        self._widgets = {}  # 保存控件引用，用于更新值
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self._title = QLabel("选择字段后编辑属性")
        self._title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._title)
        self._form = QFormLayout()
        layout.addLayout(self._form)
        layout.addStretch()

    def load(self, item: FieldItem):
        # 如果是同一个字段对象，只更新值，不重建表单
        if self._item is item and item is not None:
            logger.info(f"字段未变化，只更新值 - type: '{item.field.get('type', '')}'")
            self.update_values()
            return

        self._item = item
        self._building = True
        self._widgets.clear()
        while self._form.rowCount():
            self._form.removeRow(0)
        field = item.field
        ftype = field.get('type', '')

        self._title.setText(f"字段类型: {ftype}")

        # 调试输出
        logger.info(f"加载字段属性 - type: '{ftype}', field: {field}")

        if ftype in ('text', 'date', 'model'):
            field.setdefault('font_size', 8)
            field.pop('x_scale', None)
            field.pop('y_scale', None)
            self._spin("字号", 'font_size', field, 4, 72)
            self._checkbox("加粗", 'bold', field)
            self._spin("旋转", 'rotation', field, 0, 270, step=90)
        if ftype == 'model':
            self._text("文本内容", 'value', field)
        if ftype == 'text':
            logger.info(f"进入 text 分支")
            self._text("前缀", 'label', field)
        if ftype == 'date':
            logger.info(f"进入 date 分支")
            self._text("格式", 'format', field)
        if ftype == 'qrcode':
            logger.info(f"进入 qrcode 分支")
            self._spin("模块宽(dot)", 'cell_width', field, 1, 20)
        if ftype == 'barcode':
            self._spin("高度(dot)", 'height', field, 10, 500)
            self._spin("窄条(dot)", 'narrow', field, 1, 10)
            self._spin("宽条(dot)", 'wide', field, 2, 20)

        self._building = False

    def _spin(self, label, key, field, lo, hi, step=1):
        w = QSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setValue(int(field.get(key, lo)))
        w.valueChanged.connect(lambda v, k=key: self._changed(k, v))
        self._form.addRow(label, w)
        self._widgets[key] = w  # 保存控件引用

    def _checkbox(self, label, key, field):
        widget = QCheckBox()
        widget.setChecked(bool(field.get(key, False)))
        widget.toggled.connect(lambda checked, k=key: self._changed(k, checked))
        self._form.addRow(label, widget)
        self._widgets[key] = widget

    def _combo(self, label, key, field, opts):
        w = QComboBox()
        w.addItems(opts)
        idx = w.findText(str(field.get(key, opts[0])))
        if idx >= 0:
            w.setCurrentIndex(idx)
        w.currentTextChanged.connect(lambda v, k=key: self._changed(k, v))
        self._form.addRow(label, w)
        self._widgets[key] = w  # 保存控件引用

    def _text(self, label, key, field):
        w = QLineEdit(str(field.get(key, '')))
        w.textChanged.connect(lambda v, k=key: self._changed(k, v))
        self._form.addRow(label, w)
        self._widgets[key] = w  # 保存控件引用

    def update_values(self):
        """只更新控件的值，不重建表单"""
        if self._item is None:
            return
        self._building = True
        field = self._item.field
        for key, widget in self._widgets.items():
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(field.get(key, False)))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(field.get(key, 0)))
            elif isinstance(widget, QComboBox):
                idx = widget.findText(str(field.get(key, '')))
                if idx >= 0:
                    widget.setCurrentIndex(idx)
            elif isinstance(widget, QLineEdit):
                widget.setText(str(field.get(key, '')))
        self._building = False

    def _changed(self, key, value):
        if self._building or self._item is None:
            return
        self._item.field[key] = value
        if key == 'font_size':
            self._item.field.pop('x_scale', None)
            self._item.field.pop('y_scale', None)
        self._item.refresh()
        self.changed.emit(self._item)


# ──────────────────────────────────────────────────────────────────────────────
# 主对话框
# ──────────────────────────────────────────────────────────────────────────────

class TemplateEditorDialog(QDialog):

    DEFAULT_TEMPLATE = {
        'description': '新模板',
        'paper_width': 50,
        'paper_height': 30,
        'fields': []
    }
    FIELD_TYPES = [
        ('文本 (SN)', 'text'),
        ('日期',      'date'),
        ('二维码',    'qrcode'),
        ('条形码',    'barcode'),
        ('自定义文本', 'model'),
    ]

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.templates = {name: copy.deepcopy(template)
                          for name, template in config.get_print_templates().items()}
        self._current_name = None
        self._modified = False
        self._pending_backgrounds = set()
        self._sample_data = (parent.qrcode_input.text().strip()
                             if parent is not None and hasattr(parent, 'qrcode_input') else '') or 'SN'

        self.setWindowTitle("标签模板编辑器")
        self.resize(1400, 800)
        self._init_ui()
        self._load_list()

    def _init_ui(self):
        root = QHBoxLayout(self)
        sp = QSplitter(Qt.Horizontal)
        root.addWidget(sp)
        sp.addWidget(self._left_panel())
        sp.addWidget(self._center_panel())
        sp.addWidget(self._right_panel())
        sp.setSizes([260, 880, 260])

    # ── 左侧 ──────────────────────────────────────────────────────────────────

    def _left_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 4, 0)

        grp = QGroupBox("模板列表")
        gl = QVBoxLayout(grp)
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_tmpl_selected)
        gl.addWidget(self._list)
        btns = QHBoxLayout()
        for txt, slot in [("新增", self._add_tmpl), ("复制", self._copy_tmpl), ("删除", self._del_tmpl)]:
            b = QPushButton(txt)
            b.clicked.connect(slot)
            btns.addWidget(b)
        gl.addLayout(btns)
        lay.addWidget(grp)

        grp2 = QGroupBox("标签尺寸 (mm)")
        fl = QFormLayout(grp2)
        self._w_spin = QSpinBox(); self._w_spin.setRange(10, 300)
        self._h_spin = QSpinBox(); self._h_spin.setRange(10, 300)
        self._w_spin.valueChanged.connect(self._on_size_changed)
        self._h_spin.valueChanged.connect(self._on_size_changed)
        fl.addRow("宽", self._w_spin)
        fl.addRow("高", self._h_spin)
        lay.addWidget(grp2)

        grp3 = QGroupBox("模板描述")
        dl = QVBoxLayout(grp3)
        self._desc = QLineEdit()
        self._desc.textChanged.connect(self._on_desc_changed)
        dl.addWidget(self._desc)
        lay.addWidget(grp3)

        background_group = QGroupBox("标签底图")
        background_layout = QVBoxLayout(background_group)
        self._upload_background_button = QPushButton("上传并替换底图")
        self._upload_background_button.clicked.connect(self._upload_background)
        background_layout.addWidget(self._upload_background_button)
        background_hint = QLabel("等比例适配标签，完整显示图片\n替换后请点击“保存所有模板”")
        background_hint.setWordWrap(True)
        background_hint.setStyleSheet("color:#555;font-size:11px")
        background_layout.addWidget(background_hint)
        lay.addWidget(background_group)

        grp4 = QGroupBox("添加字段")
        fl2 = QVBoxLayout(grp4)
        self._ftype = QComboBox()
        for lbl, _ in self.FIELD_TYPES:
            self._ftype.addItem(lbl)
        fl2.addWidget(self._ftype)
        b_add = QPushButton("添加到画布")
        b_add.clicked.connect(self._add_field)
        fl2.addWidget(b_add)
        b_del = QPushButton("删除选中字段")
        b_del.clicked.connect(self._del_field)
        fl2.addWidget(b_del)
        lay.addWidget(grp4)

        lay.addStretch()

        b_save = QPushButton("保存所有模板")
        b_save.setStyleSheet("background:#4CAF50;color:white;font-weight:bold;padding:6px")
        b_save.clicked.connect(self._save_all)
        lay.addWidget(b_save)
        return panel

    # ── 中间 ─────────────────────────────────────────────────────────────────

    def _center_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        tip = QLabel("拖动字段移动位置 · 在属性面板调整字段大小")
        tip.setAlignment(Qt.AlignCenter)
        tip.setStyleSheet("color:#555;font-size:11px;padding:2px")
        lay.addWidget(tip)
        self._canvas = LabelCanvas()
        self._canvas.set_sample_data(self._sample_data)
        self._canvas.field_selected.connect(self._on_field_selected)
        lay.addWidget(self._canvas)
        return panel

    # ── 右侧 ─────────────────────────────────────────────────────────────────

    def _right_panel(self):
        grp = QGroupBox("字段属性")
        lay = QVBoxLayout(grp)
        self._props = FieldPropertyPanel()
        self._props.changed.connect(self._on_prop_changed)
        lay.addWidget(self._props)
        return grp

    # ── 模板列表操作 ──────────────────────────────────────────────────────────

    def _load_list(self):
        self._list.clear()
        for name in self.templates:
            self._list.addItem(QListWidgetItem(name))
        if self._list.count():
            self._list.setCurrentRow(0)

    def _on_tmpl_selected(self, cur, _prev):
        if cur is None:
            return
        name = cur.text()
        self._current_name = name
        tmpl = self.templates[name]
        self._upload_background_button.setEnabled(bool(tmpl.get('background_image')))
        for spin, key in [(self._w_spin, 'paper_width'), (self._h_spin, 'paper_height')]:
            spin.blockSignals(True)
            spin.setValue(tmpl.get(key, 50 if key == 'paper_width' else 30))
            spin.blockSignals(False)
        self._desc.blockSignals(True)
        self._desc.setText(tmpl.get('description', ''))
        self._desc.blockSignals(False)
        self._canvas.set_background(None)
        self._canvas.set_paper_size(tmpl.get('paper_width', 50), tmpl.get('paper_height', 30))
        self._canvas.set_background(background_path(self.config.config_path, tmpl))
        self._canvas.load_fields(tmpl.get('fields', []))

    def _add_tmpl(self):
        name, ok = QInputDialog.getText(self, "新增模板", "模板名称:")
        if not ok or not name.strip(): return
        name = name.strip()
        if name in self.templates:
            QMessageBox.warning(self, "提示", f"模板 '{name}' 已存在"); return
        self.templates[name] = copy.deepcopy(self.DEFAULT_TEMPLATE)
        item = QListWidgetItem(name)
        self._list.addItem(item)
        self._list.setCurrentItem(item)
        self._modified = True

    def _copy_tmpl(self):
        if not self._current_name: return
        name, ok = QInputDialog.getText(self, "复制模板", "新模板名称:")
        if not ok or not name.strip(): return
        name = name.strip()
        if name in self.templates:
            QMessageBox.warning(self, "提示", f"模板 '{name}' 已存在"); return
        copied = copy.deepcopy(self.templates[self._current_name])
        copied_path = None
        try:
            if copied.get('background_image'):
                copied['background_image'] = copy_background(self.config.config_path, copied)
                copied_path = background_path(self.config.config_path, copied)
        except Exception as exc:
            QMessageBox.warning(self, "复制模板失败", f"无法复制底图：{exc}")
            return
        if copied_path is not None:
            self._pending_backgrounds.add(copied_path)
        self.templates[name] = copied
        item = QListWidgetItem(name)
        self._list.addItem(item)
        self._list.setCurrentItem(item)
        self._modified = True

    def _del_tmpl(self):
        if not self._current_name: return
        if len(self.templates) <= 1:
            QMessageBox.warning(self, "提示", "至少保留一个模板"); return
        if QMessageBox.question(self, "确认", f"删除模板 '{self._current_name}'？",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes: return
        del self.templates[self._current_name]
        self._list.takeItem(self._list.currentRow())
        self._cleanup_pending_backgrounds()
        self._modified = True

    def _on_size_changed(self):
        if not self._current_name: return
        w, h = self._w_spin.value(), self._h_spin.value()
        self.templates[self._current_name]['paper_width'] = w
        self.templates[self._current_name]['paper_height'] = h
        self._canvas.set_paper_size(w, h)
        self._modified = True

    def _on_desc_changed(self, text):
        if not self._current_name: return
        self.templates[self._current_name]['description'] = text
        self._modified = True

    def _upload_background(self):
        if not self._current_name:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "上传并替换当前模板底图", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;所有文件 (*)")
        if not filename:
            return
        template = self.templates[self._current_name]
        shared = any(
            name != self._current_name and other.get('background_image') == template.get('background_image')
            for name, other in self.templates.items())
        replacement = template
        copied_path = None
        try:
            if shared:
                replacement = dict(template)
                replacement['background_image'] = copy_background(self.config.config_path, template)
                copied_path = background_path(self.config.config_path, replacement)
            import_background(self.config.config_path, filename, replacement)
            self._canvas.set_background(background_path(self.config.config_path, replacement))
        except Exception as exc:
            if copied_path is not None:
                copied_path.unlink(missing_ok=True)
            QMessageBox.warning(self, "底图替换失败", f"无法使用所选图片：{exc}")
            return
        if shared:
            template['background_image'] = replacement['background_image']
            self._pending_backgrounds.add(copied_path)
            self._modified = True

    # ── 字段操作 ──────────────────────────────────────────────────────────────

    def _add_field(self):
        if not self._current_name: return
        _, ftype = self.FIELD_TYPES[self._ftype.currentIndex()]
        defaults = {
            'text':    {'type': 'text',    'key': 'sn', 'label': 'SN:', 'x': 34,  'y': 50,  'font_size': 8, 'rotation': 0},
            'date':    {'type': 'date',    'x': 120, 'y': 20, 'font_size': 8, 'rotation': 0, 'format': '%Y/%m/%d'},
            'qrcode':  {'type': 'qrcode',  'key': 'sn', 'x': 200, 'y': 30, 'cell_width': 6},
            'barcode': {'type': 'barcode', 'key': 'sn', 'x': 34,  'y': 50, 'height': 40, 'narrow': 2, 'wide': 4},
            'model':   {'type': 'model',   'value': '自定义文本', 'x': 34, 'y': 20, 'font_size': 8, 'rotation': 0},
        }
        field = copy.deepcopy(defaults[ftype])
        self.templates[self._current_name].setdefault('fields', []).append(field)
        item = self._canvas.add_field(field)
        # 强制加载属性面板
        if item:
            self._props.load(item)
        self._modified = True

    def _del_field(self):
        if not self._current_name: return
        removed = self._canvas.remove_selected()
        if removed is None: return
        fields = self.templates[self._current_name].get('fields', [])
        if removed in fields:
            fields.remove(removed)
        self._modified = True

    def _on_field_selected(self, item: FieldItem):
        # 始终重新加载属性面板，确保控件类型正确
        self._props.load(item)

    def _on_prop_changed(self, item: FieldItem):
        self._modified = True

    # ── 保存 ─────────────────────────────────────────────────────────────────

    def _cleanup_pending_backgrounds(self, discard_all=False):
        referenced = set()
        config_dir = Path(self.config.config_path).parent
        for template in self.config.get_print_templates().values():
            if template.get('background_image'):
                referenced.add(config_dir / template['background_image'])
        if not discard_all:
            for template in self.templates.values():
                if template.get('background_image'):
                    referenced.add(config_dir / template['background_image'])
        for path in self._pending_backgrounds - referenced:
            path.unlink(missing_ok=True)
        self._pending_backgrounds.intersection_update(referenced)

    def _save_all(self):
        self._cleanup_pending_backgrounds()
        for name, tmpl in self.templates.items():
            if not self.config.save_print_template(name, tmpl):
                QMessageBox.warning(self, "保存失败", f"模板“{name}”保存失败")
                return
        self._pending_backgrounds.clear()
        self._modified = False
        QMessageBox.information(self, "保存成功", "所有模板已保存到配置文件")
        logger.info("模板已保存")

    def done(self, result):
        if result == QDialog.Rejected and self._modified:
            if QMessageBox.question(self, "未保存", "有未保存的修改，确认关闭？",
                                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
                return
        self._cleanup_pending_backgrounds(discard_all=True)
        super().done(result)

    def closeEvent(self, event):
        if self._modified:
            if QMessageBox.question(self, "未保存", "有未保存的修改，确认关闭？",
                                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
                event.ignore()
                return
        self._cleanup_pending_backgrounds(discard_all=True)
        event.accept()
