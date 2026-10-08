"""Главное окно приложения."""
from __future__ import annotations

import os
import re

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QAction, QActionGroup, QColor, QKeySequence, QPainter, QPen, QTransform
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDockWidget, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGraphicsScene, QGraphicsView, QLabel,
                             QMainWindow, QMessageBox, QSpinBox, QVBoxLayout)

from .canvas import (TOOL_ELLIPSE, TOOL_LINE, TOOL_POLYLINE, TOOL_RECT, TOOL_SELECT, CanvasView)
from .core import Document, MachineSettings, Shape, paths_bounds
from .gcode import compile_document, frame_gcode
from .grbl import RT_RESET, MachineStatus
from .importers import import_file
from .panels import ConsolePanel, LayerPanel, MachinePanel
from .serial_worker import SerialWorker

APP_NAME = "LaserBurn Analogue"
PROJECT_FILTER = "Проект LaserBurn (*.lbrn.json)"


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def load_settings() -> MachineSettings:
    q = QSettings()
    return MachineSettings(
        bed_width=float(q.value("machine/bed_width", 400.0)),
        bed_height=float(q.value("machine/bed_height", 400.0)),
        s_max=int(q.value("machine/s_max", 1000)),
        travel_speed=float(q.value("machine/travel_speed", 6000.0)),
        return_home=q.value("machine/return_home", True, type=bool),
    )


def save_settings(s: MachineSettings) -> None:
    q = QSettings()
    q.setValue("machine/bed_width", s.bed_width)
    q.setValue("machine/bed_height", s.bed_height)
    q.setValue("machine/s_max", s.s_max)
    q.setValue("machine/travel_speed", s.travel_speed)
    q.setValue("machine/return_home", s.return_home)


