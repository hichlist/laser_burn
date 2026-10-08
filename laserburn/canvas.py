"""Холст рабочего поля: отображение фигур, инструменты рисования, масштаб и панорама.

Сцена работает прямо в миллиметрах станка, ось Y перевёрнута трансформацией вида,
поэтому mapToScene() сразу даёт координаты станка."""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QKeyEvent, QMouseEvent, QPainter, QPainterPath,
                         QPainterPathStroker, QPen, QTransform, QWheelEvent)
from PyQt6.QtWidgets import (QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsView,
                             QStyle)

from .core import (MODE_FILL, Document, Layer, Shape, is_closed, make_ellipse, make_line,
                   make_rect)

TOOL_SELECT = "select"
TOOL_LINE = "line"
TOOL_RECT = "rect"
TOOL_ELLIPSE = "ellipse"
TOOL_POLYLINE = "polyline"

HIT_TOLERANCE_PX = 6


def paths_to_qpath(paths) -> QPainterPath:
    qp = QPainterPath()
    for p in paths:
        if len(p) < 2:
            continue
        qp.moveTo(*p[0])
        for pt in p[1:]:
            qp.lineTo(*pt)
    return qp


class ShapeItem(QGraphicsPathItem):
    """Графический объект холста, связанный с фигурой документа."""
    hit_width = 1.0  # мм, обновляется видом при изменении масштаба

    def __init__(self, shape: Shape, layer: Layer | None):
        super().__init__(paths_to_qpath(shape.paths))
        self.shape_ref = shape
        self._closed = any(is_closed(p) for p in shape.paths)
        self._fill = False
        self.setPos(shape.dx, shape.dy)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.apply_layer(layer)

    def apply_layer(self, layer: Layer | None) -> None:
        color = QColor(layer.color if layer else "#808080")
        pen = QPen(color, 1.5)
        pen.setCosmetic(True)
        self.setPen(pen)
        self._fill = bool(layer and layer.mode == MODE_FILL and self._closed)
        if self._fill:
            fill = QColor(color)
            fill.setAlpha(60)
            self.setBrush(QBrush(fill))
        else:
            self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setOpacity(1.0 if (layer is None or layer.output) else 0.35)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(self.hit_width)
        outline = stroker.createStroke(self.path())
        return outline.united(self.path()) if self._fill else outline

    def boundingRect(self) -> QRectF:
        m = self.hit_width
        return self.path().boundingRect().adjusted(-m, -m, m, m)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.shape_ref.dx = self.pos().x()
            self.shape_ref.dy = self.pos().y()
        return super().itemChange(change, value)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        option.state &= ~QStyle.StateFlag.State_Selected
        if selected:
            pen = QPen(self.pen())
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(self.brush())
            painter.drawPath(self.path())
        else:
            super().paint(painter, option, widget)


class HeadMarker(QGraphicsItem):
    """Перекрестие текущего положения лазерной головы (размер не зависит от масштаба)."""

    def __init__(self):
        super().__init__()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setZValue(1000)

    def boundingRect(self) -> QRectF:
        return QRectF(-12, -12, 24, 24)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        pen = QPen(QColor("#e00000"), 1.5)
        painter.setPen(pen)
        painter.drawLine(-10, 0, 10, 0)
        painter.drawLine(0, -10, 0, 10)
        painter.drawEllipse(QPointF(0, 0), 5, 5)


class CanvasView(QGraphicsView):
    shape_created = pyqtSignal(object)        # Shape
    cursor_moved = pyqtSignal(float, float)   # мм
    shapes_moved = pyqtSignal()
    tool_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Document | None = None
        self.current_layer_id = 0
        self.tool = TOOL_SELECT
        self.snap = True
        self.snap_step = 1.0
        self._scene = QGraphicsScene(self)
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.setScene(self._scene)
        self.head = HeadMarker()
        self._scene.addItem(self.head)
        self._items: list[ShapeItem] = []
        self._start: QPointF | None = None
        self._poly: list[tuple[float, float]] = []
        self._preview = QGraphicsPathItem()
        pen = QPen(QColor("#0078d7"), 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self._preview.setPen(pen)
        self._preview.setZValue(999)
        self._scene.addItem(self._preview)
        self._pan_from = None
        self._moving = False

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setTransform(QTransform.fromScale(2, -2))
        self.set_tool(TOOL_SELECT)

    # --- документ ---
    def set_document(self, doc: Document) -> None:
        self.doc = doc
        for item in self._items:
            self._scene.removeItem(item)
        self._items = []
        m = max(doc.bed_width, doc.bed_height) * 2
        self._scene.setSceneRect(-m, -m, doc.bed_width + 2 * m, doc.bed_height + 2 * m)
        for s in doc.shapes:
            self._add_item(s)
        self.zoom_fit()

    def _add_item(self, shape: Shape) -> ShapeItem:
        item = ShapeItem(shape, self.doc.layer(shape.layer_id) if self.doc else None)
        self._scene.addItem(item)
        self._items.append(item)
        return item

    def add_shape(self, shape: Shape, select: bool = False) -> ShapeItem:
        self.doc.shapes.append(shape)
        item = self._add_item(shape)
        if select:
            item.setSelected(True)
        return item

    def remove_shapes(self, shapes: list[Shape]) -> None:
        ids = {id(s) for s in shapes}
        for item in [i for i in self._items if id(i.shape_ref) in ids]:
            self._scene.removeItem(item)
            self._items.remove(item)
        self.doc.shapes = [s for s in self.doc.shapes if id(s) not in ids]

    def selected_shapes(self) -> list[Shape]:
        return [i.shape_ref for i in self._items if i.isSelected()]

    def select_all(self) -> None:
        for i in self._items:
            i.setSelected(True)

    def refresh_layers(self) -> None:
        """Перекрасить объекты после изменения слоёв; удалить объекты удалённых слоёв."""
        alive = {id(s) for s in self.doc.shapes}
        for item in list(self._items):
            if id(item.shape_ref) not in alive:
                self._scene.removeItem(item)
                self._items.remove(item)
            else:
                item.apply_layer(self.doc.layer(item.shape_ref.layer_id))
        self._scene.update()

    def set_head_position(self, x: float, y: float) -> None:
        self.head.setPos(x, y)

    # --- вид ---
    @property
    def zoom(self) -> float:
        return self.transform().m11()

    def _update_hit_width(self) -> None:
        ShapeItem.hit_width = HIT_TOLERANCE_PX / max(self.zoom, 1e-6)
        for item in self._items:
            item.prepareGeometryChange()

    def zoom_fit(self) -> None:
        if not self.doc:
            return
        margin = 10
        w, h = self.doc.bed_width + 2 * margin, self.doc.bed_height + 2 * margin
        vw, vh = max(self.viewport().width(), 100), max(self.viewport().height(), 100)
        z = min(vw / w, vh / h)
        self.setTransform(QTransform.fromScale(z, -z))
        self.centerOn(self.doc.bed_width / 2, self.doc.bed_height / 2)
        self._update_hit_width()

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        new_zoom = self.zoom * factor
        if 0.05 <= new_zoom <= 500:
            self.scale(factor, factor)
            self._update_hit_width()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor("#5a5a5a"))
        if not self.doc:
            return
        bed = QRectF(0, 0, self.doc.bed_width, self.doc.bed_height)
        painter.fillRect(bed, QColor("#ffffff"))
        # Сетка 10 мм, каждая 5-я линия — темнее
        minor, major = QPen(QColor("#e6e6e6"), 0), QPen(QColor("#c4c4c4"), 0)
        step = 10.0
        if self.zoom * step < 4:
            step = 50.0
        x = 0.0
        while x <= self.doc.bed_width + 1e-6:
            painter.setPen(major if round(x) % 50 == 0 else minor)
            painter.drawLine(QPointF(x, 0), QPointF(x, self.doc.bed_height))
            x += step
        y = 0.0
        while y <= self.doc.bed_height + 1e-6:
            painter.setPen(major if round(y) % 50 == 0 else minor)
            painter.drawLine(QPointF(0, y), QPointF(self.doc.bed_width, y))
            y += step
        painter.setPen(QPen(QColor("#404040"), 0))
        painter.drawRect(bed)

    # --- инструменты ---
    def set_tool(self, tool: str) -> None:
        self._cancel_drawing()
        self.tool = tool
        if tool == TOOL_SELECT:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self._scene.clearSelection()
        self.tool_changed.emit(tool)

    def _scene_point(self, event: QMouseEvent) -> QPointF:
        p = self.mapToScene(event.position().toPoint())
        if self.snap and self.tool != TOOL_SELECT:
            s = self.snap_step
            p = QPointF(round(p.x() / s) * s, round(p.y() / s) * s)
        return p

    def _cancel_drawing(self) -> None:
        self._start = None
        self._poly = []
        self._preview.setPath(QPainterPath())

    def _make_shape(self, a: QPointF, b: QPointF, shift: bool) -> Shape | None:
        lid = self.current_layer_id
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if self.tool == TOOL_LINE:
            if shift:
                ang = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
                r = math.hypot(dx, dy)
                b = QPointF(a.x() + r * math.cos(ang), a.y() + r * math.sin(ang))
            if math.dist((a.x(), a.y()), (b.x(), b.y())) < 1e-3:
                return None
            return make_line(lid, (a.x(), a.y()), (b.x(), b.y()))
        if shift:
            side = max(abs(dx), abs(dy))
            dx, dy = math.copysign(side, dx), math.copysign(side, dy)
        if abs(dx) < 1e-3 or abs(dy) < 1e-3:
            return None
        if self.tool == TOOL_RECT:
            return make_rect(lid, a.x(), a.y(), a.x() + dx, a.y() + dy)
        if self.tool == TOOL_ELLIPSE:
            return make_ellipse(lid, a.x() + dx / 2, a.y() + dy / 2, dx / 2, dy / 2)
        return None

    def _finish_polyline(self, close: bool = False) -> None:
        pts = self._poly
        if close and len(pts) >= 3:
            pts = pts + [pts[0]]
        if len(pts) >= 2:
            self.shape_created.emit(Shape(self.current_layer_id, [pts], kind="path"))
        self._cancel_drawing()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_from = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self.tool == TOOL_SELECT:
            self._moving = event.button() == Qt.MouseButton.LeftButton
            super().mousePressEvent(event)
            return
        p = self._scene_point(event)
        if self.tool == TOOL_POLYLINE:
            if event.button() == Qt.MouseButton.RightButton:
                self._finish_polyline()
            elif event.button() == Qt.MouseButton.LeftButton:
                if len(self._poly) >= 3 and math.dist(self._poly[0], (p.x(), p.y())) * self.zoom < 8:
                    self._finish_polyline(close=True)
                else:
                    self._poly.append((p.x(), p.y()))
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = p
        elif event.button() == Qt.MouseButton.RightButton:
            self._cancel_drawing()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        raw = self.mapToScene(event.position().toPoint())
        self.cursor_moved.emit(raw.x(), raw.y())
        if self._pan_from is not None:
            d = event.position() - self._pan_from
            self._pan_from = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            return
        if self.tool == TOOL_SELECT:
            super().mouseMoveEvent(event)
            return
        p = self._scene_point(event)
        if self.tool == TOOL_POLYLINE and self._poly:
            self._preview.setPath(paths_to_qpath([self._poly + [(p.x(), p.y())]]))
        elif self._start is not None:
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            s = self._make_shape(self._start, p, shift)
            self._preview.setPath(paths_to_qpath(s.paths) if s else QPainterPath())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_from is not None:
            self._pan_from = None
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor if self.tool == TOOL_SELECT
                                      else Qt.CursorShape.CrossCursor)
            return
        if self.tool == TOOL_SELECT:
            super().mouseReleaseEvent(event)
            if self._moving and self._scene.selectedItems():
                self.shapes_moved.emit()
            self._moving = False
            return
        if self.tool in (TOOL_LINE, TOOL_RECT, TOOL_ELLIPSE) and self._start is not None \
                and event.button() == Qt.MouseButton.LeftButton:
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            s = self._make_shape(self._start, self._scene_point(event), shift)
            self._cancel_drawing()
            if s:
                self.shape_created.emit(s)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self.tool == TOOL_POLYLINE:
            self._finish_polyline()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._poly or self._start:
                self._cancel_drawing()
            else:
                self.set_tool(TOOL_SELECT)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.tool == TOOL_POLYLINE:
            self._finish_polyline()
            return
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                  Qt.Key.Key_Up: (0, 1), Qt.Key.Key_Down: (0, -1)}
        if key in arrows and self.tool == TOOL_SELECT:
            step = 10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
            dx, dy = arrows[key]
            items = [i for i in self._items if i.isSelected()]
            for item in items:
                item.moveBy(dx * step, dy * step)
            if items:
                self.shapes_moved.emit()
            return
        super().keyPressEvent(event)