class SettingsDialog(QDialog):
    def __init__(self, settings: MachineSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки станка")
        self.w = QDoubleSpinBox()
        self.h = QDoubleSpinBox()
        for sb in (self.w, self.h):
            sb.setRange(10, 3000)
            sb.setSuffix(" мм")
        self.w.setValue(settings.bed_width)
        self.h.setValue(settings.bed_height)
        self.s_max = QSpinBox()
        self.s_max.setRange(1, 100000)
        self.s_max.setValue(settings.s_max)
        self.s_max.setToolTip("Должно совпадать с $30 в прошивке GRBL")
        self.travel = QDoubleSpinBox()
        self.travel.setRange(100, 50000)
        self.travel.setDecimals(0)
        self.travel.setSuffix(" мм/мин")
        self.travel.setValue(settings.travel_speed)
        self.home = QCheckBox("Возвращаться в 0,0 после задания")
        self.home.setChecked(settings.return_home)
        form = QFormLayout(self)
        form.addRow("Ширина поля (X):", self.w)
        form.addRow("Высота поля (Y):", self.h)
        form.addRow("Макс. мощность S ($30):", self.s_max)
        form.addRow("Скорость холостых / рамки:", self.travel)
        form.addRow(self.home)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def result_settings(self) -> MachineSettings:
        return MachineSettings(self.w.value(), self.h.value(), self.s_max.value(),
                               self.travel.value(), self.home.isChecked())


class _ZoomView(QGraphicsView):
    def wheelEvent(self, event):
        f = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.scale(f, f)


class PreviewDialog(QDialog):
    """Предпросмотр траектории: рез — красным, холостые — серым пунктиром."""

    def __init__(self, lines: list[str], stats, bed: tuple[float, float], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Предпросмотр G-code")
        self.resize(900, 750)
        scene = QGraphicsScene(self)
        scene.setBackgroundBrush(QColor("#5a5a5a"))
        scene.addRect(0, 0, bed[0], bed[1], QPen(QColor("#404040"), 0), QColor("#ffffff"))
        cut_pen = QPen(QColor("#d00000"), 0)
        travel_pen = QPen(QColor("#9a9a9a"), 0, Qt.PenStyle.DashLine)
        x = y = 0.0
        s = 0.0
        laser = False
        for line in lines:
            code = line.split(";", 1)[0].upper()
            words = dict(re.findall(r"([A-Z])(-?\d*\.?\d+)", code))
            if "M" in words:
                laser = words["M"] in ("3", "4")
            if "S" in words:
                s = float(words["S"])
            g = words.get("G")
            if g in ("0", "1") and ("X" in words or "Y" in words):
                nx, ny = float(words.get("X", x)), float(words.get("Y", y))
                pen = cut_pen if (g == "1" and laser and s > 0) else travel_pen
                scene.addLine(x, y, nx, ny, pen)
                x, y = nx, ny
        view = _ZoomView(scene)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        view.setTransform(QTransform.fromScale(1, -1))
        info = QLabel(f"Строк: {len(lines)}   Длина реза: {stats.cut_length:.0f} мм   "
                      f"Холостые: {stats.travel_length:.0f} мм   "
                      f"Оценка времени: {format_duration(stats.est_seconds)}")
        lay = QVBoxLayout(self)
        lay.addWidget(view)
        lay.addWidget(info)
        self._view = view
        self._rect = scene.itemsBoundingRect()

    def showEvent(self, e):
        super().showEvent(e)
        self._view.fitInView(self._rect.adjusted(-5, -5, 5, 5), Qt.AspectRatioMode.KeepAspectRatio)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.doc = Document(self.settings.bed_width, self.settings.bed_height)
        self.file_path: str | None = None
        self.modified = False
        self.worker: SerialWorker | None = None
        self.job_running = False

        self.canvas = CanvasView()
        self.setCentralWidget(self.canvas)
        self.layers = LayerPanel()
        self.machine = MachinePanel()
        self.console = ConsolePanel()

        self._dock("Слои", self.layers, Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock("Станок", self.machine, Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock("Консоль GRBL", self.console, Qt.DockWidgetArea.BottomDockWidgetArea)

        self._build_actions()
        self.cursor_label = QLabel()
        self.statusBar().addPermanentWidget(self.cursor_label)

        # Связи холста и слоёв
        self.canvas.shape_created.connect(self._on_shape_created)
        self.canvas.shapes_moved.connect(self._mark_modified)
        self.canvas.cursor_moved.connect(lambda x, y: self.cursor_label.setText(f"X: {x:.2f}  Y: {y:.2f} мм"))
        self.canvas.tool_changed.connect(self._on_tool_changed)
        self.layers.layers_changed.connect(self._on_layers_changed)
        self.layers.current_layer_changed.connect(self._on_current_layer)
        self.layers.assign_requested.connect(self._assign_layer)

        # Связи станка
        self.machine.connect_requested.connect(self.connect_machine)
        self.machine.disconnect_requested.connect(self.disconnect_machine)
        self.machine.command.connect(self.send_command)
        self.machine.realtime.connect(lambda b: self.worker and self.worker.realtime(b))
        self.machine.frame_requested.connect(self.run_frame)
        self.machine.start_requested.connect(self.start_job)
        self.machine.pause_requested.connect(lambda: self.worker and self.worker.pause())
        self.machine.resume_requested.connect(lambda: self.worker and self.worker.resume())
        self.machine.stop_requested.connect(self.stop_job)
        self.console.command.connect(self.send_command)

        q = QSettings()
        self.machine.set_port(q.value("serial/port", ""), int(q.value("serial/baud", 115200)))
        geom = q.value("window/geometry")
        if geom is not None:
            self.restoreGeometry(geom)
        else:
            self.resize(1400, 900)
        state = q.value("window/state")
        if state is not None:
            self.restoreState(state)

        self._set_document(self.doc)

    # --- построение интерфейса ---
    def _dock(self, title: str, widget, area) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(widget)
        self.addDockWidget(area, dock)
        return dock

    def _action(self, text, slot=None, shortcut=None, checkable=False, tip=None) -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if slot:
            a.triggered.connect(slot)
        a.setCheckable(checkable)
        if tip:
            a.setToolTip(tip)
        return a

    def _build_actions(self) -> None:
        new = self._action("Новый", self.new_project, QKeySequence.StandardKey.New)
        open_ = self._action("Открыть…", self.open_project, QKeySequence.StandardKey.Open)
        save = self._action("Сохранить", self.save_project, QKeySequence.StandardKey.Save)
        save_as = self._action("Сохранить как…", self.save_project_as, "Ctrl+Shift+S")
        imp = self._action("Импорт SVG/DXF…", self.import_graphics, "Ctrl+I")
        export = self._action("Экспорт G-code…", self.export_gcode, "Ctrl+E")
        quit_ = self._action("Выход", self.close, QKeySequence.StandardKey.Quit)
        delete = self._action("Удалить", self.delete_selected, QKeySequence.StandardKey.Delete)
        select_all = self._action("Выделить всё", self.canvas.select_all, QKeySequence.StandardKey.SelectAll)
        assign = self._action("Назначить текущий слой выделенным",
                              lambda: self._assign_layer(self.layers.current_layer_id()), "Ctrl+L")
        fit = self._action("Вписать поле", self.canvas.zoom_fit, "Ctrl+0")
        self.snap_action = self._action("Привязка к сетке 1 мм", self._toggle_snap, checkable=True)
        self.snap_action.setChecked(self.canvas.snap)
        preview = self._action("Предпросмотр", self.preview, "Ctrl+P")
        settings = self._action("Настройки станка…", self.edit_settings)
        about = self._action("О программе", self.about)

        self.tool_actions: dict[str, QAction] = {}
        group = QActionGroup(self)
        for tool, text, key in [(TOOL_SELECT, "Выделение", "S"), (TOOL_LINE, "Линия", "L"),
                                (TOOL_RECT, "Прямоугольник", "R"), (TOOL_ELLIPSE, "Эллипс", "E"),
                                (TOOL_POLYLINE, "Полилиния", "P")]:
            a = self._action(text, lambda _, t=tool: self.canvas.set_tool(t), key, checkable=True,
                             tip=f"{text} ({key}). Shift — квадрат/круг/угол 45°")
            group.addAction(a)
            self.tool_actions[tool] = a
        self.tool_actions[TOOL_SELECT].setChecked(True)

        mb = self.menuBar()
        m = mb.addMenu("Файл")
        for a in (new, open_, save, save_as):
            m.addAction(a)
        m.addSeparator()
        m.addAction(imp)
        m.addAction(export)
        m.addSeparator()
        m.addAction(quit_)
        m = mb.addMenu("Правка")
        for a in (delete, select_all, assign):
            m.addAction(a)
        m = mb.addMenu("Инструменты")
        for a in self.tool_actions.values():
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.snap_action)
        m = mb.addMenu("Вид")
        m.addAction(fit)
        m.addAction(preview)
        m = mb.addMenu("Станок")
        m.addAction(settings)
        m = mb.addMenu("Справка")
        m.addAction(about)

        tb = self.addToolBar("Основная")
        tb.setObjectName("main_toolbar")
        for a in (new, open_, save, imp):
            tb.addAction(a)
        tb.addSeparator()
        for a in self.tool_actions.values():
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.snap_action)
        tb.addAction(fit)
        tb.addSeparator()
        tb.addAction(preview)
        tb.addAction(export)

    # --- документ ---
    def _set_document(self, doc: Document) -> None:
        self.doc = doc
        doc.bed_width, doc.bed_height = self.settings.bed_width, self.settings.bed_height
        self.canvas.set_document(doc)
        self.layers.set_document(doc)
        self.canvas.current_layer_id = self.layers.current_layer_id()
        self.modified = False
        self._update_title()

    def _update_title(self) -> None:
        name = os.path.basename(self.file_path) if self.file_path else "Без имени"
        self.setWindowTitle(f"{name}{' *' if self.modified else ''} — {APP_NAME}")

    def _mark_modified(self) -> None:
        if not self.modified:
            self.modified = True
            self._update_title()

    def _confirm_discard(self) -> bool:
        if not self.modified:
            return True
        r = QMessageBox.question(
            self, APP_NAME, "Проект изменён. Сохранить изменения?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save_project()
        return r == QMessageBox.StandardButton.Discard

    def new_project(self) -> None:
        if self._confirm_discard():
            self.file_path = None
            self._set_document(Document(self.settings.bed_width, self.settings.bed_height))

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "", PROJECT_FILTER + ";;Все файлы (*)")
        if not path:
            return
        try:
            doc = Document.load(path)
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.critical(self, APP_NAME, f"Не удалось открыть проект:\n{e}")
            return
        self.file_path = path
        self._set_document(doc)

    def save_project(self) -> bool:
        if not self.file_path:
            return self.save_project_as()
        try:
            self.doc.save(self.file_path)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Не удалось сохранить:\n{e}")
            return False
        self.modified = False
        self._update_title()
        self.statusBar().showMessage(f"Сохранено: {self.file_path}", 3000)
        return True

    def save_project_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", self.file_path or "project.lbrn.json",
                                              PROJECT_FILTER)
        if not path:
            return False
        if not path.endswith(".json"):
            path += ".lbrn.json"
        self.file_path = path
        return self.save_project()

    def import_graphics(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Импорт графики", "",
                                              "Векторная графика (*.svg *.dxf);;SVG (*.svg);;DXF (*.dxf)")
        if path:
            self.import_path(path)

    def import_path(self, path: str) -> list[Shape]:
        try:
            groups = import_file(path)
        except Exception as e:  # библиотеки разбора бросают самые разные исключения
            QMessageBox.critical(self, APP_NAME, f"Не удалось импортировать {os.path.basename(path)}:\n{e}")
            return []
        all_paths = [p for _, polys in groups for p in polys]
        b = paths_bounds(all_paths)
        if b is None:
            QMessageBox.information(self, APP_NAME, "В файле не найдено векторных объектов.")
            return []
        # Центрируем импорт на рабочем поле
        dx = self.doc.bed_width / 2 - (b[0] + b[2]) / 2
        dy = self.doc.bed_height / 2 - (b[1] + b[3]) / 2
        self.canvas.scene().clearSelection()
        shapes = []
        for color, polys in groups:
            layer = self.doc.layer_by_color(color) if color else None
            if layer is None:
                layer = self.doc.add_layer(color) if color else self.doc.layer(self.layers.current_layer_id())
            shape = Shape(layer.id, polys, kind="import", dx=dx, dy=dy)
            self.canvas.add_shape(shape, select=True)
            shapes.append(shape)
        self.layers.rebuild()
        self.canvas.refresh_layers()
        self._mark_modified()
        self.statusBar().showMessage(
            f"Импортировано {len(all_paths)} контуров, {b[2] - b[0]:.1f} × {b[3] - b[1]:.1f} мм", 5000)
        return shapes

    def _compile(self):
        lines, stats = compile_document(self.doc, self.settings)
        has_motion = any(line.startswith("G1") for line in lines)
        return (lines if has_motion else None), stats

    def export_gcode(self) -> None:
        lines, _ = self._compile()
        if not lines:
            QMessageBox.information(self, APP_NAME, "Нечего выводить: нет объектов на включённых слоях.")
            return
        base = os.path.splitext(self.file_path or "job")[0].removesuffix(".lbrn")
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт G-code", base + ".gcode",
                                              "G-code (*.gcode *.nc *.gc)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self.statusBar().showMessage(f"G-code сохранён: {path}", 5000)

    def preview(self) -> None:
        lines, stats = self._compile()
        if not lines:
            QMessageBox.information(self, APP_NAME, "Нечего показывать: нет объектов на включённых слоях.")
            return
        PreviewDialog(lines, stats, (self.doc.bed_width, self.doc.bed_height), self).exec()

    def edit_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.result_settings()
            save_settings(self.settings)
            self.doc.bed_width, self.doc.bed_height = self.settings.bed_width, self.settings.bed_height
            self.canvas.set_document(self.doc)

    def about(self) -> None:
        QMessageBox.about(self, APP_NAME,
                          f"<b>{APP_NAME}</b><br>Управление лазерным гравёром на GRBL 1.1 "
                          "(TwoTrees и аналоги).<br>Python + PyQt6 + pySerial.")

    # --- редактирование ---
    def _on_shape_created(self, shape: Shape) -> None:
        shape.layer_id = self.layers.current_layer_id()
        self.canvas.add_shape(shape)
        self._mark_modified()

    def _on_tool_changed(self, tool: str) -> None:
        if tool in self.tool_actions:
            self.tool_actions[tool].setChecked(True)

    def _toggle_snap(self, checked: bool) -> None:
        self.canvas.snap = checked

    def _on_layers_changed(self) -> None:
        self.canvas.refresh_layers()
        self._mark_modified()

    def _on_current_layer(self, layer_id: int) -> None:
        self.canvas.current_layer_id = layer_id

    def _assign_layer(self, layer_id: int) -> None:
        shapes = self.canvas.selected_shapes()
        for s in shapes:
            s.layer_id = layer_id
        if shapes:
            self.canvas.refresh_layers()
            self._mark_modified()

    def delete_selected(self) -> None:
        shapes = self.canvas.selected_shapes()
        if shapes:
            self.canvas.remove_shapes(shapes)
            self._mark_modified()

    # --- станок ---
    def connect_machine(self, port: str, baud: int) -> None:
        if self.worker is not None or not port:
            self.machine.set_connected(self.worker is not None, "Укажите порт" if not port else "Уже подключено")
            return
        q = QSettings()
        q.setValue("serial/port", port)
        q.setValue("serial/baud", baud)
        self.worker = SerialWorker(port, baud, self)
        self.worker.connection_changed.connect(self._on_connection)
        self.worker.line_received.connect(self._on_rx)
        self.worker.line_sent.connect(self._on_tx)
        self.worker.status_changed.connect(self._on_status)
        self.worker.progress.connect(self._on_progress)
        self.worker.job_finished.connect(self._on_job_finished)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def disconnect_machine(self) -> None:
        if self.worker:
            self.worker.shutdown()

    def _on_connection(self, ok: bool, message: str) -> None:
        self.machine.set_connected(ok, message)
        self.console.append(f"[{message}]")

    def _on_worker_finished(self) -> None:
        self.worker = None
        self._set_job_running(False)
        if self.machine.connected:
            self.machine.set_connected(False, "Не подключено")

    def _on_rx(self, line: str) -> None:
        if self.job_running and line == "ok":
            return
        self.console.append(line)

    def _on_tx(self, line: str) -> None:
        if not self.job_running:
            self.console.append(f">> {line}")

    def _on_status(self, st: MachineStatus) -> None:
        self.machine.set_status(st)
        self.canvas.set_head_position(st.wpos[0], st.wpos[1])

    def _on_progress(self, done: int, total: int) -> None:
        self.machine.progress.setMaximum(max(total, 1))
        self.machine.progress.setValue(done)

    def _on_job_finished(self, ok: bool, message: str) -> None:
        self._set_job_running(False)
        self.console.append(f"[{message}]")
        self.machine.job_label.setText(message)
        if not ok:
            self.statusBar().showMessage(message, 10000)

    def _set_job_running(self, running: bool) -> None:
        self.job_running = running
        self.machine.set_job_running(running)

    def send_command(self, line: str) -> None:
        if not self.worker:
            self.console.append("[Нет подключения к станку]")
            return
        if line.strip().lower() in ("reset", "ctrl-x"):
            self.worker.realtime(RT_RESET)
            return
        self.worker.send(line)

    def run_frame(self) -> None:
        b = self.doc.bounds([s for s in self.doc.shapes if (lyr := self.doc.layer(s.layer_id)) and lyr.output])
        if b is None:
            QMessageBox.information(self, APP_NAME, "Нет объектов для рамки.")
            return
        for line in frame_gcode(b, self.settings.travel_speed):
            self.send_command(line)

    def start_job(self) -> None:
        if not self.worker:
            return
        lines, stats = self._compile()
        if not lines:
            QMessageBox.information(self, APP_NAME, "Нечего выполнять: нет объектов на включённых слоях.")
            return
        b = self.doc.bounds()
        if b and (b[0] < 0 or b[1] < 0 or b[2] > self.doc.bed_width or b[3] > self.doc.bed_height):
            if QMessageBox.warning(self, APP_NAME, "Часть объектов выходит за рабочее поле. Продолжить?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                   ) != QMessageBox.StandardButton.Yes:
                return
        self.machine.job_label.setText(
            f"Рез {stats.cut_length:.0f} мм, оценка времени {format_duration(stats.est_seconds)}")
        self._set_job_running(True)
        self.worker.start_job(lines)

    def stop_job(self) -> None:
        if self.worker:
            self.worker.stop_job()

    def closeEvent(self, event) -> None:
        if self.job_running and QMessageBox.question(
                self, APP_NAME, "Идёт задание. Остановить его и выйти?") != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        if not self._confirm_discard():
            event.ignore()
            return
        if self.worker:
            self.worker.shutdown()  # при активном задании поток сам остановит станок
        q = QSettings()
        q.setValue("window/geometry", self.saveGeometry())
        q.setValue("window/state", self.saveState())
        event.accept()
